"""Sideloadly Windows MCP Server (v0.2.0).

Provides full programmatic control over Sideloadly on Windows:
- Auto-discovers Sideloadly installation (env var, standard paths, Windows Registry, running process),
  SQLite database, MD5 IPA cache, and roaming session directories
- Reads and manages iOS/iPadOS devices, installations, Apple ID App ID quotas, and options
- Enqueues app refreshes and new/updated IPA installations via Sideloadly's SQLite DB + localhost:28811 IPC API
  with cold-start retries, SQLite lock handling, and accurate completion/error tracking
- Parses IPA Info.plist metadata automatically
- Manages Sideloadly GUI / Daemon processes, stuck queues, and daemon logs
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("sideloadly")

IPC_BASE_URL = "http://127.0.0.1:28811"
ZERO_TIME = "0001-01-01 00:00:00+00:00"

T = TypeVar("T")


def _discover_sideloadly_dir_from_registry() -> Optional[Path]:
    """Attempt to locate Sideloadly installation directory from Windows Registry."""
    if os.name != "nt":
        return None
    try:
        import winreg

        registry_paths = [
            (winreg.HKEY_CURRENT_USER, r"Software\Classes\sideloadly\shell\open\command"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Classes\sideloadly\shell\open\command"),
        ]
        for hive, subkey in registry_paths:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    if isinstance(val, str):
                        m = re.search(r'"?([A-Za-z]:\\[^"]*?sideloadly\.exe)"?', val, re.IGNORECASE)
                        if m:
                            exe_path = Path(m.group(1))
                            if exe_path.exists():
                                return exe_path.parent
            except OSError:
                continue
    except Exception:
        pass
    return None


def _discover_sideloadly_dir_from_process() -> Optional[Path]:
    """Attempt to locate Sideloadly directory from a currently running process on Windows."""
    if os.name != "nt":
        return None
    try:
        cmd = (
            "Get-Process -Name sideloadly,sideloadlydaemon -ErrorAction SilentlyContinue "
            "| Select-Object -First 1 -ExpandProperty Path"
        )
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", cmd],
            text=True,
            timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).strip()
        if out:
            p = Path(out)
            if p.exists():
                return p.parent
    except Exception:
        pass
    return None


class SideloadlyPaths:
    """Dynamically resolves Sideloadly paths on any Windows machine."""

    def __init__(self) -> None:
        self.is_windows = os.name == "nt"
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        roaming_app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))

        custom_dir = os.environ.get("SIDELOADLY_DIR")
        if custom_dir and Path(custom_dir).exists():
            self.local_dir = Path(custom_dir)
        else:
            candidates = [
                local_app_data / "Sideloadly",
                Path("C:/Program Files/Sideloadly"),
                Path("C:/Program Files (x86)/Sideloadly"),
            ]
            found = next((p for p in candidates if (p / "sideloadly.exe").exists() or (p / "installations.db").exists()), None)
            if found is None:
                found = next((p for p in candidates if p.exists()), None)
            if found is None:
                found = _discover_sideloadly_dir_from_registry()
            if found is None:
                found = _discover_sideloadly_dir_from_process()
            self.local_dir = found if found is not None else (local_app_data / "Sideloadly")

        self.roaming_dir = roaming_app_data / "Sideloadly"
        self.cache_dir = local_app_data / "cache" / "sideloadly"

        self.exe_path = self.local_dir / "sideloadly.exe"
        self.daemon_exe_path = self.local_dir / "sideloadlydaemon.exe"
        self.db_path = self.local_dir / "installations.db"
        self.daemon_log_path = self.local_dir / "sideloadlydaemon.log"
        self.account_appids_path = self.local_dir / "account-appids.json"
        self.anisette_dir = self.local_dir / "an"
        self.sessions_path = self.roaming_dir / "sessions.json"
        self.machine_id_path = self.roaming_dir / "machine_id.txt"


def get_paths() -> SideloadlyPaths:
    return SideloadlyPaths()


def _check_platform_error() -> Optional[dict[str, Any]]:
    if os.name != "nt" and not os.environ.get("SIDELOADLY_ALLOW_NON_WINDOWS"):
        return {
            "error": "Sideloadly MCP currently supports Windows only (detected non-Windows OS).",
            "platform": os.name,
        }
    return None


def get_db_connection(readonly: bool = False) -> sqlite3.Connection:
    paths = get_paths()
    if not paths.db_path.exists():
        raise FileNotFoundError(
            f"Sideloadly database not found at {paths.db_path}. "
            "Open Sideloadly at least once so it initializes installations.db."
        )
    if readonly:
        uri = f"file:{paths.db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    else:
        conn = sqlite3.connect(str(paths.db_path), timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn


def _run_db_with_retry(fn: Callable[[sqlite3.Connection], T], readonly: bool = False, retries: int = 5) -> T:
    """Execute a SQLite operation with automatic retry on transient lock/busy errors, always closing the handle."""
    last_err: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = get_db_connection(readonly=readonly)
            return fn(conn)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if ("locked" in msg or "busy" in msg) and attempt < retries:
                last_err = e
                time.sleep(0.25 * attempt)
                continue
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    if last_err:
        raise last_err
    raise RuntimeError("Unexpected SQLite retry exit")


def get_running_processes() -> dict[str, list[int]]:
    """Return PIDs for sideloadly.exe and sideloadlydaemon.exe."""
    result: dict[str, list[int]] = {"sideloadly.exe": [], "sideloadlydaemon.exe": []}
    if os.name != "nt":
        return result
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq sideloadly*", "/FO", "CSV", "/NH"],
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in output.strip().splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                name = parts[0].lower()
                if name in result and parts[1].isdigit():
                    result[name].append(int(parts[1]))
    except Exception:
        pass
    return result


def check_ipc_alive() -> bool:
    """Check if Sideloadly's localhost:28811 IPC server is listening."""
    try:
        r = httpx.get(f"{IPC_BASE_URL}/", timeout=1.5)
        return r.status_code in (200, 404)
    except Exception:
        return False


def ensure_sideloadly_running(mode: str = "enqueue", wait_seconds: float = 10.0) -> dict[str, Any]:
    """Ensure sideloadly.exe is running and listening on port 28811.

    Args:
        mode: 'enqueue' launches `sideloadly.exe --silent ENQUEUE` (headless background processing).
              'gui' launches `sideloadly.exe` normally so the window stays open.
    """
    if check_ipc_alive():
        return {"running": True, "started_now": False, "mode": mode}

    paths = get_paths()
    if not paths.exe_path.exists():
        return {"running": False, "error": f"sideloadly.exe not found at {paths.exe_path}"}

    args = [str(paths.exe_path), "--silent", "ENQUEUE"] if mode == "enqueue" else [str(paths.exe_path)]
    subprocess.Popen(
        args,
        cwd=str(paths.local_dir),
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if check_ipc_alive():
            return {"running": True, "started_now": True, "mode": mode}
        time.sleep(0.5)

    return {"running": False, "started_now": True, "mode": mode, "error": "Timed out waiting for IPC port 28811"}


def enqueue_via_ipc(
    ins_id: int,
    before_state: Optional[dict[str, Any]] = None,
    wait_for_completion: bool = True,
    timeout_seconds: float = 240.0,
) -> dict[str, Any]:
    """Trigger Sideloadly's /enqueue endpoint with retries, then optionally wait until the install finishes.

    Sideloadly's IPC port can accept connections before its queue is ready (right after a cold start),
    in which case /enqueue answers 'Task enqueue fail'. We retry until it answers 'Task enqueued'.
    Completion is detected when:
      - `last_updated` changes (installed)
      - `failures_count` increases, `last_failure_at` changes, or a new non-empty `last_error` appears (failed)
      - `enqueued_at` is cleared back to zero without an update or error (cleared_without_update)
    """
    proc_status = ensure_sideloadly_running(mode="enqueue")
    result: dict[str, Any] = {"sideloadly_process": proc_status, "enqueued": False}

    if before_state is None:
        def _read_before(conn: sqlite3.Connection) -> dict[str, Any]:
            r = conn.execute(
                "SELECT last_updated, last_error, failures_count, last_failure_at FROM installations WHERE id = ?",
                (ins_id,),
            ).fetchone()
            return dict(r) if r else {}

        before_state = _run_db_with_retry(_read_before, readonly=True)

    before_updated = before_state.get("last_updated")
    before_error = before_state.get("last_error") or ""
    before_failures = before_state.get("failures_count") or 0
    before_failure_at = before_state.get("last_failure_at") or ZERO_TIME

    last_text = ""
    last_code = None
    for attempt in range(1, 9):
        try:
            resp = httpx.get(f"{IPC_BASE_URL}/enqueue", params={"id": str(ins_id)}, timeout=10.0)
            last_code = resp.status_code
            last_text = resp.text.strip()
        except Exception as e:
            last_text = f"IPC request error: {e}"
        if last_text.startswith("Task enqueued"):
            result["enqueued"] = True
            break
        time.sleep(min(2.0 * attempt, 8.0))

    result.update({"ipc_status_code": last_code, "ipc_response": last_text})
    if not result["enqueued"] or not wait_for_completion:
        return result

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(3.0)

        def _poll(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            r = conn.execute(
                "SELECT last_updated, last_error, failures_count, last_failure_at, enqueued_at, enqueue_token "
                "FROM installations WHERE id = ?",
                (ins_id,),
            ).fetchone()
            return dict(r) if r else None

        row = _run_db_with_retry(_poll, readonly=True)
        if not row:
            break

        if row["last_updated"] != before_updated:
            result.update({"completed": True, "status": "installed", "last_updated": row["last_updated"]})
            return result

        cur_failures = row["failures_count"] or 0
        cur_failure_at = row["last_failure_at"] or ZERO_TIME
        cur_error = row["last_error"] or ""
        new_failure = (
            cur_failures > before_failures
            or cur_failure_at != before_failure_at
            or (cur_error != "" and cur_error != before_error)
        )
        if new_failure:
            result.update(
                {
                    "completed": True,
                    "status": "failed",
                    "last_error": cur_error,
                    "failures_count": cur_failures,
                    "last_failure_at": cur_failure_at,
                }
            )
            return result

        enq_at = str(row.get("enqueued_at") or "")
        enq_tok = row.get("enqueue_token") or ""
        if enq_at.startswith("0001-01-01") and not enq_tok:
            result.update({"completed": False, "status": "cleared_without_update"})
            return result

    result.update({"completed": False, "status": "timeout_waiting_for_install"})
    return result


def parse_iso_datetime(val: Optional[str]) -> Optional[datetime.datetime]:
    if not val or val.startswith("0001-01-01"):
        return None
    try:
        cleaned = re.sub(r"\.(\d{6})\d+", r".\1", val.strip())
        return datetime.datetime.fromisoformat(cleaned)
    except Exception:
        return None


def now_go_iso() -> str:
    """Format current local time in Go/SQLite ISO format with timezone."""
    return datetime.datetime.now().astimezone().isoformat(sep=" ")


def _discover_accounts(paths: SideloadlyPaths) -> list[str]:
    """Discover configured Apple IDs from account-appids.json, cert-*.pem, and sessions.json."""
    accounts: list[str] = []
    if paths.account_appids_path.exists():
        try:
            data = json.loads(paths.account_appids_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k in data.keys():
                    if k and k not in accounts:
                        accounts.append(k)
        except Exception:
            pass

    if paths.roaming_dir.exists():
        for c in paths.roaming_dir.glob("cert-*.pem"):
            email = c.name.removeprefix("cert-").removesuffix(".pem")
            if email and email not in accounts:
                accounts.append(email)

    if paths.sessions_path.exists():
        try:
            sdata = json.loads(paths.sessions_path.read_text(encoding="utf-8"))
            if isinstance(sdata, dict):
                for k in sdata.keys():
                    if isinstance(k, str) and "@" in k:
                        email = k.split(":", 1)[0].strip()
                        if email and email not in accounts:
                            accounts.append(email)
        except Exception:
            pass

    return accounts


def _discover_device_udid_from_log(paths: SideloadlyPaths) -> Optional[str]:
    if not paths.daemon_log_path.exists():
        return None
    try:
        text = paths.daemon_log_path.read_text(encoding="utf-8", errors="replace")
        matches = list(re.finditer(r"Installed apps on ([0-9A-Za-z-]+):", text))
        if matches:
            return matches[-1].group(1)
    except Exception:
        pass
    return None


def _read_anisette_mode(paths: SideloadlyPaths, fallback: int = 2) -> int:
    opt = paths.local_dir / "option-anisette-mode"
    if opt.exists():
        try:
            return int(opt.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    return fallback


def _enrich_installation_row(row: dict[str, Any], paths: SideloadlyPaths, now: datetime.datetime) -> dict[str, Any]:
    item = dict(row)
    last_upd = parse_iso_datetime(item.get("last_updated"))
    ttl_days = item.get("known_ttl") or 7
    if last_upd:
        expires_at = last_upd + datetime.timedelta(days=ttl_days)
        hours_left = round((expires_at - now).total_seconds() / 3600.0, 1)
        item["expires_at"] = expires_at.isoformat()
        item["hours_left"] = hours_left
        item["is_expired"] = hours_left <= 0
        item["needs_refresh"] = hours_left < (item.get("refresh_at_hours") or 96)
    else:
        item["expires_at"] = None
        item["hours_left"] = None
        item["is_expired"] = None
        item["needs_refresh"] = True

    enq_at = item.get("enqueued_at")
    item["is_enqueued"] = bool(
        enq_at and not str(enq_at).startswith("0001-01-01") and item.get("enqueue_token")
    )
    cached_name = item.get("cached_ipa_filename")
    if cached_name:
        cached_full = paths.cache_dir / cached_name
        item["cached_ipa_path"] = str(cached_full)
        item["cached_ipa_exists"] = cached_full.exists()
    return item


def inspect_ipa_metadata(ipa_path: Path) -> dict[str, Any]:
    """Extract Info.plist metadata and list frameworks/dylibs/plugins from an IPA file."""
    if not ipa_path.exists():
        raise FileNotFoundError(f"IPA file not found: {ipa_path}")

    with zipfile.ZipFile(ipa_path, "r") as zf:
        names = zf.namelist()
        info_plist_entry = next(
            (n for n in names if re.match(r"^Payload/[^/]+\.app/Info\.plist$", n)),
            None,
        )
        if not info_plist_entry:
            raise ValueError(f"No Payload/*.app/Info.plist found inside {ipa_path}")

        raw_plist = zf.read(info_plist_entry)
        plist_data = plistlib.loads(raw_plist)

        app_dir = info_plist_entry.rsplit("/", 1)[0]
        plugins = sorted(
            {
                n[len(app_dir) + 1 :].split("/")[1]
                for n in names
                if n.startswith(f"{app_dir}/PlugIns/") and n.count("/") >= 3
            }
        )
        frameworks = sorted(
            {
                n[len(app_dir) + 1 :].split("/")[1]
                for n in names
                if n.startswith(f"{app_dir}/Frameworks/") and n.count("/") >= 3
            }
        )

        return {
            "ipa_path": str(ipa_path.resolve()),
            "file_size_bytes": ipa_path.stat().st_size,
            "app_bundle_folder": app_dir.split("/")[1],
            "bundle_name": plist_data.get("CFBundleDisplayName")
            or plist_data.get("CFBundleName")
            or ipa_path.stem,
            "bundle_id": plist_data.get("CFBundleIdentifier", ""),
            "version": plist_data.get("CFBundleShortVersionString", "1.0"),
            "build_version": plist_data.get("CFBundleVersion", "1"),
            "minimum_os_version": plist_data.get("MinimumOSVersion", ""),
            "executable_name": plist_data.get("CFBundleExecutable", ""),
            "plugins": plugins,
            "frameworks": frameworks,
        }


@mcp.tool()
def sideloadly_doctor() -> dict[str, Any]:
    """Run a comprehensive health and environment check for Sideloadly on this Windows PC.
    Works on any newly configured machine to verify paths, processes, IPC port, Apple ID session, and Anisette libraries.
    """
    plat_err = _check_platform_error()
    if plat_err:
        return {"healthy": False, **plat_err}

    paths = get_paths()
    procs = get_running_processes()
    ipc_ok = check_ipc_alive()

    anisette_dlls = list(paths.anisette_dir.glob("*.dll")) if paths.anisette_dir.exists() else []
    accounts = _discover_accounts(paths)

    device_count = 0
    installation_count = 0
    if paths.db_path.exists():
        try:
            def _counts(conn: sqlite3.Connection) -> tuple[int, int]:
                dc = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
                ic = conn.execute("SELECT COUNT(*) FROM installations WHERE deleted_at IS NULL").fetchone()[0]
                return dc, ic

            device_count, installation_count = _run_db_with_retry(_counts, readonly=True)
        except Exception:
            pass

    issues: list[str] = []
    if not paths.exe_path.exists():
        issues.append(f"Sideloadly executable not found at {paths.exe_path}. Install Sideloadly or set SIDELOADLY_DIR.")
    if not anisette_dlls:
        issues.append("Local Anisette DLLs ('an' folder) not found. Open Sideloadly once to download Anisette libraries.")
    if not accounts and not paths.sessions_path.exists():
        issues.append("No Apple ID session found. Log in with your Apple ID in Sideloadly GUI once.")
    if device_count == 0:
        issues.append("No iOS/iPadOS devices found in database. Connect your device via USB and trust this computer.")

    return {
        "healthy": len(issues) == 0,
        "ipc_port_28811_listening": ipc_ok,
        "processes": {
            "sideloadly_gui_pids": procs["sideloadly.exe"],
            "sideloadly_daemon_pids": procs["sideloadlydaemon.exe"],
        },
        "paths": {
            "local_dir": str(paths.local_dir),
            "roaming_dir": str(paths.roaming_dir),
            "cache_dir": str(paths.cache_dir),
            "db_exists": paths.db_path.exists(),
            "sessions_exists": paths.sessions_path.exists(),
            "anisette_dll_count": len(anisette_dlls),
        },
        "configured_apple_ids": accounts,
        "registered_devices_count": device_count,
        "active_installations_count": installation_count,
        "recommendations": issues,
    }


@mcp.tool()
def list_devices() -> dict[str, Any]:
    """List all iOS/iPadOS/tvOS/Mac devices registered in Sideloadly along with their installation counts and status."""
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err
    try:
        def _query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            devices = [dict(r) for r in conn.execute("SELECT * FROM devices").fetchall()]
            for d in devices:
                count = conn.execute(
                    "SELECT COUNT(*) FROM installations WHERE device_udid = ? AND deleted_at IS NULL",
                    (d["udid"],),
                ).fetchone()[0]
                d["active_installations_count"] = count
                d["is_m1"] = bool(d.get("is_m1"))
                d["is_tv"] = bool(d.get("is_tv"))
            return devices

        devices = _run_db_with_retry(_query, readonly=True)
        return {"count": len(devices), "devices": devices}
    except FileNotFoundError as e:
        return {"count": 0, "devices": [], "error": str(e)}


@mcp.tool()
def list_installations(device_udid: Optional[str] = None) -> dict[str, Any]:
    """List all Sideloadly app installations, including expiration countdown, cached IPA details, bundle IDs, and error status."""
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    query = """
        SELECT
            i.*,
            d.name AS device_name,
            sf.stored_name AS cached_ipa_filename,
            sf.nickname AS original_ipa_name,
            sf.size AS ipa_size_bytes,
            sf.uncached_path AS ipa_uncached_path
        FROM installations i
        LEFT JOIN devices d ON d.udid = i.device_udid
        LEFT JOIN stored_files sf ON sf.id = i.ipa_id
        WHERE i.deleted_at IS NULL
    """
    params: list[Any] = []
    if device_udid:
        query += " AND i.device_udid = ?"
        params.append(device_udid)
    query += " ORDER BY i.updated_at DESC"

    paths = get_paths()
    now = datetime.datetime.now().astimezone()

    try:
        def _query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(query, params).fetchall()
            return [_enrich_installation_row(dict(r), paths, now) for r in rows]

        items = _run_db_with_retry(_query, readonly=True)
        return {"count": len(items), "installations": items}
    except FileNotFoundError as e:
        return {"count": 0, "installations": [], "error": str(e)}


@mcp.tool()
def get_installation_status(installation_id: int) -> dict[str, Any]:
    """Get live status of a single Sideloadly installation (useful for polling after wait_for_completion=False)."""
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    paths = get_paths()
    now = datetime.datetime.now().astimezone()

    try:
        def _query(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
            row = conn.execute(
                """
                SELECT
                    i.*,
                    d.name AS device_name,
                    sf.stored_name AS cached_ipa_filename,
                    sf.nickname AS original_ipa_name,
                    sf.size AS ipa_size_bytes,
                    sf.uncached_path AS ipa_uncached_path
                FROM installations i
                LEFT JOIN devices d ON d.udid = i.device_udid
                LEFT JOIN stored_files sf ON sf.id = i.ipa_id
                WHERE i.id = ? AND i.deleted_at IS NULL
                """,
                (installation_id,),
            ).fetchone()
            return _enrich_installation_row(dict(row), paths, now) if row else None

        item = _run_db_with_retry(_query, readonly=True)
        if not item:
            return {"found": False, "installation_id": installation_id, "error": "Installation not found"}
        return {"found": True, "installation": item}
    except FileNotFoundError as e:
        return {"found": False, "installation_id": installation_id, "error": str(e)}


@mcp.tool()
def get_apple_id_quota() -> dict[str, Any]:
    """Get Apple ID remaining App ID quota (10 per 7 days on free accounts), nearest expiration date, and certificate files."""
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    paths = get_paths()
    quotas: dict[str, Any] = {}
    if paths.account_appids_path.exists():
        try:
            quotas = json.loads(paths.account_appids_path.read_text(encoding="utf-8"))
        except Exception as e:
            quotas = {"error": str(e)}

    certs = []
    if paths.roaming_dir.exists():
        for cert_file in paths.roaming_dir.glob("cert-*.pem"):
            certs.append(
                {
                    "apple_id": cert_file.name.removeprefix("cert-").removesuffix(".pem"),
                    "cert_path": str(cert_file),
                    "modified_at": datetime.datetime.fromtimestamp(cert_file.stat().st_mtime).isoformat(),
                }
            )

    return {
        "quotas": quotas,
        "certificates": certs,
        "discovered_apple_ids": _discover_accounts(paths),
        "sessions_file_exists": paths.sessions_path.exists(),
    }


@mcp.tool()
def get_sideloadly_options() -> dict[str, Any]:
    """Read Sideloadly configuration options (anisette-mode, zipstream, ignore-preflight, custom-chunk-size)."""
    paths = get_paths()
    options: dict[str, str] = {}
    if paths.local_dir.exists():
        for opt_file in paths.local_dir.glob("option-*"):
            try:
                options[opt_file.name.removeprefix("option-")] = opt_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
    return {"options": options}


@mcp.tool()
def set_sideloadly_option(option_name: str, value: str) -> dict[str, Any]:
    """Set a Sideloadly option file (e.g. 'anisette-mode', 'zipstream', 'ignore-preflight', 'custom-chunk-size')."""
    clean_name = option_name.removeprefix("option-").strip()
    if not re.match(r"^[a-z0-9-]+$", clean_name):
        raise ValueError(f"Invalid option name: {option_name}")
    paths = get_paths()
    paths.local_dir.mkdir(parents=True, exist_ok=True)
    opt_path = paths.local_dir / f"option-{clean_name}"
    opt_path.write_text(str(value).strip(), encoding="utf-8")
    return {"option": clean_name, "value": str(value).strip(), "path": str(opt_path)}


@mcp.tool()
def inspect_ipa(ipa_path: str) -> dict[str, Any]:
    """Inspect an .ipa file on disk and extract its Info.plist metadata (Bundle ID, Display Name, Version, Minimum iOS, PlugIns, Frameworks)."""
    return inspect_ipa_metadata(Path(ipa_path))


@mcp.tool()
def trigger_refresh(
    installation_id: Optional[int] = None,
    app_name_or_bundle_id: Optional[str] = None,
    clear_errors: bool = True,
    wait_for_completion: bool = True,
) -> dict[str, Any]:
    """Enqueue an existing Sideloadly installation for immediate re-signing and installation on the target iOS device.
    You can specify either `installation_id` or `app_name_or_bundle_id` (e.g., 'Luma Portal' or 'com.luma.portal').
    """
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    if installation_id is None and not app_name_or_bundle_id:
        raise ValueError("Provide either installation_id or app_name_or_bundle_id.")

    def _enqueue_db(conn: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, Any], str]:
        if installation_id is not None:
            row = conn.execute(
                "SELECT * FROM installations WHERE id = ? AND deleted_at IS NULL",
                (installation_id,),
            ).fetchone()
        else:
            q = f"%{app_name_or_bundle_id}%"
            row = conn.execute(
                """
                SELECT * FROM installations
                WHERE deleted_at IS NULL AND (name LIKE ? OR final_bundle_id LIKE ?)
                ORDER BY updated_at DESC LIMIT 1
                """,
                (q, q),
            ).fetchone()

        if not row:
            raise LookupError("Matching installation not found in installations.db")

        ins = dict(row)
        before_state = {
            "last_updated": ins.get("last_updated"),
            "last_error": "" if clear_errors else (ins.get("last_error") or ""),
            "failures_count": 0 if clear_errors else (ins.get("failures_count") or 0),
            "last_failure_at": ins.get("last_failure_at") or ZERO_TIME,
        }

        ins_id = ins["id"]
        token = secrets.token_hex(16)
        now_str = now_go_iso()

        if clear_errors:
            conn.execute(
                """
                UPDATE installations
                SET enqueued_at = ?, enqueue_token = ?, updated_at = ?, last_error = '', failures_count = 0
                WHERE id = ?
                """,
                (now_str, token, now_str, ins_id),
            )
        else:
            conn.execute(
                """
                UPDATE installations
                SET enqueued_at = ?, enqueue_token = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_str, token, now_str, ins_id),
            )
        conn.commit()
        return ins, before_state, token

    ins, before_state, token = _run_db_with_retry(_enqueue_db, readonly=False)
    ins_id = ins["id"]

    outcome = enqueue_via_ipc(ins_id, before_state=before_state, wait_for_completion=wait_for_completion)
    return {
        "installation_id": ins_id,
        "app_name": ins["name"],
        "final_bundle_id": ins["final_bundle_id"],
        "device_udid": ins["device_udid"],
        "enqueue_token": token,
        **outcome,
    }


@mcp.tool()
def install_or_update_ipa(
    ipa_path: str,
    device_udid: Optional[str] = None,
    apple_id: Optional[str] = None,
    custom_bundle_id: Optional[str] = None,
    custom_app_name: Optional[str] = None,
    drop_plugins: bool = False,
    refresh_at_hours: int = 96,
    wait_for_completion: bool = True,
) -> dict[str, Any]:
    """Install a new .ipa file or update an existing installation's cached .ipa and immediately enqueue it in Sideloadly.

    - Automatically hashes the .ipa (MD5), copies it into Sideloadly's cache directory (`%LOCALAPPDATA%\\cache\\sideloadly\\<md5>.ipa`),
      registers it in `stored_files`, creates or updates the `installations` record, and triggers `http://127.0.0.1:28811/enqueue?id=<id>`.
    - If `device_udid` or `apple_id` are omitted, automatically discovers the primary registered device and Apple ID from Sideloadly.
    """
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    src_ipa = Path(ipa_path).resolve()
    meta = inspect_ipa_metadata(src_ipa)
    paths = get_paths()
    paths.cache_dir.mkdir(parents=True, exist_ok=True)

    # Calculate MD5 hash of the IPA (Sideloadly stores 16-byte raw MD5 in stored_files.hash and <md5_hex>.ipa on disk)
    md5 = hashlib.md5()
    with src_ipa.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            md5.update(chunk)
    md5_bytes = md5.digest()
    md5_hex = md5.hexdigest()
    stored_name = f"{md5_hex}.ipa"
    cached_ipa_path = paths.cache_dir / stored_name

    if not cached_ipa_path.exists() or cached_ipa_path.stat().st_size != src_ipa.stat().st_size:
        shutil.copy2(src_ipa, cached_ipa_path)

    now_str = now_go_iso()

    def _upsert_db(conn: sqlite3.Connection) -> dict[str, Any]:
        nonlocal device_udid, apple_id

        # 1. Resolve device_udid if not provided
        if not device_udid:
            dev_row = conn.execute(
                """
                SELECT device_udid FROM installations
                WHERE deleted_at IS NULL AND device_udid IS NOT NULL AND device_udid != ''
                GROUP BY device_udid ORDER BY COUNT(*) DESC LIMIT 1
                """
            ).fetchone()
            if dev_row:
                device_udid = dev_row[0]
            else:
                dev_row2 = conn.execute("SELECT udid FROM devices LIMIT 1").fetchone()
                if dev_row2:
                    device_udid = dev_row2[0]
                else:
                    device_udid = _discover_device_udid_from_log(paths)
        if not device_udid:
            raise ValueError("No iOS device found in Sideloadly database. Connect a device or specify device_udid.")

        # 2. Resolve apple_id and defaults (supports brand-new PCs with 0 prior installations)
        existing_ins = conn.execute(
            "SELECT apple_id, final_bundle_id, anisette_mode, sign_mode FROM installations "
            "WHERE deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()

        if not apple_id:
            if existing_ins and existing_ins["apple_id"]:
                apple_id = existing_ins["apple_id"]
            else:
                discovered = _discover_accounts(paths)
                if discovered:
                    apple_id = discovered[0]
        if not apple_id:
            raise ValueError("No Apple ID found in Sideloadly. Specify apple_id or log in via Sideloadly GUI once.")

        default_anisette = existing_ins["anisette_mode"] if existing_ins else 2
        anisette_mode = _read_anisette_mode(paths, fallback=default_anisette)
        sign_mode = existing_ins["sign_mode"] if existing_ins else 3

        # Determine final_bundle_id (preserve team ID suffix if Sideloadly uses one, e.g. .R5J9P5LKTH)
        if custom_bundle_id:
            final_bundle_id = custom_bundle_id
        else:
            base_bundle = meta["bundle_id"]
            team_suffix = ""
            if existing_ins and existing_ins["final_bundle_id"]:
                parts = existing_ins["final_bundle_id"].rsplit(".", 1)
                if len(parts) == 2 and re.match(r"^[A-Z0-9]{10}$", parts[1]):
                    team_suffix = f".{parts[1]}"
            if team_suffix and not base_bundle.endswith(team_suffix):
                final_bundle_id = f"{base_bundle}{team_suffix}"
            else:
                final_bundle_id = base_bundle

        app_name = custom_app_name or meta["bundle_name"] or src_ipa.stem

        # 3. Upsert into stored_files
        sf_row = conn.execute(
            "SELECT id FROM stored_files WHERE stored_name = ?",
            (stored_name,),
        ).fetchone()
        if sf_row:
            ipa_id = sf_row["id"]
            conn.execute(
                "UPDATE stored_files SET updated_at = ?, deleted_at = NULL, nickname = ?, size = ?, hash = ? WHERE id = ?",
                (now_str, src_ipa.name, src_ipa.stat().st_size, md5_bytes, ipa_id),
            )
        else:
            cur = conn.execute(
                """
                INSERT INTO stored_files (created_at, updated_at, deleted_at, stored_name, nickname, ext, size, hash, uncached_path)
                VALUES (?, ?, NULL, ?, ?, '.ipa', ?, ?, '')
                """,
                (now_str, now_str, stored_name, src_ipa.name, src_ipa.stat().st_size, md5_bytes),
            )
            ipa_id = cur.lastrowid

        # 4. Upsert into installations
        token = secrets.token_hex(16)
        ins_row = conn.execute(
            """
            SELECT id, last_updated, last_failure_at FROM installations
            WHERE device_udid = ? AND final_bundle_id = ? AND one_off = 0 AND deleted_at IS NULL
            LIMIT 1
            """,
            (device_udid, final_bundle_id),
        ).fetchone()

        if ins_row:
            ins_id = ins_row["id"]
            before_state = {
                "last_updated": ins_row["last_updated"],
                "last_error": "",
                "failures_count": 0,
                "last_failure_at": ins_row["last_failure_at"] or ZERO_TIME,
            }
            conn.execute(
                """
                UPDATE installations
                SET updated_at = ?, name = ?, ipa_id = ?, apple_id = ?, version = ?,
                    drop_plugins = ?, refresh_at_hours = ?, enqueued_at = ?, enqueue_token = ?,
                    last_error = '', failures_count = 0
                WHERE id = ?
                """,
                (
                    now_str,
                    app_name,
                    ipa_id,
                    apple_id,
                    meta["version"],
                    1 if drop_plugins else 0,
                    refresh_at_hours,
                    now_str,
                    token,
                    ins_id,
                ),
            )
            action = "updated_existing_installation"
        else:
            before_state = {
                "last_updated": ZERO_TIME,
                "last_error": "",
                "failures_count": 0,
                "last_failure_at": ZERO_TIME,
            }
            cur = conn.execute(
                """
                INSERT INTO installations (
                    created_at, updated_at, deleted_at, name, ipa_id, device_udid, apple_id,
                    anisette_mode, info_props, sign_mode, drop_plugins, alt_entitlements_id,
                    last_updated, version, final_bundle_id, one_off, installation_token,
                    known_ttl, refresh_at_hours, enqueued_at, enqueue_token,
                    last_error, failures_count, last_failure_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, '{}', ?, ?, NULL, ?, ?, ?, 0, '', 7, ?, ?, ?, '', 0, ?)
                """,
                (
                    now_str,
                    now_str,
                    app_name,
                    ipa_id,
                    device_udid,
                    apple_id,
                    anisette_mode,
                    sign_mode,
                    1 if drop_plugins else 0,
                    ZERO_TIME,
                    meta["version"],
                    final_bundle_id,
                    refresh_at_hours,
                    now_str,
                    token,
                    ZERO_TIME,
                ),
            )
            ins_id = cur.lastrowid
            action = "created_new_installation"

        conn.commit()
        return {
            "action": action,
            "ins_id": ins_id,
            "ipa_id": ipa_id,
            "app_name": app_name,
            "final_bundle_id": final_bundle_id,
            "device_udid": device_udid,
            "apple_id": apple_id,
            "before_state": before_state,
        }

    db_res = _run_db_with_retry(_upsert_db, readonly=False)

    # 5. Notify Sideloadly IPC (with retries) and optionally wait for the install to finish
    outcome = enqueue_via_ipc(
        db_res["ins_id"],
        before_state=db_res["before_state"],
        wait_for_completion=wait_for_completion,
    )
    return {
        "action": db_res["action"],
        "installation_id": db_res["ins_id"],
        "stored_file_id": db_res["ipa_id"],
        "cached_ipa_path": str(cached_ipa_path),
        "app_name": db_res["app_name"],
        "version": meta["version"],
        "final_bundle_id": db_res["final_bundle_id"],
        "device_udid": db_res["device_udid"],
        "apple_id": db_res["apple_id"],
        **outcome,
    }


@mcp.tool()
def cancel_queue(installation_id: Optional[int] = None) -> dict[str, Any]:
    """Clear pending/stuck enqueued tasks in Sideloadly's database.
    If `installation_id` is provided, clears only that installation; otherwise clears all enqueued tasks.
    """
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    def _clear(conn: sqlite3.Connection) -> int:
        if installation_id is not None:
            cur = conn.execute(
                "UPDATE installations SET enqueued_at = ?, enqueue_token = '' WHERE id = ? AND enqueued_at NOT LIKE '0001-01-01%'",
                (ZERO_TIME, installation_id),
            )
        else:
            cur = conn.execute(
                "UPDATE installations SET enqueued_at = ?, enqueue_token = '' WHERE enqueued_at NOT LIKE '0001-01-01%'",
                (ZERO_TIME,),
            )
        conn.commit()
        return cur.rowcount

    try:
        cleared = _run_db_with_retry(_clear, readonly=False)
        return {"cleared_count": cleared, "installation_id": installation_id}
    except FileNotFoundError as e:
        return {"cleared_count": 0, "error": str(e)}


@mcp.tool()
def get_device_installed_apps(search_query: Optional[str] = None) -> dict[str, Any]:
    """Parse Sideloadly daemon log to list all user-installed apps detected on the connected iOS/iPadOS device.
    Optionally filter by `search_query` (matches bundle ID or app name).
    """
    paths = get_paths()
    if not paths.daemon_log_path.exists():
        return {"error": f"Daemon log not found at {paths.daemon_log_path}", "apps": []}

    text = paths.daemon_log_path.read_text(encoding="utf-8", errors="replace")
    matches = list(re.finditer(r"Installed apps on ([0-9A-Za-z-]+): (\d+) \[(.*)\]", text))
    if not matches:
        return {
            "message": "No installed app dump found in current sideloadlydaemon.log (daemon log may have just rotated).",
            "apps": [],
        }

    last_match = matches[-1]
    device_udid = last_match.group(1)
    raw_list = last_match.group(3)

    entries = re.findall(r"\{([^{}]+)\}", raw_list)
    apps = []
    for entry in entries:
        parts = entry.strip().split(" ")
        if len(parts) >= 4:
            bundle_id = parts[0]
            container_path = parts[-1] if parts[-1].startswith("/") else ""
            apps.append(
                {
                    "bundle_id": bundle_id,
                    "raw_info": entry.strip(),
                    "container_path": container_path,
                }
            )

    if search_query:
        q = search_query.lower()
        apps = [a for a in apps if q in a["raw_info"].lower()]

    return {
        "device_udid": device_udid,
        "total_matched": len(apps),
        "apps": apps,
    }


@mcp.tool()
def get_daemon_logs(tail_lines: int = 80) -> dict[str, Any]:
    """Read the last `tail_lines` lines from `sideloadlydaemon.log`."""
    paths = get_paths()
    if not paths.daemon_log_path.exists():
        return {"exists": False, "path": str(paths.daemon_log_path), "lines": []}

    lines = paths.daemon_log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return {
        "exists": True,
        "path": str(paths.daemon_log_path),
        "total_lines": len(lines),
        "tail": lines[-tail_lines:] if tail_lines > 0 else lines,
    }


@mcp.tool()
def control_sideloadly(action: str = "status", ipa_path: Optional[str] = None) -> dict[str, Any]:
    """Control Sideloadly GUI and Daemon processes.
    Supported `action` values:
    - `status`: Check running PIDs and port 28811 IPC status
    - `raise`: Bring the Sideloadly GUI window to the foreground (or open an IPA in the GUI if `ipa_path` is provided)
    - `start_silent`: Start `sideloadly.exe --silent ENQUEUE` in the background
    - `start_gui`: Start `sideloadly.exe` normally in GUI mode
    - `start_daemon`: Start `sideloadlydaemon.exe`
    - `restart_gui`: Request Sideloadly GUI to restart via `http://127.0.0.1:28811/restart`
    """
    plat_err = _check_platform_error()
    if plat_err:
        return plat_err

    paths = get_paths()
    action = action.lower().strip()

    if action == "status":
        return {
            "processes": get_running_processes(),
            "ipc_listening": check_ipc_alive(),
        }

    if action == "raise":
        proc_info = ensure_sideloadly_running(mode="gui")
        params = {}
        if ipa_path:
            params["fn"] = str(Path(ipa_path).resolve())
        try:
            resp = httpx.get(f"{IPC_BASE_URL}/raise", params=params, timeout=5.0)
            return {
                "action": "raise",
                "status_code": resp.status_code,
                "response": resp.text.strip(),
                "sideloadly_process": proc_info,
            }
        except Exception as e:
            return {"action": "raise", "error": str(e), "sideloadly_process": proc_info}

    if action == "start_gui":
        return ensure_sideloadly_running(mode="gui")

    if action == "start_silent":
        return ensure_sideloadly_running(mode="enqueue")

    if action == "start_daemon":
        procs = get_running_processes()
        if procs["sideloadlydaemon.exe"]:
            return {"running": True, "already_running": True, "pids": procs["sideloadlydaemon.exe"]}
        if not paths.daemon_exe_path.exists():
            return {"running": False, "error": f"sideloadlydaemon.exe not found at {paths.daemon_exe_path}"}
        subprocess.Popen(
            [str(paths.daemon_exe_path)],
            cwd=str(paths.local_dir),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        time.sleep(1.0)
        return {"running": True, "pids": get_running_processes()["sideloadlydaemon.exe"]}

    if action == "restart_gui":
        try:
            resp = httpx.get(
                f"{IPC_BASE_URL}/restart",
                headers={"X-Sideloadly": "1"},
                timeout=3.0,
            )
            return {"action": "restart_gui", "status_code": resp.status_code, "response": resp.text.strip()}
        except Exception as e:
            return {"action": "restart_gui", "message": f"Restart triggered ({e})"}

    raise ValueError(
        f"Unsupported action: {action}. Use status, raise, start_gui, start_silent, start_daemon, or restart_gui."
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

"""Sideloadly Windows MCP Server (v0.3.0).

Provides full programmatic control over Sideloadly on Windows plus a Zero-Mac GitHub Actions iOS Cloud Build pipeline:
- Auto-discovers Sideloadly installation (env var, standard paths, Windows Registry, running process),
  SQLite database, MD5 IPA cache, and roaming session directories
- Reads and manages iOS/iPadOS devices, installations, Apple ID App ID quotas, and options
- Enqueues app refreshes and new/updated IPA installations via Sideloadly's SQLite DB + localhost:28811 IPC API
  with cold-start retries, SQLite lock handling, and accurate completion/error tracking
- Parses IPA Info.plist metadata automatically
- Manages Sideloadly GUI / Daemon processes, stuck queues, and daemon logs
- Zero-Mac iOS Cloud Build & Sideload Pipeline: detects Flutter, Swift (Xcode/XcodeGen), and React Native projects
  on Windows, loads GITHUB_PAT from .env, syncs to a private GitHub repository, compiles an unsigned .ipa on
  GitHub Actions macos-latest, downloads the artifact, and installs it to the connected iPhone via Sideloadly.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import io
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
        "cloud_build_optional_addon": {
            "optional": True,
            "git_installed": shutil.which("git") is not None,
            "github_pat_configured": _resolve_github_pat_info()[0] is not None,
            "github_pat_source": _resolve_github_pat_info()[1],
            "note": "Only needed if compiling source code (Flutter/Swift/React Native) via GitHub Actions. Not required for normal .ipa sideloading or auto-refresh.",
        },
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


# ============================================================================
# Zero-Mac GitHub Actions iOS Cloud Build & Sideload Pipeline (v0.3.0)
# Supports: Flutter, Swift (Xcode / XcodeGen), React Native (Bare & Expo)
# Auth: Reads GITHUB_PAT (or GITHUB_TOKEN / GH_TOKEN) from .env
# ============================================================================

GITHUB_API_BASE = "https://api.github.com"
WORKFLOW_REL_PATH = Path(".github") / "workflows" / "sideloadly-ios-build.yml"
PAT_ENV_KEYS = ("GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN")

FLUTTER_WORKFLOW_YAML = """name: Sideloadly iOS Cloud Build (Flutter)

on:
  push:
    branches: [ main, master, sideloadly-build ]
  workflow_dispatch:

jobs:
  build-ios-ipa:
    runs-on: macos-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Flutter
        uses: subosito/flutter-action@v2
        with:
          channel: stable
          cache: true

      - name: Ensure iOS runner exists and fetch dependencies
        run: |
          if [ ! -d "ios" ]; then
            flutter create --platforms=ios .
          fi
          flutter pub get

      - name: Build unsigned iOS release bundle
        run: |
          flutter build ios --release --no-codesign

      - name: Package Payload into unsigned .ipa
        run: |
          rm -rf Payload unsigned.ipa
          mkdir -p Payload
          APP_PATH=$(find build/ios/iphoneos -maxdepth 1 -name "*.app" | head -n 1)
          if [ -z "$APP_PATH" ]; then
            echo "::error::No .app bundle found in build/ios/iphoneos"
            exit 1
          fi
          cp -R "$APP_PATH" Payload/
          zip -qry unsigned.ipa Payload

      - name: Upload unsigned IPA artifact
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-ipa
          path: unsigned.ipa
          if-no-files-found: error
"""

SWIFT_WORKFLOW_YAML = """name: Sideloadly iOS Cloud Build (Swift)

on:
  push:
    branches: [ main, master, sideloadly-build ]
  workflow_dispatch:

jobs:
  build-ios-ipa:
    runs-on: macos-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Generate Xcode project via XcodeGen if project.yml exists
        run: |
          if [ -f "project.yml" ]; then
            if ! command -v xcodegen >/dev/null 2>&1; then
              brew install xcodegen
            fi
            xcodegen generate
          fi

      - name: Build unsigned iOS app with xcodebuild
        run: |
          WORKSPACE=$(find . -maxdepth 2 -name "*.xcworkspace" ! -path "*.xcodeproj/*" | head -n 1)
          PROJECT=$(find . -maxdepth 2 -name "*.xcodeproj" | head -n 1)
          if [ -n "$WORKSPACE" ]; then
            SCHEME=$(xcodebuild -workspace "$WORKSPACE" -list -json | python3 -c "import sys,json; d=json.load(sys.stdin); s=[x for x in d.get('workspace',{}).get('schemes',[]) if not x.startswith('Pods')]; print(s[0] if s else '')")
            xcodebuild -workspace "$WORKSPACE" -scheme "$SCHEME" -configuration Release -sdk iphoneos -derivedDataPath build \\
              CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" AD_HOC_CODE_SIGNING_ALLOWED=YES build
          elif [ -n "$PROJECT" ]; then
            SCHEME=$(xcodebuild -project "$PROJECT" -list -json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('project',{}).get('schemes',[''])[0])")
            xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration Release -sdk iphoneos -derivedDataPath build \\
              CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" AD_HOC_CODE_SIGNING_ALLOWED=YES build
          else
            echo "::error::No .xcworkspace, .xcodeproj, or project.yml found in repository."
            exit 1
          fi

      - name: Package Payload into unsigned .ipa
        run: |
          rm -rf Payload unsigned.ipa
          mkdir -p Payload
          APP_PATH=$(find build/Build/Products/Release-iphoneos -maxdepth 1 -name "*.app" | head -n 1)
          if [ -z "$APP_PATH" ]; then
            echo "::error::No .app bundle found in build/Build/Products/Release-iphoneos"
            exit 1
          fi
          cp -R "$APP_PATH" Payload/
          zip -qry unsigned.ipa Payload

      - name: Upload unsigned IPA artifact
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-ipa
          path: unsigned.ipa
          if-no-files-found: error
"""

REACT_NATIVE_WORKFLOW_YAML = """name: Sideloadly iOS Cloud Build (React Native)

on:
  push:
    branches: [ main, master, sideloadly-build ]
  workflow_dispatch:

jobs:
  build-ios-ipa:
    runs-on: macos-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: 20

      - name: Install JS dependencies and CocoaPods
        run: |
          if [ -f "yarn.lock" ]; then
            yarn install
          elif [ -f "pnpm-lock.yaml" ]; then
            npx pnpm install
          else
            npm install --legacy-peer-deps
          fi
          if [ ! -d "ios" ]; then
            npx expo prebuild --platform ios --no-install
          fi
          cd ios
          pod install --repo-update

      - name: Build unsigned iOS app with xcodebuild
        run: |
          cd ios
          WORKSPACE=$(find . -maxdepth 1 -name "*.xcworkspace" | head -n 1)
          PROJECT=$(find . -maxdepth 1 -name "*.xcodeproj" | head -n 1)
          if [ -n "$WORKSPACE" ]; then
            SCHEME=$(xcodebuild -workspace "$WORKSPACE" -list -json | python3 -c "import sys,json; d=json.load(sys.stdin); s=[x for x in d.get('workspace',{}).get('schemes',[]) if not x.startswith('Pods')]; print(s[0] if s else '')")
            xcodebuild -workspace "$WORKSPACE" -scheme "$SCHEME" -configuration Release -sdk iphoneos -derivedDataPath ../build \\
              CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" AD_HOC_CODE_SIGNING_ALLOWED=YES build
          elif [ -n "$PROJECT" ]; then
            SCHEME=$(xcodebuild -project "$PROJECT" -list -json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('project',{}).get('schemes',[''])[0])")
            xcodebuild -project "$PROJECT" -scheme "$SCHEME" -configuration Release -sdk iphoneos -derivedDataPath ../build \\
              CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO CODE_SIGN_IDENTITY="" AD_HOC_CODE_SIGNING_ALLOWED=YES build
          else
            echo "::error::No Xcode workspace or project found inside ios/ directory."
            exit 1
          fi

      - name: Package Payload into unsigned .ipa
        run: |
          rm -rf Payload unsigned.ipa
          mkdir -p Payload
          APP_PATH=$(find build/Build/Products/Release-iphoneos -maxdepth 1 -name "*.app" | head -n 1)
          if [ -z "$APP_PATH" ]; then
            echo "::error::No .app bundle found in build/Build/Products/Release-iphoneos"
            exit 1
          fi
          cp -R "$APP_PATH" Payload/
          zip -qry unsigned.ipa Payload

      - name: Upload unsigned IPA artifact
        uses: actions/upload-artifact@v4
        with:
          name: unsigned-ipa
          path: unsigned.ipa
          if-no-files-found: error
"""

WORKFLOW_TEMPLATES: dict[str, str] = {
    "flutter": FLUTTER_WORKFLOW_YAML,
    "swift": SWIFT_WORKFLOW_YAML,
    "react_native": REACT_NATIVE_WORKFLOW_YAML,
}


def _parse_dotenv_file(env_path: Path) -> dict[str, str]:
    """Parse a .env file into a dictionary without external dependencies."""
    values: dict[str, str] = {}
    if not env_path.exists() or not env_path.is_file():
        return values
    try:
        for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
                val = val[1:-1]
            elif " #" in val:
                val = val.split(" #", 1)[0].strip()
            if key:
                values[key] = val
    except Exception:
        pass
    return values


def _resolve_github_pat_info(
    project_dir: Optional[Path] = None,
    env_file: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Locate the GitHub PAT token from .env files or process environment variables.
    Returns (token, source_description). Never exposes the token value in source_description.
    """
    candidate_files: list[Path] = []
    if env_file:
        candidate_files.append(Path(env_file).resolve())
    if project_dir:
        candidate_files.append(Path(project_dir).resolve() / ".env")
    candidate_files.append(Path.cwd() / ".env")
    try:
        candidate_files.append(get_paths().local_dir / ".env")
    except Exception:
        pass
    pkg_root_env = Path(__file__).resolve().parents[2] / ".env"
    candidate_files.append(pkg_root_env)

    seen: set[Path] = set()
    for fpath in candidate_files:
        if fpath in seen:
            continue
        seen.add(fpath)
        parsed = _parse_dotenv_file(fpath)
        for k in PAT_ENV_KEYS:
            tok = parsed.get(k, "").strip()
            if tok:
                return tok, f"{fpath} ({k})"

    for k in PAT_ENV_KEYS:
        tok = os.environ.get(k, "").strip()
        if tok:
            return tok, f"environment ({k})"

    return None, None


def _require_github_pat(project_dir: Optional[Path] = None, env_file: Optional[str] = None) -> tuple[str, str]:
    tok, src = _resolve_github_pat_info(project_dir=project_dir, env_file=env_file)
    if not tok or not src:
        raise ValueError(
            "GitHub Personal Access Token (PAT) not found. "
            "Define GITHUB_PAT=ghp_... inside a .env file in your project directory (or Sideloadly directory) "
            "with 'repo' and 'workflow' permissions."
        )
    return tok, src


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sideloadly-mcp/0.3.0",
    }


def detect_ios_project_type(project_dir: Path) -> str:
    """Detect whether a directory is a Flutter, React Native, or Swift iOS project."""
    if not project_dir.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    if (project_dir / "pubspec.yaml").exists():
        return "flutter"

    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
            deps = {
                **(data.get("dependencies") or {}),
                **(data.get("devDependencies") or {}),
            }
            if "react-native" in deps or "expo" in deps:
                return "react_native"
        except Exception:
            pass

    if (
        (project_dir / "project.yml").exists()
        or (project_dir / "Package.swift").exists()
        or list(project_dir.glob("*.xcodeproj"))
        or list(project_dir.glob("*.xcworkspace"))
        or list(project_dir.rglob("*.swift"))
    ):
        return "swift"

    raise ValueError(
        f"Could not auto-detect project type in {project_dir}. "
        "Supported types: 'flutter' (pubspec.yaml), 'react_native' (package.json with react-native/expo), "
        "or 'swift' (*.xcodeproj, *.xcworkspace, project.yml, or *.swift)."
    )


def _ensure_safe_gitignore(project_dir: Path, project_type: str) -> Path:
    """Ensure .gitignore exists and excludes .env files and heavy build folders so secrets/junk are never pushed."""
    gitignore_path = project_dir / ".gitignore"
    existing_lines: list[str] = []
    if gitignore_path.exists():
        existing_lines = gitignore_path.read_text(encoding="utf-8", errors="replace").splitlines()

    existing_set = {line.strip() for line in existing_lines}
    must_ignore = [".env", ".env.*", "!.env.example", "unsigned.ipa", "Payload/"]
    if project_type == "flutter":
        must_ignore.extend(["/build/", ".dart_tool/", ".flutter-plugins", ".flutter-plugins-dependencies"])
    elif project_type == "react_native":
        must_ignore.extend(["node_modules/", "ios/Pods/", "build/", ".expo/"])
    elif project_type == "swift":
        must_ignore.extend(["build/", "DerivedData/", ".build/", "Pods/"])

    added = [entry for entry in must_ignore if entry not in existing_set]
    if added:
        content = "\n".join(existing_lines).rstrip()
        if content:
            content += "\n"
        content += "# Added by sideloadly-mcp (protect .env secrets & build artifacts)\n"
        content += "\n".join(added) + "\n"
        gitignore_path.write_text(content, encoding="utf-8")
    return gitignore_path


def _scaffold_swift_xcodegen_if_needed(
    project_dir: Path,
    app_name: Optional[str] = None,
    bundle_id: Optional[str] = None,
) -> Optional[Path]:
    """If a Swift project on Windows has no .xcodeproj/.xcworkspace/project.yml, scaffold a clean project.yml for XcodeGen."""
    if (
        (project_dir / "project.yml").exists()
        or list(project_dir.glob("*.xcodeproj"))
        or list(project_dir.glob("*.xcworkspace"))
    ):
        return None

    clean_name = re.sub(r"[^A-Za-z0-9_]", "", app_name or project_dir.name) or "SideloadlySwiftApp"
    clean_bundle = bundle_id or f"com.sideloadly.{clean_name.lower()}"

    swift_files = list(project_dir.rglob("*.swift"))
    if not swift_files:
        src_dir = project_dir / "Sources"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "App.swift").write_text(
            "import SwiftUI\n\n"
            "@main\n"
            f"struct {clean_name}App: App {{\n"
            "    var body: some Scene {\n"
            "        WindowGroup {\n"
            f'            Text("Hello from {clean_name} (Built via Sideloadly MCP)")\n'
            "                .padding()\n"
            "        }\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        sources_path = "Sources"
    else:
        first_rel = swift_files[0].relative_to(project_dir)
        sources_path = first_rel.parts[0] if len(first_rel.parts) > 1 else "."

    project_yml = project_dir / "project.yml"
    project_yml.write_text(
        f"name: {clean_name}\n"
        "options:\n"
        f"  bundleIdPrefix: {clean_bundle.rsplit('.', 1)[0]}\n"
        "  deploymentTarget:\n"
        '    iOS: "16.0"\n'
        "targets:\n"
        f"  {clean_name}:\n"
        "    type: application\n"
        "    platform: iOS\n"
        "    sources:\n"
        f"      - {sources_path}\n"
        "    settings:\n"
        "      base:\n"
        f"        PRODUCT_BUNDLE_IDENTIFIER: {clean_bundle}\n"
        "        GENERATE_INFOPLIST_FILE: YES\n"
        f'        INFOPLIST_KEY_CFBundleDisplayName: "{app_name or clean_name}"\n'
        '        INFOPLIST_KEY_UILaunchScreen_Generation: YES\n'
        '        MARKETING_VERSION: "1.0.0"\n'
        '        CURRENT_PROJECT_VERSION: "1"\n',
        encoding="utf-8",
    )
    return project_yml


def _run_git(project_dir: Path, args: list[str], pat_token: Optional[str] = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run a git command inside project_dir. If pat_token is given, injects ephemeral HTTP Basic auth without writing token to disk."""
    cmd = ["git"]
    if pat_token:
        b64 = base64.b64encode(f"x-access-token:{pat_token}".encode("utf-8")).decode("ascii")
        cmd.extend(["-c", f"http.extraHeader=Authorization: Basic {b64}"])
    cmd.extend(args)

    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        cmd,
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _redact_token(text: str, token: Optional[str]) -> str:
    if not text or not token:
        return text
    b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return text.replace(token, "***REDACTED_PAT***").replace(b64, "***REDACTED_AUTH***")


@mcp.tool()
def prepare_ios_cloud_build(
    project_dir: str,
    project_type: str = "auto",
    app_name: Optional[str] = None,
    bundle_id: Optional[str] = None,
    overwrite_workflow: bool = False,
    create_env_example: bool = False,
) -> dict[str, Any]:
    """[Optional Cloud Build Add-on] Prepare a Windows project (Flutter, Swift, or React Native) for Zero-Mac GitHub Actions iOS cloud compilation.

    - Auto-detects `flutter`, `swift`, or `react_native` (or uses explicit `project_type`).
    - Generates `.github/workflows/sideloadly-ios-build.yml` configured to build an unsigned `.ipa` on `macos-latest`.
    - For Swift projects created on Windows without an `.xcodeproj`, automatically scaffolds a `project.yml` for XcodeGen.
    - Updates `.gitignore` so `.env` secrets and heavy build directories are never pushed to GitHub.
    """
    pdir = Path(project_dir).resolve()
    if not pdir.exists():
        raise FileNotFoundError(f"Project directory does not exist: {pdir}")

    ptype = project_type.lower().strip()
    if ptype in ("react-native", "reactnative", "rn", "expo"):
        ptype = "react_native"
    elif ptype == "auto":
        ptype = detect_ios_project_type(pdir)

    if ptype not in WORKFLOW_TEMPLATES:
        raise ValueError(f"Unsupported project_type '{project_type}'. Choose from: auto, flutter, swift, react_native.")

    workflow_path = pdir / WORKFLOW_REL_PATH
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_written = False
    if not workflow_path.exists() or overwrite_workflow:
        workflow_path.write_text(WORKFLOW_TEMPLATES[ptype], encoding="utf-8")
        workflow_written = True

    scaffolded_project_yml: Optional[Path] = None
    if ptype == "swift":
        scaffolded_project_yml = _scaffold_swift_xcodegen_if_needed(pdir, app_name=app_name, bundle_id=bundle_id)

    gitignore_path = _ensure_safe_gitignore(pdir, ptype)

    env_example = pdir / ".env.example"
    if create_env_example and not (pdir / ".env").exists() and not env_example.exists():
        env_example.write_text(
            "# GitHub Personal Access Token (PAT) with 'repo' and 'workflow' scopes\n"
            "GITHUB_PAT=ghp_your_personal_access_token_here\n",
            encoding="utf-8",
        )

    pat_tok, pat_src = _resolve_github_pat_info(project_dir=pdir)
    return {
        "project_dir": str(pdir),
        "detected_project_type": ptype,
        "workflow_path": str(workflow_path),
        "workflow_written": workflow_written,
        "scaffolded_xcodegen_project_yml": str(scaffolded_project_yml) if scaffolded_project_yml else None,
        "gitignore_path": str(gitignore_path),
        "github_pat_configured": pat_tok is not None,
        "github_pat_source": pat_src,
    }


@mcp.tool()
def trigger_ios_cloud_build(
    project_dir: str,
    repo_name: Optional[str] = None,
    private: bool = True,
    branch: str = "main",
    project_type: str = "auto",
    commit_message: str = "chore: trigger iOS cloud build via sideloadly-mcp",
    env_file: Optional[str] = None,
) -> dict[str, Any]:
    """Push a local Flutter, Swift, or React Native project to a private GitHub repository using GITHUB_PAT from .env
    and trigger the macOS iOS `.ipa` GitHub Actions build.

    - Reads `GITHUB_PAT` (or `GITHUB_TOKEN` / `GH_TOKEN`) from `.env` (never hardcodes or stores tokens in `.git/config`).
    - Automatically creates the private GitHub repository if it does not exist yet.
    - Commits and pushes the project and workflow file, then locates or dispatches the GitHub Actions run.
    """
    pdir = Path(project_dir).resolve()
    token, pat_source = _require_github_pat(project_dir=pdir, env_file=env_file)
    prep = prepare_ios_cloud_build(str(pdir), project_type=project_type)

    headers = _github_headers(token)

    # 1. Verify PAT and resolve GitHub owner/username
    user_resp = httpx.get(f"{GITHUB_API_BASE}/user", headers=headers, timeout=15.0)
    if user_resp.status_code != 200:
        raise RuntimeError(
            f"GitHub authentication failed (HTTP {user_resp.status_code}): {user_resp.text}. "
            "Check your GITHUB_PAT in .env and ensure it has 'repo' and 'workflow' permissions."
        )
    owner = user_resp.json()["login"]

    # 2. Resolve target repo_name (supports 'owner/repo' or 'repo')
    if repo_name and "/" in repo_name:
        owner, clean_repo = repo_name.split("/", 1)
    else:
        clean_repo = re.sub(r"[^A-Za-z0-9._-]", "-", (repo_name or f"{pdir.name}-ios-build").strip()).strip("-")
        if not clean_repo:
            clean_repo = "sideloadly-ios-cloud-build"
    repo_full_name = f"{owner}/{clean_repo}"

    # 3. Check if repo exists; if not, create it (private by default)
    repo_check = httpx.get(f"{GITHUB_API_BASE}/repos/{repo_full_name}", headers=headers, timeout=15.0)
    repo_created = False
    if repo_check.status_code == 404:
        create_resp = httpx.post(
            f"{GITHUB_API_BASE}/user/repos",
            headers=headers,
            json={
                "name": clean_repo,
                "private": private,
                "description": f"Automated iOS cloud build repository managed by sideloadly-mcp ({prep['detected_project_type']})",
                "auto_init": False,
            },
            timeout=20.0,
        )
        if create_resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to create GitHub repository '{repo_full_name}' (HTTP {create_resp.status_code}): {create_resp.text}"
            )
        repo_created = True
    elif repo_check.status_code != 200:
        raise RuntimeError(
            f"Failed checking GitHub repository '{repo_full_name}' (HTTP {repo_check.status_code}): {repo_check.text}"
        )

    # 4. Initialize local Git repo, ensure .env is untracked, commit, and push with ephemeral HTTP header
    if not (pdir / ".git").exists():
        init_res = _run_git(pdir, ["init", "-b", branch])
        if init_res.returncode != 0:
            _run_git(pdir, ["init"])
            _run_git(pdir, ["checkout", "-B", branch])

    # Ensure git user.name and user.email are configured locally so commit never fails on fresh PCs
    if not _run_git(pdir, ["config", "user.name"]).stdout.strip():
        _run_git(pdir, ["config", "user.name", owner])
    if not _run_git(pdir, ["config", "user.email"]).stdout.strip():
        _run_git(pdir, ["config", "user.email", f"{owner}@users.noreply.github.com"])

    # Make sure .env is never staged even if someone previously added it
    _run_git(pdir, ["rm", "--cached", "--ignore-unmatch", ".env"])
    _run_git(pdir, ["add", "-A"])

    status_res = _run_git(pdir, ["status", "--porcelain"])
    if status_res.stdout.strip():
        commit_res = _run_git(pdir, ["commit", "-m", commit_message])
        if commit_res.returncode != 0:
            raise RuntimeError(f"Git commit failed: {_redact_token(commit_res.stderr or commit_res.stdout, token)}")

    # Ensure branch exists / matches
    _run_git(pdir, ["branch", "-M", branch])
    head_sha = _run_git(pdir, ["rev-parse", "HEAD"]).stdout.strip()

    clean_remote_url = f"https://github.com/{repo_full_name}.git"
    remotes = _run_git(pdir, ["remote"]).stdout.splitlines()
    if "sideloadly-cloud" in [r.strip() for r in remotes]:
        _run_git(pdir, ["remote", "set-url", "sideloadly-cloud", clean_remote_url])
    else:
        _run_git(pdir, ["remote", "add", "sideloadly-cloud", clean_remote_url])

    push_res = _run_git(pdir, ["push", "-u", "--force", "sideloadly-cloud", f"HEAD:refs/heads/{branch}"], pat_token=token)
    if push_res.returncode != 0:
        err_msg = _redact_token(push_res.stderr or push_res.stdout, token)
        raise RuntimeError(
            f"Git push to {clean_remote_url} failed: {err_msg}. "
            "Ensure your GITHUB_PAT has 'repo' and 'workflow' scopes enabled."
        )

    # 5. Locate the triggered workflow run for head_sha (or dispatch workflow_dispatch if none started automatically)
    run_id: Optional[int] = None
    run_url: Optional[str] = None
    run_status: Optional[str] = None

    for attempt in range(1, 6):
        time.sleep(2.0)
        runs_resp = httpx.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/runs",
            headers=headers,
            params={"head_sha": head_sha, "per_page": 5},
            timeout=15.0,
        )
        if runs_resp.status_code == 200:
            runs = runs_resp.json().get("workflow_runs") or []
            if runs:
                run_id = runs[0]["id"]
                run_url = runs[0].get("html_url")
                run_status = runs[0].get("status")
                break
        if attempt == 2:
            # Explicitly trigger workflow_dispatch in case push didn't fire (e.g. identical commit SHA)
            httpx.post(
                f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/workflows/sideloadly-ios-build.yml/dispatches",
                headers=headers,
                json={"ref": branch},
                timeout=15.0,
            )

    return {
        "repo_full_name": repo_full_name,
        "repo_url": f"https://github.com/{repo_full_name}",
        "repo_created": repo_created,
        "private": private,
        "branch": branch,
        "head_sha": head_sha,
        "project_type": prep["detected_project_type"],
        "github_pat_source": pat_source,
        "run_id": run_id,
        "run_url": run_url,
        "run_status": run_status or "queued",
    }


def _extract_failure_log_tail(raw_log: str, max_lines: int = 80) -> list[str]:
    """Extract the most relevant compiler/Xcode/Flutter error lines from a GitHub Actions job log."""
    lines = raw_log.splitlines()
    error_indices = [
        i
        for i, line in enumerate(lines)
        if any(
            kw in line.lower()
            for kw in ("error:", "##[error]", "fatal error:", "build failed", "exception:", "pod install")
        )
    ]
    if error_indices:
        first_err = max(0, error_indices[0] - 5)
        last_err = min(len(lines), error_indices[-1] + 25)
        snippet = lines[first_err:last_err]
        return snippet[-max_lines:]
    return lines[-max_lines:]


@mcp.tool()
def get_ios_cloud_build_status(
    repo_full_name: str,
    run_id: Optional[int] = None,
    include_error_logs: bool = True,
    project_dir: Optional[str] = None,
    env_file: Optional[str] = None,
) -> dict[str, Any]:
    """Check the live status of a GitHub Actions iOS cloud build, list produced `.ipa` artifacts,
    and automatically extract Xcode/Flutter compiler error logs if the build failed.
    """
    pdir = Path(project_dir).resolve() if project_dir else None
    token, pat_source = _require_github_pat(project_dir=pdir, env_file=env_file)
    headers = _github_headers(token)

    if run_id is None:
        runs_resp = httpx.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/runs",
            headers=headers,
            params={"per_page": 1},
            timeout=15.0,
        )
        if runs_resp.status_code != 200:
            raise RuntimeError(f"Failed fetching workflow runs (HTTP {runs_resp.status_code}): {runs_resp.text}")
        runs = runs_resp.json().get("workflow_runs") or []
        if not runs:
            return {"found": False, "repo_full_name": repo_full_name, "error": "No workflow runs found."}
        run_data = runs[0]
        run_id = run_data["id"]
    else:
        run_resp = httpx.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/runs/{run_id}",
            headers=headers,
            timeout=15.0,
        )
        if run_resp.status_code != 200:
            raise RuntimeError(f"Failed fetching workflow run {run_id} (HTTP {run_resp.status_code}): {run_resp.text}")
        run_data = run_resp.json()

    status = run_data.get("status")
    conclusion = run_data.get("conclusion")

    jobs_Summary: list[dict[str, Any]] = []
    error_logs: list[str] = []
    jobs_resp = httpx.get(
        f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/runs/{run_id}/jobs",
        headers=headers,
        timeout=15.0,
    )
    if jobs_resp.status_code == 200:
        for job in jobs_resp.json().get("jobs") or []:
            steps = [
                {
                    "name": s.get("name"),
                    "status": s.get("status"),
                    "conclusion": s.get("conclusion"),
                }
                for s in (job.get("steps") or [])
            ]
            jobs_Summary.append(
                {
                    "job_id": job.get("id"),
                    "name": job.get("name"),
                    "status": job.get("status"),
                    "conclusion": job.get("conclusion"),
                    "steps": steps,
                }
            )
            if include_error_logs and job.get("conclusion") == "failure":
                log_resp = httpx.get(
                    f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/jobs/{job['id']}/logs",
                    headers=headers,
                    follow_redirects=True,
                    timeout=30.0,
                )
                if log_resp.status_code == 200:
                    error_logs.extend(_extract_failure_log_tail(log_resp.text))

    artifacts: list[dict[str, Any]] = []
    if status == "completed":
        art_resp = httpx.get(
            f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/runs/{run_id}/artifacts",
            headers=headers,
            timeout=15.0,
        )
        if art_resp.status_code == 200:
            for a in art_resp.json().get("artifacts") or []:
                artifacts.append(
                    {
                        "id": a.get("id"),
                        "name": a.get("name"),
                        "size_in_bytes": a.get("size_in_bytes"),
                        "expired": a.get("expired"),
                        "archive_download_url": a.get("archive_download_url"),
                    }
                )

    return {
        "found": True,
        "repo_full_name": repo_full_name,
        "run_id": run_id,
        "run_url": run_data.get("html_url"),
        "status": status,
        "conclusion": conclusion,
        "head_sha": run_data.get("head_sha"),
        "created_at": run_data.get("created_at"),
        "updated_at": run_data.get("updated_at"),
        "jobs": jobs_Summary,
        "artifacts": artifacts,
        "error_logs": error_logs,
        "github_pat_source": pat_source,
    }


@mcp.tool()
def download_cloud_build_ipa(
    repo_full_name: str,
    run_id: Optional[int] = None,
    artifact_name: str = "unsigned-ipa",
    output_dir: Optional[str] = None,
    install_via_sideloadly: bool = True,
    device_udid: Optional[str] = None,
    apple_id: Optional[str] = None,
    custom_bundle_id: Optional[str] = None,
    custom_app_name: Optional[str] = None,
    wait_for_install: bool = True,
    project_dir: Optional[str] = None,
    env_file: Optional[str] = None,
) -> dict[str, Any]:
    """Download the compiled `.ipa` artifact from a completed GitHub Actions iOS cloud build,
    extract it to disk, and optionally sign & install it onto the connected iOS device via Sideloadly.
    """
    pdir = Path(project_dir).resolve() if project_dir else None
    token, _ = _require_github_pat(project_dir=pdir, env_file=env_file)
    headers = _github_headers(token)

    status_info = get_ios_cloud_build_status(
        repo_full_name=repo_full_name,
        run_id=run_id,
        include_error_logs=True,
        project_dir=project_dir,
        env_file=env_file,
    )
    if not status_info.get("found"):
        return status_info

    resolved_run_id = status_info["run_id"]
    if status_info.get("status") != "completed" or status_info.get("conclusion") != "success":
        return {
            "downloaded": False,
            "error": f"Workflow run {resolved_run_id} is not a completed successful build "
            f"(status={status_info.get('status')}, conclusion={status_info.get('conclusion')}).",
            "build_status": status_info,
        }

    artifacts = status_info.get("artifacts") or []
    target_art = next((a for a in artifacts if a.get("name") == artifact_name), None) or (
        artifacts[0] if artifacts else None
    )
    if not target_art:
        raise LookupError(f"No artifact named '{artifact_name}' found on run {resolved_run_id}.")

    dl_url = target_art.get("archive_download_url") or (
        f"{GITHUB_API_BASE}/repos/{repo_full_name}/actions/artifacts/{target_art['id']}/zip"
    )
    dl_resp = httpx.get(dl_url, headers=headers, follow_redirects=True, timeout=120.0)
    if dl_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to download artifact zip (HTTP {dl_resp.status_code}): {dl_resp.text}"
        )

    dest_dir = Path(output_dir).resolve() if output_dir else (get_paths().local_dir / "cloud-builds")
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_repo_slug = repo_full_name.replace("/", "_")

    extracted_ipa_path: Optional[Path] = None
    with zipfile.ZipFile(io.BytesIO(dl_resp.content), "r") as zf:
        ipa_entries = [n for n in zf.namelist() if n.lower().endswith(".ipa")]
        if not ipa_entries:
            raise ValueError(f"Artifact zip from run {resolved_run_id} did not contain any .ipa file.")
        ipa_entry = ipa_entries[0]
        out_path = dest_dir / f"{safe_repo_slug}_{resolved_run_id}.ipa"
        with zf.open(ipa_entry) as src_f, out_path.open("wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f)
        extracted_ipa_path = out_path

    ipa_meta = inspect_ipa_metadata(extracted_ipa_path)
    result: dict[str, Any] = {
        "downloaded": True,
        "repo_full_name": repo_full_name,
        "run_id": resolved_run_id,
        "artifact_id": target_art["id"],
        "ipa_path": str(extracted_ipa_path),
        "ipa_metadata": ipa_meta,
    }

    if install_via_sideloadly:
        install_res = install_or_update_ipa(
            ipa_path=str(extracted_ipa_path),
            device_udid=device_udid,
            apple_id=apple_id,
            custom_bundle_id=custom_bundle_id,
            custom_app_name=custom_app_name,
            wait_for_completion=wait_for_install,
        )
        result["sideload_result"] = install_res

    return result


@mcp.tool()
def cloud_build_and_sideload(
    project_dir: str,
    repo_name: Optional[str] = None,
    private: bool = True,
    branch: str = "main",
    project_type: str = "auto",
    device_udid: Optional[str] = None,
    apple_id: Optional[str] = None,
    custom_bundle_id: Optional[str] = None,
    custom_app_name: Optional[str] = None,
    wait_for_build: bool = True,
    build_timeout_seconds: int = 1200,
    poll_interval_seconds: int = 15,
    install_after_build: bool = True,
    wait_for_install: bool = True,
    env_file: Optional[str] = None,
) -> dict[str, Any]:
    """End-to-end Zero-Mac Windows-to-iPhone Pipeline:
    1. Detects project type (`flutter`, `swift`, or `react_native`) and injects the GitHub Actions macOS `.ipa` workflow.
    2. Authenticates via `GITHUB_PAT` from `.env`, creates/updates a private GitHub repository, and pushes the source.
    3. Monitors the `macos-latest` GitHub Actions build until completion (returns compiler error logs if build fails).
    4. Downloads the compiled unsigned `.ipa` artifact to Windows.
    5. Signs and installs the `.ipa` onto the connected iOS device via Sideloadly!
    """
    trigger_res = trigger_ios_cloud_build(
        project_dir=project_dir,
        repo_name=repo_name,
        private=private,
        branch=branch,
        project_type=project_type,
        env_file=env_file,
    )
    if not wait_for_build:
        return {
            "stage": "triggered",
            "completed": False,
            "trigger": trigger_res,
        }

    repo_full_name = trigger_res["repo_full_name"]
    run_id = trigger_res.get("run_id")
    deadline = time.time() + build_timeout_seconds
    last_status: dict[str, Any] = {}

    while time.time() < deadline:
        last_status = get_ios_cloud_build_status(
            repo_full_name=repo_full_name,
            run_id=run_id,
            include_error_logs=True,
            project_dir=project_dir,
            env_file=env_file,
        )
        if last_status.get("found"):
            run_id = last_status.get("run_id")
            if last_status.get("status") == "completed":
                break
        time.sleep(max(1, poll_interval_seconds))

    if last_status.get("status") != "completed":
        return {
            "stage": "build_timeout",
            "completed": False,
            "trigger": trigger_res,
            "build_status": last_status,
        }

    if last_status.get("conclusion") != "success":
        return {
            "stage": "build_failed",
            "completed": True,
            "status": "build_failed",
            "trigger": trigger_res,
            "build_status": last_status,
        }

    dl_res = download_cloud_build_ipa(
        repo_full_name=repo_full_name,
        run_id=run_id,
        install_via_sideloadly=install_after_build,
        device_udid=device_udid,
        apple_id=apple_id,
        custom_bundle_id=custom_bundle_id,
        custom_app_name=custom_app_name,
        wait_for_install=wait_for_install,
        project_dir=project_dir,
        env_file=env_file,
    )
    return {
        "stage": "completed",
        "completed": True,
        "status": dl_res.get("sideload_result", {}).get("status", "downloaded"),
        "trigger": trigger_res,
        "build_status": last_status,
        "download_and_sideload": dl_res,
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

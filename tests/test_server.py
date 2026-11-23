"""Automated test suite for sideloadly-mcp.

Covers edge cases across brand-new PCs, existing installations, cold-start IPC retries,
completion/error tracking, stuck queue recovery, option validation, IPA inspection,
and daemon log parsing. Runs completely isolated in temporary directories.
"""

from __future__ import annotations

import json
import os
import plistlib
import sqlite3
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from sideloadly_mcp import server

SCHEMA_SQL = """
CREATE TABLE devices (
    udid text PRIMARY KEY, name text, is_m1 numeric, is_tv numeric,
    last_seen datetime, last_checked datetime, last_error text, failures_count integer
);
CREATE TABLE stored_files (
    id integer PRIMARY KEY, created_at datetime, updated_at datetime, deleted_at datetime,
    stored_name text UNIQUE, nickname text, ext text, size integer, hash blob, uncached_path text
);
CREATE TABLE installations (
    id integer PRIMARY KEY, created_at datetime, updated_at datetime, deleted_at datetime,
    name text, ipa_id integer NOT NULL, device_udid text, apple_id text,
    anisette_mode integer, info_props JSON, sign_mode integer, drop_plugins numeric,
    alt_entitlements_id integer, last_updated datetime, version text, final_bundle_id text,
    one_off numeric, installation_token text, known_ttl integer, refresh_at_hours integer,
    enqueued_at datetime, enqueue_token text, last_error text, failures_count integer,
    last_failure_at datetime
);
"""


def make_dummy_ipa(path: Path, bundle_id: str = "com.example.demo", name: str = "DemoApp", version: str = "2.1.0") -> Path:
    info = {
        "CFBundleDisplayName": name,
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": "42",
        "MinimumOSVersion": "16.0",
        "CFBundleExecutable": "DemoApp",
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/DemoApp.app/Info.plist", plistlib.dumps(info))
        zf.writestr("Payload/DemoApp.app/PlugIns/Widget.appex/Info.plist", b"")
        zf.writestr("Payload/DemoApp.app/Frameworks/Core.framework/Core", b"")
    return path


class FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class TestSideloadlyMCP(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.local_app = self.root / "Local"
        self.roaming_app = self.root / "Roaming"
        self.sl_dir = self.local_app / "Sideloadly"
        self.roaming_sl = self.roaming_app / "Sideloadly"
        self.sl_dir.mkdir(parents=True)
        self.roaming_sl.mkdir(parents=True)

        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.local_app),
                "APPDATA": str(self.roaming_app),
                "SIDELOADLY_DIR": str(self.sl_dir),
                "SIDELOADLY_ALLOW_NON_WINDOWS": "1",
            },
        )
        self.env_patcher.start()

    def tearDown(self) -> None:
        self.env_patcher.stop()
        self.tmp.cleanup()

    def _init_db(self, with_device: bool = True) -> Path:
        db = self.sl_dir / "installations.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(SCHEMA_SQL)
        if with_device:
            conn.execute(
                "INSERT INTO devices VALUES (?, ?, 0, 0, ?, ?, '', 0)",
                ("00008110-TESTDEVICE", "Test iPhone", server.ZERO_TIME, server.ZERO_TIME),
            )
        conn.commit()
        conn.close()
        return db

    def test_missing_db_returns_graceful_error(self) -> None:
        res_dev = server.list_devices()
        res_ins = server.list_installations()
        res_cancel = server.cancel_queue()
        self.assertIn("error", res_dev)
        self.assertEqual(res_dev["count"], 0)
        self.assertIn("error", res_ins)
        self.assertEqual(res_cancel["cleared_count"], 0)

    def test_doctor_fresh_pc_recommendations(self) -> None:
        doc = server.sideloadly_doctor()
        self.assertFalse(doc["healthy"])
        self.assertGreaterEqual(len(doc["recommendations"]), 3)

        # Populate required artifacts
        (self.sl_dir / "sideloadly.exe").write_bytes(b"MZ")
        (self.sl_dir / "an").mkdir()
        (self.sl_dir / "an" / "CoreADI.dll").write_bytes(b"MZ")
        (self.roaming_sl / "sessions.json").write_text("{}", encoding="utf-8")
        (self.roaming_sl / "cert-dev@example.com.pem").write_text("CERT", encoding="utf-8")
        self._init_db(with_device=True)

        with mock.patch.object(server, "check_ipc_alive", return_value=True):
            doc2 = server.sideloadly_doctor()
        self.assertTrue(doc2["healthy"])
        self.assertEqual(doc2["configured_apple_ids"], ["dev@example.com"])
        self.assertEqual(doc2["registered_devices_count"], 1)

    def test_inspect_ipa_valid_and_invalid(self) -> None:
        ipa = make_dummy_ipa(self.root / "Demo.ipa")
        meta = server.inspect_ipa(str(ipa))
        self.assertEqual(meta["bundle_id"], "com.example.demo")
        self.assertEqual(meta["bundle_name"], "DemoApp")
        self.assertEqual(meta["version"], "2.1.0")
        self.assertEqual(meta["plugins"], ["Widget.appex"])
        self.assertEqual(meta["frameworks"], ["Core.framework"])

        bad = self.root / "bad.ipa"
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("README.txt", "not an ipa")
        with self.assertRaises(ValueError):
            server.inspect_ipa(str(bad))

    def test_install_on_fresh_pc_with_cert_fallback_and_ipc_retry(self) -> None:
        self._init_db(with_device=True)
        (self.roaming_sl / "cert-fresh@example.com.pem").write_text("CERT", encoding="utf-8")
        (self.sl_dir / "option-anisette-mode").write_text("3", encoding="utf-8")
        ipa = make_dummy_ipa(self.root / "FreshApp.ipa", bundle_id="com.fresh.app", name="FreshApp")

        call_count = {"n": 0}

        def fake_get(url: str, params=None, timeout=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return FakeResponse(200, "Task enqueue failOK")
            # Simulate Sideloadly completing the install on second attempt
            conn = server.get_db_connection(readonly=False)
            try:
                conn.execute(
                    "UPDATE installations SET last_updated = ?, enqueued_at = ?, enqueue_token = '' WHERE id = ?",
                    (server.now_go_iso(), server.ZERO_TIME, int(params["id"])),
                )
                conn.commit()
            finally:
                conn.close()
            return FakeResponse(200, "Task enqueuedOK")

        with mock.patch.object(server, "ensure_sideloadly_running", return_value={"running": True, "started_now": True}), \
             mock.patch.object(server.httpx, "get", side_effect=fake_get), \
             mock.patch.object(server.time, "sleep", return_value=None):
            res = server.install_or_update_ipa(str(ipa), wait_for_completion=True)

        self.assertEqual(res["action"], "created_new_installation")
        self.assertEqual(res["apple_id"], "fresh@example.com")
        self.assertEqual(res["device_udid"], "00008110-TESTDEVICE")
        self.assertTrue(res["enqueued"])
        self.assertTrue(res["completed"])
        self.assertEqual(res["status"], "installed")
        self.assertEqual(call_count["n"], 2)

        # Verify anisette_mode was read from option-anisette-mode
        status = server.get_installation_status(res["installation_id"])
        self.assertTrue(status["found"])
        self.assertEqual(status["installation"]["anisette_mode"], 3)

    def test_trigger_refresh_does_not_false_fail_when_clear_errors_false(self) -> None:
        self._init_db(with_device=True)
        conn = server.get_db_connection(readonly=False)
        try:
            conn.execute(
                "INSERT INTO stored_files VALUES (1, ?, ?, NULL, 'abc.ipa', 'App.ipa', '.ipa', 100, X'00', '')",
                (server.ZERO_TIME, server.ZERO_TIME),
            )
            conn.execute(
                """
                INSERT INTO installations VALUES (
                    10, ?, ?, NULL, 'MyApp', 1, '00008110-TESTDEVICE', 'a@b.com', 2, '{}', 3, 0, NULL,
                    '2026-11-01 10:00:00+03:00', '1.0', 'com.test.app', 0, '', 7, 96,
                    ?, '', 'Old error', 1, '2026-11-02 10:00:00+03:00'
                )
                """,
                (server.ZERO_TIME, server.ZERO_TIME, server.ZERO_TIME),
            )
            conn.commit()
        finally:
            conn.close()

        def fake_get(url: str, params=None, timeout=None):
            c = server.get_db_connection(readonly=False)
            try:
                c.execute(
                    "UPDATE installations SET last_updated = '2026-11-21 15:00:00+03:00', enqueued_at = ?, enqueue_token = '' WHERE id = 10",
                    (server.ZERO_TIME,),
                )
                c.commit()
            finally:
                c.close()
            return FakeResponse(200, "Task enqueuedOK")

        with mock.patch.object(server, "ensure_sideloadly_running", return_value={"running": True, "started_now": False}), \
             mock.patch.object(server.httpx, "get", side_effect=fake_get), \
             mock.patch.object(server.time, "sleep", return_value=None):
            res = server.trigger_refresh(installation_id=10, clear_errors=False, wait_for_completion=True)

        self.assertTrue(res["completed"])
        self.assertEqual(res["status"], "installed")

    def test_cancel_queue_clears_stuck_tasks(self) -> None:
        self._init_db(with_device=True)
        conn = server.get_db_connection(readonly=False)
        try:
            conn.execute(
                "INSERT INTO stored_files VALUES (1, ?, ?, NULL, 'abc.ipa', 'App.ipa', '.ipa', 100, X'00', '')",
                (server.ZERO_TIME, server.ZERO_TIME),
            )
            conn.execute(
                """
                INSERT INTO installations VALUES (
                    7, ?, ?, NULL, 'StuckApp', 1, '00008110-TESTDEVICE', 'a@b.com', 2, '{}', 3, 0, NULL,
                    ?, '1.0', 'com.test.stuck', 0, '', 7, 96,
                    '2026-11-21 12:00:00+03:00', 'tok123', '', 0, ?
                )
                """,
                (server.ZERO_TIME, server.ZERO_TIME, server.ZERO_TIME, server.ZERO_TIME),
            )
            conn.commit()
        finally:
            conn.close()

        res = server.cancel_queue()
        self.assertEqual(res["cleared_count"], 1)
        st = server.get_installation_status(7)
        self.assertFalse(st["installation"]["is_enqueued"])

    def test_options_validation(self) -> None:
        server.set_sideloadly_option("zipstream", "1")
        opts = server.get_sideloadly_options()
        self.assertEqual(opts["options"]["zipstream"], "1")
        with self.assertRaises(ValueError):
            server.set_sideloadly_option("../evil", "1")

    def test_device_installed_apps_parsing(self) -> None:
        log = self.sl_dir / "sideloadlydaemon.log"
        log.write_text(
            "2026/11/21 10:00:00 Installed apps on 00008110-TESTDEVICE: 2 "
            "[{com.example.one OneApp User OneApp 1.0 1 OneApp /private/var/mobile/Containers/Data/Application/UUID-1} "
            "{com.example.two TwoApp User TwoApp 2.0 2 TwoApp /private/var/mobile/Containers/Data/Application/UUID-2}]\n",
            encoding="utf-8",
        )
        res = server.get_device_installed_apps(search_query="two")
        self.assertEqual(res["device_udid"], "00008110-TESTDEVICE")
        self.assertEqual(res["total_matched"], 1)
        self.assertEqual(res["apps"][0]["bundle_id"], "com.example.two")

    def test_prepare_ios_cloud_build_flutter_swift_react_native_and_dotenv(self) -> None:
        # 1. Flutter project detection & workflow generation
        flutter_dir = self.root / "my_flutter_app"
        flutter_dir.mkdir()
        (flutter_dir / "pubspec.yaml").write_text("name: my_flutter_app\n", encoding="utf-8")
        (flutter_dir / ".env").write_text('GITHUB_PAT="ghp_testSecretToken123"\n', encoding="utf-8")

        prep_fl = server.prepare_ios_cloud_build(str(flutter_dir))
        self.assertEqual(prep_fl["detected_project_type"], "flutter")
        self.assertTrue(prep_fl["workflow_written"])
        self.assertTrue(prep_fl["github_pat_configured"])
        self.assertIn("GITHUB_PAT", prep_fl["github_pat_source"])
        wf_text = Path(prep_fl["workflow_path"]).read_text(encoding="utf-8")
        self.assertIn("flutter build ios --release --no-codesign", wf_text)
        gi_text = Path(prep_fl["gitignore_path"]).read_text(encoding="utf-8")
        self.assertIn(".env", gi_text)

        # 2. Swift project on Windows without .xcodeproj -> auto-scaffolds XcodeGen project.yml
        swift_dir = self.root / "my_swift_app"
        swift_dir.mkdir()
        (swift_dir / "ContentView.swift").write_text("import SwiftUI\n", encoding="utf-8")
        prep_sw = server.prepare_ios_cloud_build(str(swift_dir), app_name="PoiziSwift", bundle_id="com.poizi.swift")
        self.assertEqual(prep_sw["detected_project_type"], "swift")
        self.assertIsNotNone(prep_sw["scaffolded_xcodegen_project_yml"])
        proj_yml = (swift_dir / "project.yml").read_text(encoding="utf-8")
        self.assertIn("com.poizi.swift", proj_yml)

        # 3. React Native project detection
        rn_dir = self.root / "my_rn_app"
        rn_dir.mkdir()
        (rn_dir / "package.json").write_text(
            json.dumps({"name": "my-rn-app", "dependencies": {"react": "18.2.0", "react-native": "0.74.0"}}),
            encoding="utf-8",
        )
        prep_rn = server.prepare_ios_cloud_build(str(rn_dir))
        self.assertEqual(prep_rn["detected_project_type"], "react_native")
        rn_wf = Path(prep_rn["workflow_path"]).read_text(encoding="utf-8")
        self.assertIn("pod install", rn_wf)

    def test_cloud_build_and_sideload_end_to_end_with_dotenv_pat(self) -> None:
        self._init_db(with_device=True)
        (self.roaming_sl / "cert-dev@example.com.pem").write_text("CERT", encoding="utf-8")

        proj_dir = self.root / "cloud_flutter_app"
        proj_dir.mkdir()
        (proj_dir / "pubspec.yaml").write_text("name: cloud_flutter_app\n", encoding="utf-8")
        (proj_dir / ".env").write_text("GITHUB_PAT=ghp_mockPatFromDotEnv999\n", encoding="utf-8")

        # Build an in-memory artifact zip containing a valid unsigned .ipa
        ipa_file = make_dummy_ipa(self.root / "unsigned.ipa", bundle_id="com.cloud.flutter", name="CloudFlutter")
        art_zip_buf = server.io.BytesIO()
        with zipfile.ZipFile(art_zip_buf, "w") as zf:
            zf.write(ipa_file, arcname="unsigned.ipa")
        art_zip_bytes = art_zip_buf.getvalue()

        class FakeBinaryResponse:
            def __init__(self, status_code: int, data: Any = None, content: bytes = b"", text: str = "") -> None:
                self.status_code = status_code
                self._data = data
                self.content = content
                self.text = text

            def json(self) -> Any:
                return self._data

        def fake_httpx_get(url: str, headers=None, params=None, follow_redirects=False, timeout=None):
            if url.endswith("/user"):
                self.assertEqual(headers["Authorization"], "Bearer ghp_mockPatFromDotEnv999")
                return FakeBinaryResponse(200, {"login": "DarkMirrorq"})
            if url.endswith("/repos/DarkMirrorq/cloud_flutter_app-ios-build"):
                return FakeBinaryResponse(404, {"message": "Not Found"}, text="Not Found")
            if url.endswith("/actions/runs") and params:
                return FakeBinaryResponse(
                    200,
                    {
                        "workflow_runs": [
                            {
                                "id": 777,
                                "html_url": "https://github.com/DarkMirrorq/cloud_flutter_app-ios-build/actions/runs/777",
                                "status": "completed",
                                "conclusion": "success",
                                "head_sha": "abc123sha",
                            }
                        ]
                    },
                )
            if url.endswith("/actions/runs/777"):
                return FakeBinaryResponse(
                    200,
                    {
                        "id": 777,
                        "html_url": "https://github.com/DarkMirrorq/cloud_flutter_app-ios-build/actions/runs/777",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": "abc123sha",
                    },
                )
            if url.endswith("/actions/runs/777/jobs"):
                return FakeBinaryResponse(
                    200,
                    {"jobs": [{"id": 888, "name": "build-ios-ipa", "status": "completed", "conclusion": "success", "steps": []}]},
                )
            if url.endswith("/actions/runs/777/artifacts"):
                return FakeBinaryResponse(
                    200,
                    {
                        "artifacts": [
                            {
                                "id": 999,
                                "name": "unsigned-ipa",
                                "size_in_bytes": len(art_zip_bytes),
                                "expired": False,
                                "archive_download_url": "https://api.github.com/repos/DarkMirrorq/cloud_flutter_app-ios-build/actions/artifacts/999/zip",
                            }
                        ]
                    },
                )
            if url.endswith("/artifacts/999/zip"):
                return FakeBinaryResponse(200, content=art_zip_bytes)
            if "/enqueue" in url:
                conn = server.get_db_connection(readonly=False)
                try:
                    conn.execute(
                        "UPDATE installations SET last_updated = ?, enqueued_at = ?, enqueue_token = '' WHERE id = ?",
                        (server.now_go_iso(), server.ZERO_TIME, int(params["id"])),
                    )
                    conn.commit()
                finally:
                    conn.close()
                return FakeResponse(200, "Task enqueuedOK")
            raise AssertionError(f"Unexpected GET URL: {url}")

        def fake_httpx_post(url: str, headers=None, json=None, timeout=None):
            if url.endswith("/user/repos"):
                self.assertTrue(json["private"])
                return FakeBinaryResponse(201, {"full_name": "DarkMirrorq/cloud_flutter_app-ios-build"})
            raise AssertionError(f"Unexpected POST URL: {url}")

        def fake_run_git(pdir: Path, args: list[str], pat_token: str = None, timeout: int = 120):
            if args == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, stdout="abc123sha\n", stderr="")
            if args == ["status", "--porcelain"]:
                return subprocess.CompletedProcess(args, 0, stdout="M pubspec.yaml\n", stderr="")
            if args[0] == "push":
                self.assertEqual(pat_token, "ghp_mockPatFromDotEnv999")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(server, "ensure_sideloadly_running", return_value={"running": True, "started_now": False}), \
             mock.patch.object(server.httpx, "get", side_effect=fake_httpx_get), \
             mock.patch.object(server.httpx, "post", side_effect=fake_httpx_post), \
             mock.patch.object(server, "_run_git", side_effect=fake_run_git), \
             mock.patch.object(server.time, "sleep", return_value=None):
            res = server.cloud_build_and_sideload(str(proj_dir), poll_interval_seconds=1)

        self.assertEqual(res["stage"], "completed")
        self.assertTrue(res["completed"])
        self.assertEqual(res["status"], "installed")
        self.assertTrue(res["trigger"]["repo_created"])
        self.assertEqual(res["trigger"]["repo_full_name"], "DarkMirrorq/cloud_flutter_app-ios-build")
        self.assertEqual(res["download_and_sideload"]["ipa_metadata"]["bundle_id"], "com.cloud.flutter")
        self.assertEqual(res["download_and_sideload"]["sideload_result"]["status"], "installed")


if __name__ == "__main__":
    unittest.main()

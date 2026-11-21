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


if __name__ == "__main__":
    unittest.main()

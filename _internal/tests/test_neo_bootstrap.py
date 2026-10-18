"""Behavior tests for safe bootstrap diagnostics; no Windows/API side effects."""
from pathlib import Path
from types import SimpleNamespace
import json
import logging
import os
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from jarvis.runtime.diagnostics import collect_logs, redact, RedactingFormatter, write_report
from jarvis.runtime.self_test import run_self_test
from jarvis.runtime.startup_handshake import publish_ready, acknowledge_existing_instance


class Config:
    def __init__(self, root, enabled=False):
        self.paths = SimpleNamespace(root=root, settings=root / "settings.json", logs=root / "logs")
        self.enabled = enabled
    def get(self, key, default=None):
        return {"ollama.vision_enabled": self.enabled, "local_chat.enabled": False, "ollama.requests_enabled": False, "ollama.vision_model": "test-vision:1"}.get(key, default)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        from jarvis.ollama_status import reset_status
        reset_status()

    def test_startup_scripts_preserve_unicode_paths_with_wscript_bom(self):
        from jarvis.config import ConfigManager
        from jarvis.windows_startup import _write_hidden_launcher
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "Мой ассистент" / "ação 日本語" / "NEO.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"test fixture")
            with patch.dict(os.environ, {"APPDATA": str(root / "Roaming"), "LOCALAPPDATA": str(root / "Local")}):
                config = ConfigManager.__new__(ConfigManager)
                standard = config._write_standard_autostart_launcher(executable)
                priority = _write_hidden_launcher(executable)
            for script in (standard, priority):
                self.assertTrue(script.read_bytes().startswith(b"\xff\xfe"))
                content = script.read_text(encoding="utf-16")
                self.assertIn('WshShell.Run', content)
                target = content.split('WshShell.Run """', 1)[1].split('""", 0, False', 1)[0]
                self.assertEqual(os.path.normcase(str(Path(target).resolve())), os.path.normcase(str(executable.resolve())))
                self.assertIn('ação 日本語', target)
                self.assertIn('("NEO_JARVIS_AUTOSTART") = "1"', content)

    def test_runtime_lock_is_only_published_when_every_artifact_hash_is_known(self):
        from runtime_lock import build_lock
        package = SimpleNamespace(metadata={"Name": "Example_Package"}, version="1.2.3")
        with tempfile.TemporaryDirectory() as directory, patch("runtime_lock.importlib.metadata.distributions", return_value=[package]):
            root = Path(directory)
            reports = root / ".neo_install_reports"
            reports.mkdir()
            (reports / "deps.json").write_text(json.dumps({"install": [{"metadata": {"name": "example-package", "version": "1.2.3"}, "download_info": {"archive_info": {"hashes": {"sha256": "a" * 64}}}}]}))
            self.assertTrue(build_lock(root)["complete"])
            self.assertIn("example-package==1.2.3 --hash=sha256:", (root / "requirements.lock.txt").read_text())
            (reports / "deps.json").unlink()
            self.assertFalse(build_lock(root)["complete"])
            self.assertFalse((root / "requirements.lock.txt").exists())

    def test_configured_model_preparation_does_not_download_unused_models(self):
        from prepare_speech_models import required_models
        from tests.test_speech_understanding import Config as SpeechConfig
        config = SpeechConfig({"voice.stt_model": "small", "wake_word.stt_model": "tiny", "voice.partial_stt_model": "base", "voice.live_partial_max_updates": 0})
        self.assertEqual(required_models(config), ["small", "tiny"])
        config.values["voice.live_partial_max_updates"] = 2
        self.assertEqual(required_models(config), ["small", "tiny", "base"])

    def test_gui_shim_is_amd64_gui_pe_with_recorded_hash(self):
        import hashlib
        import struct
        root = Path(__file__).resolve().parents[2]
        data = (root / "NEO.exe").read_bytes()
        offset = struct.unpack_from("<I", data, 0x3C)[0]
        self.assertEqual(data[offset:offset + 4], b"PE\0\0")
        self.assertEqual(struct.unpack_from("<H", data, offset + 4)[0], 0x8664)
        self.assertEqual(struct.unpack_from("<H", data, offset + 24 + 68)[0], 2)
        manifest = json.loads((root / "_internal/launcher/build_manifest.json").read_text())
        self.assertEqual(manifest["binary"]["sha256"], hashlib.sha256(data).hexdigest())

    def test_disabled_vision_makes_no_requests_and_write_probe_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.Session") as session:
            root = Path(directory)
            (root / "settings.json").write_text('{"kept": true}')
            result = run_self_test(Config(root), quick=False)
            session.assert_not_called()
            self.assertEqual((root / "settings.json").read_text(), '{"kept": true}')
            self.assertFalse(list(root.glob(".neo-write-probe-*")))
            checks = {x["name"]: x for x in result["checks"]}
            self.assertEqual(checks["ollama"]["status"], "DISABLED")
            self.assertEqual(checks["settings_write"]["status"], "PASS")
            if os.name != "nt":
                self.assertEqual(checks["windows_api"]["status"], "SKIP")
            json.loads((root / "diagnostics/self_test_latest.json").read_text())

    def test_unavailable_optional_vision_does_not_break_core(self):
        with tempfile.TemporaryDirectory() as directory, patch("requests.Session") as factory:
            session = factory.return_value.__enter__.return_value
            session.get.side_effect = OSError("offline")
            result = run_self_test(Config(Path(directory), True))
            self.assertEqual(session.get.call_count, 2)  # bounded readiness retry
            self.assertFalse(session.trust_env)
            checks = {x["name"]: x for x in result["checks"]}
            self.assertEqual(checks["ollama"]["status"], "UNAVAILABLE")
            self.assertEqual(checks["ollama_model"]["status"], "SKIP")  # unknown, not a second failure
            self.assertTrue(checks["ollama"]["ok"])

    def test_readiness_unique_run_and_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "abc"
            run.mkdir()
            target = run / "ready.json"
            with patch.dict(os.environ, {"NEO_LAUNCH_RUN_ID": "abc", "NEO_LAUNCH_READY_PATH": str(target)}):
                self.assertTrue(publish_ready())
                result = json.loads(target.read_text())
                self.assertEqual(result["pid"], os.getpid())
                self.assertEqual(result["run_id"], "abc")
                self.assertEqual(result["state"], "ui_event_loop_ready")
                self.assertTrue(acknowledge_existing_instance())
                self.assertEqual(json.loads(target.read_text())["state"], "existing_instance_acknowledged")
            with patch.dict(os.environ, {"NEO_LAUNCH_RUN_ID": "wrong", "NEO_LAUNCH_READY_PATH": str(target)}):
                self.assertFalse(publish_ready())

    def test_redaction_handles_values_headers_urls_and_exceptions(self):
        examples = ['api_key=private123', '{"token":"private123"}', 'Authorization: Bearer private123', 'Cookie: auth=private123; k=2', 'https://api.test?q=1&api_key=private123&engine=google', '123456789:abcdefghijklmnopqrstuvwxyzABCDEF']
        for example in examples:
            self.assertNotIn("private123", redact(example))
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "api_key=%s", ("private123",), None)
        self.assertNotIn("private123", RedactingFormatter().format(record))

    def test_report_json_stays_valid_and_redacts_nested_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_report(Path(directory) / "r.json", {"key": "kept", "nested": {"api_key": "private123"}, "detail": "token=private123"})
            value = json.loads(path.read_text())
            self.assertEqual(value["key"], "kept")
            self.assertEqual(value["nested"]["api_key"], "[REDACTED]")
            self.assertNotIn("private123", path.read_text())

    def test_export_excludes_user_files_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "logs").mkdir()
            log = root / "logs" / "neo.log"
            log.write_text("token=private123")
            (root / "settings.json").write_text("do not export")
            (root / "screenshots").mkdir()
            (root / "screenshots" / "private.png").write_bytes(b"private")
            archive = collect_logs(root)
            with zipfile.ZipFile(archive) as zf:
                self.assertEqual(set(zf.namelist()), {"logs/neo.log", "manifest.json"})
                self.assertNotIn(b"private123", zf.read("logs/neo.log"))
            self.assertIn("private123", log.read_text())


if __name__ == "__main__":
    unittest.main()

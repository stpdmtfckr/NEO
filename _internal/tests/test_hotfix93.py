# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis.config import ConfigManager
from jarvis.integrations.mail_services import (
    GMAIL_SCOPES,
    GmailClient,
    MailMessage,
    MailRuClient,
    MailStatus,
    MAILRU_IMAP_HOST,
    MAILRU_IMAP_PORT,
    MAILRU_SMTP_HOST,
    MAILRU_SMTP_PORT,
    format_mail_briefing,
    format_mail_notification,
    test_mailru as verify_mailru_connection,
)
from jarvis.routing.ownership_firewall import classify_turn_ownership

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class DictConfig:
    def __init__(self, values: dict[str, object]):
        self.values = dict(values)

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class Hotfix93Regression(unittest.TestCase):
    def test_settings_v142_and_topmost_hotkey_default(self):
        data = json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["settings_version"], 142)
        self.assertEqual(data["hotkeys"]["window_topmost_toggle"], "Ctrl+Alt+T")
        self.assertIn("gmail", data["integrations"])
        self.assertIn("mailru", data["integrations"])

    def test_mail_secrets_are_empty_in_packaged_defaults(self):
        data = json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))
        gmail = data["integrations"]["gmail"]
        mailru = data["integrations"]["mailru"]
        self.assertEqual(gmail["client_id_encrypted"], "")
        self.assertEqual(gmail["refresh_token_encrypted"], "")
        self.assertEqual(mailru["app_password_encrypted"], "")
        self.assertNotIn("password", gmail)

    def test_migration_adds_v142_contract(self):
        defaults = INTERNAL / "config" / "default_settings.json"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            appdata = root / "NeoJarvis"
            appdata.mkdir(parents=True, exist_ok=True)
            (appdata / "settings.json").write_text(json.dumps({"settings_version": 141}), encoding="utf-8")
            with patch.dict(os.environ, {"APPDATA": td, "LOCALAPPDATA": td}, clear=False):
                cfg = ConfigManager(defaults)
            self.assertGreaterEqual(cfg.get("settings_version"), 142)
            self.assertEqual(cfg.get("hotkeys.window_topmost_toggle"), "Ctrl+Alt+T")
            self.assertFalse(cfg.get("integrations.gmail.enabled"))
            self.assertFalse(cfg.get("integrations.mailru.enabled"))

    def test_main_window_global_topmost_toggle(self):
        src = (J / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("_window_topmost_hotkey_id", src)
        self.assertIn('hotkeys.window_topmost_toggle', src)
        self.assertIn("def _toggle_window_topmost_hotkey", src)
        self.assertIn("self._set_runtime_topmost(False, hide=True)", src)
        self.assertIn("self._set_runtime_topmost(True, hide=False)", src)
        self.assertIn("if self.isVisible():", src)

    def test_settings_expose_requested_mail_controls(self):
        src = (J / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        for token in (
            "Включить интеграцию Gmail",
            "Авторизовать Gmail",
            "Включить интеграцию Mail.ru",
            "Проверить IMAP/SMTP",
            "Показывать общее количество писем в выбранных папках",
            "Показывать общее количество непрочитанных",
            "Показывать количество писем за последние 24 часа",
            "Отправитель + содержимое",
            "Добавлять Gmail в сводку на день",
            "Добавлять Mail.ru в сводку на день",
        ):
            self.assertIn(token, src)

    def test_gmail_uses_official_oauth_rest_contract(self):
        src = (J / "integrations" / "mail_services.py").read_text(encoding="utf-8")
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", src)
        self.assertIn("oauth2.googleapis.com/token", src)
        self.assertIn("gmail.googleapis.com/gmail/v1", src)
        self.assertIn("code_challenge_method", src)
        self.assertIn("access_type", src)
        self.assertIn("offline", src)
        self.assertEqual(GMAIL_SCOPES, ("https://www.googleapis.com/auth/gmail.modify",))

    def test_mailru_uses_documented_ssl_endpoints(self):
        self.assertEqual((MAILRU_IMAP_HOST, MAILRU_IMAP_PORT), ("imap.mail.ru", 993))
        self.assertEqual((MAILRU_SMTP_HOST, MAILRU_SMTP_PORT), ("smtp.mail.ru", 465))

    def test_notification_count_only(self):
        cfg = DictConfig({
            "integrations.gmail.notify_total_count": False,
            "integrations.gmail.notify_unread_count": True,
            "integrations.gmail.notify_recent24h_count": True,
            "integrations.gmail.message_display_detail": "count_only",
            "app.language": "ru",
        })
        text = format_mail_notification(cfg, "gmail", {"unread": 7, "recent24h": 4}, [{"sender": "Alice", "preview": "hello"}])
        self.assertIn("Непрочитанных всего: 7", text)  # 187: separate total/today lines
        self.assertIn("За 24 ч: 4", text)
        self.assertNotIn("Alice", text)
        self.assertNotIn("новых:", text)

    def test_notification_sender_preview(self):
        cfg = DictConfig({
            "integrations.mailru.notify_total_count": False,
            "integrations.mailru.notify_unread_count": False,
            "integrations.mailru.notify_recent24h_count": False,
            "integrations.mailru.message_display_detail": "sender_preview",
        })
        text = format_mail_notification(cfg, "mailru", {"unread": 1, "recent24h": 1}, [{"sender": "Иван", "preview": "Документы готовы"}])
        self.assertIn("Иван: Документы готовы", text)

    def test_briefing_can_include_senders(self):
        cfg = DictConfig({"integrations.gmail.briefing_show_senders": True})
        text = format_mail_briefing(cfg, "gmail", {
            "unread": 2,
            "recent24h": 3,
            "latest": [
                {"unread": True, "sender": "Alice"},
                {"unread": True, "sender": "Bob"},
                {"unread": False, "sender": "Carol"},
            ],
        })
        self.assertIn("Непрочитанных: 2", text)
        self.assertIn("За последние 24 часа: 3", text)
        self.assertIn("Alice", text)
        self.assertIn("Bob", text)
        self.assertNotIn("Carol", text)

    def test_mail_monitor_baselines_before_notifications(self):
        src = (J / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertIn("Baseline only", src)
        self.assertIn("collect_statuses, self.config, include_latest=20", src)
        self.assertIn("format_mail_notification", src)
        self.assertIn("_mail_monitor_inflight", src)
        self.assertIn('notifications_enabled", True', src)
        self.assertIn("Daily briefing performs its own one-shot status read", src)
        self.assertIn("self._append_assistant(text, mirror_telegram=False, localize=False)", src)

    def test_briefing_calls_mail_layer(self):
        src = (J / "tools" / "integrations.py").read_text(encoding="utf-8")
        # Briefing reads each provider separately so a failed account can retry
        # without re-reading a successful one (covered by the real Qt 160 tests).
        self.assertIn("provider_client(config, p)", src)
        self.assertIn("client.status(include_latest=5).compact()", src)
        self.assertIn("format_mail_briefing", src)
        self.assertIn("include_in_briefing", src)

    def test_email_tool_is_full_not_browser_only(self):
        src = (J / "tools" / "extended.py").read_text(encoding="utf-8")
        for action in ("status", "unread", "recent24h", "latest", "search", "read", "mark_read", "draft", "send", "reply"):
            self.assertIn(f'"{action}"', src)
        self.assertIn("provider_client(config, provider)", src)
        self.assertIn("client.send", src)
        self.assertNotIn("EMAIL IN BROWSER (Mail.ru only", src)

    def test_email_owner_covers_both_services(self):
        for phrase in (
            "сколько непрочитанных писем в Gmail",
            "покажи последние письма Mail.ru",
            "ответь на последнее письмо",
        ):
            claim = classify_turn_ownership(phrase)
            self.assertEqual(claim.owner, "email", phrase)
            self.assertTrue(claim.enforced, phrase)
            self.assertIn("email", claim.allowed_tools)


    def test_gmail_status_uses_configured_folder_scope(self):
        cfg = DictConfig({
            "integrations.gmail.client_id_encrypted": "client-id",
            "integrations.gmail.refresh_token_encrypted": "refresh-token",
            "integrations.gmail.account_email": "me@gmail.com",
            "integrations.gmail.include_inbox": True,
            "integrations.gmail.additional_folders": [],
        })
        client = GmailClient(cfg)
        latest = [MailMessage("gmail", "m1", sender="A", folder="Входящие")]
        unread_latest = [MailMessage("gmail", "u1", sender="B", unread=True, folder="Входящие")]
        with patch.object(client, "_folder_status", return_value=type("Row", (), {"name":"Входящие","total":12,"unread":3,"recent24h":4,"today":2,"compact":lambda self: {"name":"Входящие","total":12,"unread":3,"recent24h":4,"today":2}})()), \
             patch.object(client, "_count_query", side_effect=[12, 3, 4, 2, 1]), \
             patch.object(client, "list_messages", side_effect=[latest, unread_latest]):
            status = client.status(include_latest=5)
        self.assertEqual((status.total, status.unread, status.recent24h, status.today), (12, 3, 4, 2))
        self.assertEqual(status.folders[0].name, "Входящие")
        self.assertEqual(status.unread_latest[0].message_id, "u1")

    def test_mailru_connection_test_authenticates_both_imap_and_smtp(self):
        fake_status = MailStatus("mailru", total=9, unread=2, account="neo@mail.ru")
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.__exit__.return_value = False
        with patch.object(MailRuClient, "status", return_value=fake_status), \
             patch("jarvis.integrations.mail_services.smtplib.SMTP_SSL", return_value=smtp) as smtp_ctor:
            result = verify_mailru_connection("neo@mail.ru", "app-pass")
        self.assertTrue(result["imap"])
        self.assertTrue(result["smtp"])
        smtp_ctor.assert_called_once_with("smtp.mail.ru", 465, timeout=10.0)
        smtp.login.assert_called_once_with("neo@mail.ru", "app-pass")

    def test_briefing_prefers_actual_unread_sender_rows(self):
        cfg = DictConfig({"integrations.gmail.briefing_show_senders": True})
        text = format_mail_briefing(cfg, "gmail", {
            "unread": 1, "recent24h": 1,
            "latest": [{"unread": False, "sender": "Newest but read"}],
            "unread_latest": [{"unread": True, "sender": "Actual unread"}],
        })
        self.assertIn("Actual unread", text)
        self.assertNotIn("Newest but read", text)

    def test_mailru_recent24h_uses_wide_candidate_window_then_exact_cutoff(self):
        src = (J / "integrations" / "mail_services.py").read_text(encoding="utf-8")
        self.assertIn("cutoff.astimezone() - timedelta(days=1)", src)
        self.assertIn("timedelta(hours=24)", src)
        self.assertIn('"(UID INTERNALDATE)"', src)
        extended = (J / "tools" / "extended.py").read_text(encoding="utf-8")
        self.assertIn("client.list_recent24h", extended)

    def test_legacy_router_no_longer_emits_removed_mail_actions(self):
        src = (J / "runtime" / "router.py").read_text(encoding="utf-8")
        self.assertNotIn('"action": "open_latest_from"', src)
        self.assertNotIn('"action": "important"', src)
        self.assertIn('"action": "recent24h"', src)
        self.assertIn('"action": "unread"', src)


if __name__ == "__main__":
    unittest.main()

# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jarvis.config import ConfigManager
from jarvis.integrations.mail_services import (
    GmailClient,
    MailFolderStatus,
    MailMessage,
    MailStatus,
    _imap_utf7_decode,
    _imap_utf7_encode,
    display_detail_for_provider,
    format_mail_briefing,
    format_mail_notification,
    selected_mail_folders,
)
from jarvis.runtime.router import HybridRouter

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class DictConfig:
    def __init__(self, values: dict[str, object]): self.values = dict(values)
    def get(self, key: str, default=None): return self.values.get(key, default)


class Hotfix94Regression(unittest.TestCase):
    def test_settings_v143_and_mail_source_contract(self):
        data = json.loads((INTERNAL / "config" / "default_settings.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["settings_version"], 143)
        for provider in ("gmail", "mailru"):
            cfg = data["integrations"][provider]
            self.assertTrue(cfg["include_inbox"])
            self.assertEqual(cfg["additional_folders"], [])
            self.assertFalse(cfg["notify_total_count"])
            self.assertTrue(cfg["notify_unread_count"])
            self.assertTrue(cfg["notify_recent24h_count"])
            self.assertIn(cfg["message_display_detail"], {"count_only","sender","sender_subject","sender_preview","sender_subject_preview"})
            self.assertNotIn("notify_new_count", cfg)

    def test_v142_profile_migrates_and_retires_new_since_poll(self):
        defaults = INTERNAL / "config" / "default_settings.json"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "NeoJarvis"; root.mkdir(parents=True)
            old = {
                "settings_version": 142,
                "integrations": {
                    "gmail": {"notify_new_count": True, "notification_detail": "sender_preview"},
                    "mailru": {"notify_new_count": True, "notification_detail": "sender"},
                },
            }
            (root / "settings.json").write_text(json.dumps(old), encoding="utf-8")
            import os
            from unittest.mock import patch
            with patch.dict(os.environ, {"APPDATA": td, "LOCALAPPDATA": td}, clear=False):
                cfg = ConfigManager(defaults)
            self.assertGreaterEqual(cfg.get("settings_version"), 143)
            self.assertEqual(cfg.get("integrations.gmail.message_display_detail"), "sender_preview")
            self.assertFalse("notify_new_count" in cfg.get("integrations.gmail", {}))

    def test_selected_folders_include_inbox_and_custom_names(self):
        cfg = DictConfig({
            "integrations.gmail.include_inbox": True,
            "integrations.gmail.additional_folders": ["Проекты", "Рассылки", "проекты"],
        })
        self.assertEqual(selected_mail_folders(cfg, "gmail"), ["Входящие", "Проекты", "Рассылки"])

    def test_selected_folders_can_exclude_inbox(self):
        cfg = DictConfig({
            "integrations.mailru.include_inbox": False,
            "integrations.mailru.additional_folders": "Проекты; Отправленные",
        })
        self.assertEqual(selected_mail_folders(cfg, "mailru"), ["Проекты", "Отправленные"])

    def test_gmail_builtin_folder_aliases(self):
        self.assertEqual(GmailClient._folder_query("Входящие"), "in:inbox")
        self.assertEqual(GmailClient._folder_query("Отправленные"), "in:sent")
        self.assertEqual(GmailClient._folder_query("Рассылки"), "category:promotions")
        self.assertEqual(GmailClient._folder_query("Отправленные себе"), "from:me to:me")
        self.assertEqual(GmailClient._folder_query("Проекты"), 'label:"Проекты"')

    def test_mailru_modified_utf7_roundtrip_for_russian_folder(self):
        name = "Проекты/Работа"
        self.assertEqual(_imap_utf7_decode(_imap_utf7_encode(name)), name)

    def test_five_display_modes_are_supported(self):
        for mode in ("count_only", "sender", "sender_subject", "sender_preview", "sender_subject_preview"):
            cfg = DictConfig({"integrations.gmail.message_display_detail": mode})
            self.assertEqual(display_detail_for_provider(cfg, "gmail"), mode)

    def test_notification_has_no_legacy_new_counter_and_optional_total(self):
        cfg = DictConfig({
            "app.language": "ru",
            "integrations.gmail.notify_total_count": True,
            "integrations.gmail.notify_unread_count": True,
            "integrations.gmail.notify_recent24h_count": True,
            "integrations.gmail.message_display_detail": "sender_subject",
        })
        text = format_mail_notification(cfg, "gmail", {"total": 20, "unread": 4, "recent24h": 3}, [
            {"sender": "Alice", "subject": "Project", "folder": "Проекты"}
        ])
        self.assertIn("Всего: 20", text)  # 187: one counter per line
        self.assertIn("Непрочитанных всего: 4", text)
        self.assertIn("За 24 ч: 3", text)
        self.assertIn("[Проекты] Alice — Project", text)
        self.assertNotIn("новых:", text)

    def test_briefing_can_break_down_selected_folders(self):
        cfg = DictConfig({
            "integrations.gmail.briefing_show_folders": True,
            "integrations.gmail.briefing_show_senders": False,
        })
        text = format_mail_briefing(cfg, "gmail", {
            "unread": 7, "recent24h": 4,
            "folders": [
                {"name":"Входящие","unread":5,"recent24h":3},
                {"name":"Проекты","unread":2,"recent24h":1},
            ],
        })
        self.assertIn("Входящие", text); self.assertIn("Проекты", text)
        self.assertIn("непрочитанных 5", text); self.assertIn("за 24 ч 1", text)

    def test_router_generic_unread_aggregates_provider_auto(self):
        route = HybridRouter().route("сколько у меня непрочитанных писем")
        self.assertIsNotNone(route)
        self.assertEqual(route.tool, "email")
        self.assertEqual(route.arguments, {"provider": "auto", "action": "unread"})

    def test_router_explicit_gmail_narrows_provider(self):
        route = HybridRouter().route("сколько непрочитанных писем в Gmail")
        self.assertEqual(route.arguments["provider"], "gmail")
        self.assertEqual(route.arguments["action"], "unread")

    def test_router_explicit_mailru_today(self):
        route = HybridRouter().route("сколько писем за сегодня в Mail.ru")
        self.assertEqual(route.arguments["provider"], "mailru")
        self.assertEqual(route.arguments["action"], "today")

    def test_router_unread_today_and_24h_are_distinct(self):
        today = HybridRouter().route("сколько непрочитанных писем за сегодня")
        day = HybridRouter().route("сколько непрочитанных писем за последние 24 часа")
        self.assertEqual(today.arguments["action"], "unread_today")
        self.assertEqual(day.arguments["action"], "unread_recent24h")

    def test_router_folder_payload_is_preserved(self):
        route = HybridRouter().route("сколько писем за сегодня в папке Проекты в Gmail")
        self.assertEqual(route.arguments["provider"], "gmail")
        self.assertEqual(route.arguments["folder"].casefold(), "проекты")
        self.assertEqual(route.arguments["action"], "today")

    def test_router_all_time_folder_count(self):
        route = HybridRouter().route("сколько всего писем в папке Проекты")
        self.assertEqual(route.arguments["action"], "total")
        self.assertEqual(route.arguments["folder"].casefold(), "проекты")

    def test_router_count_does_not_emit_list_action(self):
        route = HybridRouter().route("сколько непрочитанных писем в Gmail")
        self.assertNotIn("list", route.arguments["action"])
        self.assertNotIn("limit", route.arguments)

    def test_router_list_queries_are_explicit(self):
        self.assertEqual(HybridRouter().route("покажи непрочитанные письма").arguments["action"], "unread_list")
        self.assertEqual(HybridRouter().route("покажи письма за сегодня").arguments["action"], "today_list")
        self.assertEqual(HybridRouter().route("покажи непрочитанные письма за сегодня").arguments["action"], "unread_today_list")

    def test_router_latest_field_variants(self):
        who = HybridRouter().route("от кого последнее письмо")
        subject = HybridRouter().route("какая тема последнего письма Gmail")
        read = HybridRouter().route("прочитай последнее письмо Mail.ru")
        self.assertEqual((who.arguments["action"], who.arguments["detail"], who.arguments["limit"]), ("latest", "sender", 1))
        self.assertEqual((subject.arguments["provider"], subject.arguments["detail"]), ("gmail", "sender_subject"))
        self.assertEqual((read.arguments["provider"], read.arguments["action"]), ("mailru", "read"))

    def test_email_tool_schema_contains_hotfix94_actions(self):
        src = (J / "tools" / "extended.py").read_text(encoding="utf-8")
        for token in ("unread_today_list", "unread_recent24h_list", '"folder"', '"sender_subject_preview"'):
            self.assertIn(token, src)
        self.assertIn("Generic read-only questions intentionally aggregate every enabled mailbox", src)

    def test_settings_ui_has_source_and_five_format_controls(self):
        src = (J / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
        for token in (
            "Использовать папку «Входящие»", "Дополнительные папки / ярлыки", "Дополнительные папки",
            "Показывать общее количество писем в выбранных папках", "Отправитель + тема",
            "Отправитель + содержимое", "Отправитель + тема + содержимое",
        ):
            self.assertIn(token, src)
        self.assertNotIn("Показывать количество новых писем с прошлого", src)

    def test_briefing_has_visual_section_dividers(self):
        src = (J / "tools/integrations.py").read_text(encoding="utf-8")
        self.assertIn('divider = "━━━━━━━━━━━━━━━━━━━━━━━━"', src)
        self.assertIn("combined = greeting +", src)
        self.assertIn("join(sections[1:])", src)

    def test_startup_generic_ready_message_is_removed(self):
        src = (J / "ui" / "main_window.py").read_text(encoding="utf-8")
        self.assertNotIn("Система готова. Напишите команду или нажмите микрофон.", src)
        self.assertIn("_start_morning_briefing", src)

    def test_gmail_disabled_api_error_is_human_readable(self):
        src = (J / "integrations" / "mail_services.py").read_text(encoding="utf-8")
        self.assertIn("Gmail API отключён в Google Cloud", src)
        self.assertIn("Google Cloud Console → APIs & Services → Library", src)


if __name__ == "__main__":
    unittest.main()

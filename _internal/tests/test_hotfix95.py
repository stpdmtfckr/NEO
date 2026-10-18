# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.integrations.google_calendar import parse_calendar_datetime
from jarvis.integrations.telegram_messages import TelegramMessageCenter, format_notification, format_briefing
from jarvis.routing.ownership_firewall import classify_turn_ownership
from jarvis.runtime.router import HybridRouter

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class DummyConfig:
    def __init__(self, root: Path, values=None):
        self.paths = SimpleNamespace(root=root)
        self.values = dict(values or {})
    def get(self, key, default=None): return self.values.get(key, default)


class Hotfix95Regression(unittest.TestCase):
    def test_settings_v144_service_defaults(self):
        data=json.loads((INTERNAL/'config'/'default_settings.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(data['settings_version'],144)
        self.assertNotIn('google_translate',data['integrations'])
        self.assertIn('google_calendar',data['integrations'])
        tg=data['integrations']['telegram']
        for key in ('messages_enabled','messages_notifications_enabled','messages_notification_detail','include_in_briefing','query_show_senders'):
            self.assertIn(key,tg)

    def test_translation_provider_survives_hotfix96_without_cloud(self):
        src=(J/'integrations'/'local_translation.py').read_text(encoding='utf-8')
        self.assertIn('LocalOllamaTranslationAdapter',src)
        self.assertNotIn('translation.googleapis.com/language/translate/v2',src)
        self.assertNotIn('GoogleCloudTranslationAdapter',src)

    def test_calendar_parser(self):
        dt=parse_calendar_datetime('завтра','18:30')
        self.assertEqual((dt.hour,dt.minute),(18,30))

    def test_owner_firewall(self):
        self.assertEqual(classify_turn_ownership('сколько сообщений в Telegram').owner,'telegram_messages')
        self.assertEqual(classify_turn_ownership('какое расписание на сегодня').owner,'calendar')
        self.assertEqual(classify_turn_ownership('переведи файл на английский').owner,'translation')
        self.assertEqual(classify_turn_ownership('напиши в Discord привет').owner,'discord')

    def test_router_telegram_and_calendar(self):
        r=HybridRouter()
        self.assertEqual(r.route('сколько у меня сообщений в телеграме').tool,'telegram_messages')
        reply=r.route('ответь в телеграме Ивану что буду через пять минут')
        self.assertEqual(reply.arguments['text'],'буду через пять минут')
        self.assertNotIn('ответь',reply.arguments['text'])
        cal=r.route('добавь встречу с Андреем завтра в 17:00')
        self.assertEqual(cal.tool,'google_calendar'); self.assertEqual(cal.arguments['time'],'17:00')

    def test_router_discord_text_attach(self):
        # HOTFIX98 intentionally removes user-facing Discord account automation.
        # Named channel join/send requests must fail closed instead of acting as NeoBot.
        r=HybridRouter().route('подключись к каналу Для VIP персон на сервере Бойцовский клуб в Discord')
        self.assertEqual(r.tool,'discord'); self.assertEqual(r.arguments['action'],'user_account_automation_unavailable')
        explicit=HybridRouter().route('подключись к текстовому каналу neo-music на сервере Бойцовский клуб в Discord')
        self.assertEqual(explicit.arguments['action'],'user_account_automation_unavailable')
        recent=HybridRouter().route('сколько у меня сообщений в телеграме за последние 24 часа')
        self.assertTrue(recent.arguments['recent24h'])
        send=HybridRouter().route('напиши в Discord привет, буду через пять минут')
        self.assertEqual(send.tool,'discord'); self.assertEqual(send.arguments['action'],'user_account_automation_unavailable')

    def test_telegram_center_counts_and_sender_inflection(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=DummyConfig(Path(td),{'integrations.telegram.messages_history_limit':5000})
            c=TelegramMessageCenter(cfg)
            c.record_connection({'id':'bc1','user':{'id':99,'first_name':'Owner'},'is_enabled':True,'rights':{'can_reply':True}})
            c.record_message({'business_connection_id':'bc1','message_id':1,'date':int(time.time()),'from':{'id':1,'first_name':'Иван'},'chat':{'id':1},'text':'Будешь сегодня?'})
            self.assertEqual(c.stats()['total'],1)
            self.assertIsNotNone(c.latest('Ивану'))
            self.assertEqual(c.latest('Ивану')['text'],'Будешь сегодня?')

    def test_telegram_notification_and_briefing_modes(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=DummyConfig(Path(td),{
                'integrations.telegram.messages_notification_detail':'sender_text',
                'integrations.telegram.briefing_show_recent24h':True,
                'integrations.telegram.briefing_show_total':True,
                'integrations.telegram.briefing_show_senders':True,
            })
            c=TelegramMessageCenter(cfg)
            c.record_connection({'id':'bc','user':{'id':9},'is_enabled':True,'rights':{'can_reply':True}})
            row=c.record_message({'business_connection_id':'bc','message_id':5,'date':int(time.time()),'from':{'id':1,'first_name':'Анна'},'chat':{'id':1},'text':'Привет'})
            self.assertIn('Анна',format_notification(cfg,row)); self.assertIn('Привет',format_notification(cfg,row))
            b=format_briefing(cfg,c); self.assertIn('последние 24 часа',b); self.assertIn('Анна',b)

    def test_business_updates_are_subscribed(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        for token in ('business_connection','business_message','edited_business_message','deleted_business_messages'):
            self.assertIn(token,src)
        for token in ('📧 Почта','🖱 Нажать мышью','📅 Расписание'):
            self.assertIn(token,src)
        home = src[src.index('def _home_keyboard'):src.index('def _panel', src.index('def _home_keyboard'))]
        self.assertNotIn('🌐 Переводчик', home)
        self.assertNotIn('📨 Telegram', home)

    def test_discord_official_text_attach_exists(self):
        src=(J/'integrations'/'background'/'discord_bot.py').read_text(encoding='utf-8')
        self.assertIn('discord_official_bot_text_attach',src)
        self.assertIn('_attached_text_channel_ids',src)
        self.assertIn('discord_text_channels.json',src)
        self.assertIn('_save_text_channel_attachments',src)
        schema=(J/'tools'/'domains'/'browser_media.py').read_text(encoding='utf-8')
        self.assertIn('attach_text_channel',schema)

    def test_settings_has_google_and_telegram_controls(self):
        src=(J/'ui'/'settings_dialog.py').read_text(encoding='utf-8')
        for token in ('Gmail и Календарь','Google Calendar','Telegram Messages · Secretary Mode','Отправитель + содержимое'):
            self.assertIn(token,src)
        self.assertNotIn('Google Cloud Translation', src)

    def test_tools_registered(self):
        src=(J/'tools'/'integrations.py').read_text(encoding='utf-8')
        self.assertIn('"google_calendar"',src); self.assertIn('"telegram_messages"',src)
        plugins=(J/'plugins'/'builtin'/'__init__.py').read_text(encoding='utf-8')
        self.assertIn('integrations.google_calendar',plugins); self.assertIn('integrations.telegram_messages',plugins)


if __name__ == '__main__': unittest.main()

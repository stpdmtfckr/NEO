# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.integrations.google_calendar import parse_calendar_add_command
from jarvis.integrations.telegram_messages import TelegramMessageCenter
from jarvis.integrations.telegram_remote import TelegramRemoteController
from jarvis.runtime.router import HybridRouter

INTERNAL = Path(__file__).resolve().parents[1]
J = INTERNAL / "jarvis"


class DummyConfig:
    def __init__(self, root: Path, values=None):
        self.paths = SimpleNamespace(root=root)
        self.values = dict(values or {})
    def get(self, key, default=None):
        return self.values.get(key, default)


class Hotfix96Regression(unittest.TestCase):
    def test_settings_v145_and_removed_cloud_translation(self):
        data=json.loads((INTERNAL/'config'/'default_settings.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(data['settings_version'],145)
        self.assertNotIn('google_translate',data['integrations'])
        cal=data['integrations']['google_calendar']
        self.assertNotIn('client_id_encrypted',cal)
        self.assertNotIn('client_secret_encrypted',cal)
        self.assertIn(cal['briefing_period'], {'today','3_days','month'})

    def test_version_hotfix96_markers(self):
        from tests.release_contracts import assert_release
        assert_release(self, (INTERNAL / "VERSION.txt").read_text(encoding="utf-8"))

    def test_calendar_command_title_after_datetime(self):
        row=parse_calendar_add_command('добавь в расписание 26.08.2026 в 18:00 купить расчёску')
        self.assertEqual(row, {'title':'купить расчёску','date':'26.08.2026','time':'18:00'})

    def test_calendar_command_title_before_datetime(self):
        row=parse_calendar_add_command('внеси в расписание купить расчёску 26.08.2026 в 18:00')
        self.assertEqual(row['title'],'купить расчёску')
        self.assertEqual(row['date'],'26.08.2026'); self.assertEqual(row['time'],'18:00')

    def test_calendar_router_uses_payload_parser(self):
        r=HybridRouter().route('добавь в расписание 26.08.2026 в 18:00 купить расчёску')
        self.assertEqual(r.tool,'google_calendar'); self.assertEqual(r.arguments['action'],'add')
        self.assertEqual(r.arguments['title'],'купить расчёску')

    def test_calendar_mutation_verbs_win_over_today_tomorrow_queries(self):
        router=HybridRouter()
        samples=(
            'запиши в календарь встреча с Иваном завтра 20:15',
            'поставь в расписание завтра на 12:00 купить хлеб',
            'поставить в расписание завтра в 12:00 купить хлеб',
            'записать в расписание завтра в 12:00 купить хлеб',
            'добавь в расписание сегодня в 18:00 купить расчёску',
        )
        for text in samples:
            with self.subTest(text=text):
                r=router.route(text)
                self.assertEqual(r.tool,'google_calendar')
                self.assertEqual(r.arguments['action'],'add')
                self.assertTrue(r.arguments['title'])

    def test_discord_ambiguous_named_channel_is_no_longer_user_control(self):
        r=HybridRouter().route('подключись к каналу Для VIP персон на сервере Бойцовский клуб в Discord')
        # HOTFIX98 supersedes the HOTFIX96 phone/user-control scenario: no self-bot.
        self.assertTrue(r is None or r.tool == 'discord')

    def test_discord_explicit_voice_and_text_fail_closed_as_user_account_automation(self):
        router=HybridRouter()
        voice=router.route('подключись к голосовому каналу Для VIP персон на сервере Бойцовский клуб в Discord')
        text=router.route('напиши сообщение привет в Discord канал neo-music')
        self.assertEqual(voice.arguments['action'],'user_account_automation_unavailable')
        self.assertEqual(text.arguments['action'],'user_account_automation_unavailable')

    def test_discord_explicit_voice_precedes_auto_follow(self):
        src=(J/'integrations'/'background'/'discord_bot.py').read_text(encoding='utf-8')
        start=src.index('async def _resolve_voice')
        block=src[start:src.index('async def _ensure_voice',start)]
        self.assertLess(block.index('explicit_name'), block.index('if auto_follow:'))
        self.assertIn('connect_named_channel',src)

    def test_discord_channel_inventory_and_settings_discovery(self):
        bot=(J/'integrations'/'background'/'discord_bot.py').read_text(encoding='utf-8')
        settings=(J/'ui'/'settings_dialog.py').read_text(encoding='utf-8')
        self.assertIn('list_accessible_channels',bot)
        self.assertIn('Обнаружить каналы',settings)
        self.assertIn('discord_servers_table',settings)

    def test_telegram_home_has_mouse_not_telegram_or_translator(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        start=src.index('def _home_keyboard')
        block=src[start:src.index('def _panel',start)]
        self.assertIn('🖱 Нажать мышью',block); self.assertIn('📧 Почта',block); self.assertIn('📅 Расписание',block)
        self.assertNotIn('📨 Telegram',block); self.assertNotIn('🌐 Переводчик',block)

    def test_telegram_calendar_crud_ui(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        for token in ('✏️ Редактировать','🗑 Удалить','a:cal:delconfirm:','calendar_edit_title','calendar_edit_date','calendar_edit_time'):
            self.assertIn(token,src)

    def test_remote_mouse_grid_and_negative_coordinates_supported(self):
        src=(J/'integrations'/'telegram_mouse.py').read_text(encoding='utf-8')
        self.assertIn('sct.monitors[1:]',src)
        self.assertIn('mon["left"]',src); self.assertIn('mon["top"]',src)
        self.assertIn('Координаты X=',src)
        self.assertIn('{"left","right"}',src)

    def test_remote_mouse_telegram_wizard_accepts_one_line_xy(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        self.assertIn('remote_mouse_xy',src)
        self.assertIn('X Y',src)
        self.assertIn('re.fullmatch',src)

    def test_google_cloud_translation_has_no_runtime_endpoint(self):
        src=(J/'integrations'/'local_translation.py').read_text(encoding='utf-8')
        settings=(J/'ui'/'settings_dialog.py').read_text(encoding='utf-8')
        defaults=(INTERNAL/'config'/'default_settings.json').read_text(encoding='utf-8')
        self.assertIn('LocalOllamaTranslationAdapter',src)
        self.assertNotIn('translation.googleapis.com',src)
        self.assertNotIn('Google Cloud Translation',settings)
        self.assertNotIn('"google_translate"',defaults)

    def test_calendar_shared_oauth_and_briefing_controls(self):
        cal=(J/'integrations'/'google_calendar.py').read_text(encoding='utf-8')
        settings=(J/'ui'/'settings_dialog.py').read_text(encoding='utf-8')
        self.assertIn('integrations.gmail.client_id_encrypted',cal)
        self.assertIn('integrations.gmail.client_secret_encrypted',cal)
        self.assertIn('Gmail и Календарь',settings)
        self.assertIn('Сегодняшние события',settings); self.assertIn('Ближайшие 3 дня',settings); self.assertIn('Ближайшие 31 день',settings)  # 186: UI185-BRIEFING-CALENDAR-MONTH-COPY-01 rolling 31 days

    def test_secretary_long_poll_update_types_and_webhook_recovery(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        for token in ('business_connection','business_message','edited_business_message','deleted_business_messages','getBusinessConnection'):
            self.assertIn(token,src)
        self.assertIn('deleteWebhook',src)
        self.assertIn('drop_pending_updates',src)

    def test_secretary_notifications_do_not_depend_on_remote_action_mirroring(self):
        src=(J/'integrations'/'telegram_remote.py').read_text(encoding='utf-8')
        self.assertIn('respect_action_mirror: bool = True',src)
        self.assertIn('format_telegram_message_notification(self.config, row), respect_action_mirror=False',src)

    def test_business_update_is_stored_and_notified_with_remote_mirror_off(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=DummyConfig(Path(td),{
                'integrations.telegram.messages_enabled':True,
                'integrations.telegram.messages_notifications_enabled':True,
                'integrations.telegram.messages_notification_detail':'sender_text',
                'integrations.telegram.mirror_actions_to_desktop':False,
            })
            notices=[]
            remote=TelegramRemoteController(cfg,notices.append)
            remote._handle_update({'business_connection':{
                'id':'bc1','user':{'id':1,'first_name':'Owner'},'user_chat_id':1,
                'is_enabled':True,'rights':{'can_reply':True},
            }})
            remote._handle_update({'business_message':{
                'business_connection_id':'bc1','message_id':7,'date':int(time.time()),
                'chat':{'id':55,'type':'private'},'from':{'id':2,'first_name':'Иван'},
                'text':'Привет',
            }})
            self.assertTrue(remote._message_center.connection_status()['connected'])
            self.assertEqual(remote._message_center.latest()['text'],'Привет')
            self.assertTrue(notices)
            self.assertIn('Иван',notices[-1]); self.assertIn('Привет',notices[-1])

    def test_telegram_outbound_allowlist_parses_multiword_inflection(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=DummyConfig(Path(td),{
                'integrations.telegram.outbound_allowed_people':['Иван Грозный'],
                'integrations.telegram.outbound_allowed_groups':['OPG'],
            })
            center=TelegramMessageCenter(cfg)
            parsed=center.parse_outbound_target_and_text('Ивану Грозному что буду через пять минут',kind='person')
            self.assertEqual(parsed,('Иван Грозный','буду через пять минут'))
            group=center.parse_outbound_target_and_text('OPG сообщение сбор в 20:00',kind='group')
            self.assertEqual(group,('OPG','сбор в 20:00'))

    def test_telegram_outbound_rejects_unlisted_target(self):
        with tempfile.TemporaryDirectory() as td:
            cfg=DummyConfig(Path(td),{'integrations.telegram.outbound_allowed_people':['Иван Грозный']})
            center=TelegramMessageCenter(cfg)
            with self.assertRaises(RuntimeError):
                center._resolve_allowed_name('Пётр',kind='person')

    def test_group_discovery_storage(self):
        with tempfile.TemporaryDirectory() as td:
            c=TelegramMessageCenter(DummyConfig(Path(td),{}))
            row=c.record_bot_group({'id':-1001,'title':'OPG','type':'supergroup'})
            self.assertEqual(row['title'],'OPG'); self.assertEqual(c.bot_groups()[0]['chat_id'],'-1001')

    def test_briefing_calendar_is_registered(self):
        src=(J/'tools'/'integrations.py').read_text(encoding='utf-8')
        self.assertIn('integrations.google_calendar.include_in_briefing',src)
        self.assertIn('briefing_period',src)
        self.assertIn('google_calendar',src)


if __name__ == '__main__':
    unittest.main()

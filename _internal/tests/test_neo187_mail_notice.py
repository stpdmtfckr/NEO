"""NEO 187 REC-REQ-MAIL-LAYOUT / REC-SEMANTIC-SOURCE-01: localized mail notice keeps data."""
from types import SimpleNamespace

import pytest

from jarvis.integrations.mail_services import MailFolderStatus, MailStatus, format_mail_notification

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


class Cfg:
    def __init__(self, code, **values):
        self.values = {'app.language': code, **values}
        self.data = {'app': {'language': code}}

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.mark.parametrize('code', CODES)
def test_counters_sender_and_colon_subject_survive_every_language(code):
    status = {'unread': 12, 'unread_today': 3, 'recent24h': 5, 'account': 'me@example.invalid'}
    text = format_mail_notification(Cfg(code), 'gmail', status,
                                    [{'sender': 'Иван Петров', 'subject': 'Отчёт: итоги квартала'}])
    lines = text.splitlines()
    assert lines[0] == '✉️ Gmail · me@example.invalid'
    assert any(line.endswith(': 12') for line in lines) and any(line.endswith(': 3') for line in lines)
    assert '• Иван Петров — Отчёт: итоги квартала' in lines              # default shows sender + subject
    if code != 'ru':
        assert 'непрочитанных' not in text.casefold()


def test_status_compact_carries_unread_today():
    status = MailStatus('gmail', unread=4, unread_today=2, folders=[MailFolderStatus('Inbox', unread=4, unread_today=2)])
    data = status.compact()
    assert data['unread_today'] == 2 and data['folders'][0]['unread_today'] == 2

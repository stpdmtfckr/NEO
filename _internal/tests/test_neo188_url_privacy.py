"""NEO 188 NEO-PRIVACY-003 / TZ-SENSITIVE-URL-DERIVED-COPY-PROTECTION-01 (synthetic URLs only)."""
import json
import logging
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

from jarvis.runtime.diagnostics import RedactingFormatter, clip_redacted, collect_logs, redact_credentials, redact_url

SECRET = "S3CR3TVALUE0123456789abcdefABCDEF"


@pytest.mark.parametrize('url', [
    f'https://cdn.example.test/f.zip?sig={SECRET}',
    f'https://cdn.example.test/f.zip?Signature={SECRET}&v=1',
    f'https://b.s3.example.test/a?X-Amz-Algorithm=AWS4&X-Amz-Credential={SECRET}&X-Amz-Signature={SECRET}',
    f'https://storage.example.test/x?X-Goog-Signature={SECRET}',
    f'https://example.test/?%73ignature={SECRET}&a=1',
    f'https://example.test/?token=1&TOKEN={SECRET}',
    f'https://user:{SECRET}@example.test/path',
    f'https://example.test/cb#access_token={SECRET}&state=x',
    f'https://example.test/share/{SECRET}{SECRET}/file.pdf',
    f'https://example.test/x?unknownparam={SECRET}{SECRET}',
])
def test_urlp01_03_secret_parts_never_survive(url):
    cleaned = redact_credentials(f'открой {url} пожалуйста')
    assert SECRET not in cleaned and '[REDACTED' in cleaned and 'example.test' in cleaned


def test_urlp08_benign_urls_keep_their_diagnostic_value():
    for url in ('https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf',
                'https://example.test/docs/page?lang=ru&page=2#section-3', 'https://example.test/'):
        assert redact_credentials(url) == url


def test_malformed_url_keeps_only_a_safe_minimum():
    assert SECRET not in redact_url(f'https://[bad{SECRET}/x')


@pytest.mark.parametrize('limit', [180, 320, 4000])
def test_urlp04_redaction_before_truncation_at_boundaries(limit):
    for shift in range(-12, 13, 3):
        text = 'x' * max(0, limit - 40 + shift) + f' https://example.test/f?signature={SECRET} tail'
        cut = clip_redacted(text, limit)
        assert SECRET[:8] not in cut and len(cut) <= limit + 3


def test_logs_are_redacted_by_the_formatter():
    record = logging.LogRecord('neo', logging.INFO, __file__, 1, 'GET https://x.test/a?X-Amz-Signature=%s', (SECRET,), None)
    assert SECRET not in RedactingFormatter('%(message)s').format(record)


def test_urlp07_diagnostic_zip_tail_does_not_start_with_a_keyless_secret(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    # A tail cut that would begin in the middle of «…signature=<secret>» on the first line.
    line = f'GET https://x.test/f?signature={SECRET}\n'
    (logs / 'neo.log').write_text(('z' * 3 + line) * 120000, encoding='utf-8')
    archive = collect_logs(tmp_path, tmp_path / 'out.zip')
    with zipfile.ZipFile(archive) as zipped:
        payload = zipped.read('logs/neo.log').decode('utf-8')
    assert SECRET not in payload and SECRET[5:] not in payload


def test_urlp06_action_audit_is_cleaned_on_write_and_on_read(tmp_path):
    from jarvis.database import Database
    database = Database(tmp_path / 'neo.db')
    arguments = {'action': 'open_url', 'url': f'https://x.test/f?sig={SECRET}', 'nested': {'u': f'https://x.test/?token={SECRET}'}}
    database.log_action('browser', dict(arguments), 'low', True, json.dumps({'url': f'https://x.test/?code={SECRET}'}))
    row = database.recent_actions(1)[0]
    assert SECRET not in json.dumps(row, ensure_ascii=False)
    assert arguments['url'].endswith(SECRET)                 # the operational value itself is untouched
    with sqlite3.connect(tmp_path / 'neo.db') as raw:          # legacy row written before 188
        raw.execute("INSERT INTO action_log(created_at, tool_name, arguments_json, risk, success, result_text) VALUES('x','browser',?, 'low', 1, ?)",
                    (json.dumps({'url': f'https://x.test/?signature={SECRET}'}), f'opened https://x.test/?token={SECRET}'))
    legacy = database.recent_actions(1)[0]
    assert SECRET not in json.dumps(legacy, ensure_ascii=False)


def test_urlp05_journal_readback_returns_only_cleaned_text(tmp_path):
    from jarvis.runtime.conversation_journal import ConversationJournal
    config = SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda key, default=None: {
        'dialogue.memory.enabled': True}.get(key, default))
    journal = ConversationJournal(config)
    if not journal.enabled():
        pytest.skip('journal disabled by default in this configuration')
    journal.record_turn('desktop', f'открой https://x.test/f?sig={SECRET}', 'Открыл ссылку.')
    with sqlite3.connect(journal.path) as raw:                  # a legacy unredacted row
        raw.execute("INSERT INTO turns(scope,created,role,text,outcome) VALUES('desktop', strftime('%s','now'), 'user', ?, '')",
                    (f'старое https://x.test/f?X-Amz-Signature={SECRET}',))
    rows = journal.recent('desktop')
    assert rows and SECRET not in json.dumps(rows, ensure_ascii=False)


def test_context_memory_redacts_before_clipping():
    from jarvis.runtime.context_memory import _redact_text
    text = 'y' * 3990 + f' https://x.test/f?signature={SECRET}'
    assert SECRET[:6] not in _redact_text(text)

"""NEO 187 REC-PRIVACY-SOURCE-DATA-01: secrets are removed before logging or cutting."""
import logging
from types import SimpleNamespace

from jarvis.runtime.screen_help172 import Reading, build_query, scrub
from jarvis.ui.settings_dialog import SettingsDialog

KEY = 'AIzaSyQA' + 'x' * 31


def test_known_key_never_reaches_log_or_cut_ui_line(caplog):                    # R1
    fake = SimpleNamespace(_t=lambda text: text)
    trace = 'RuntimeError: ' + 'y' * 280 + ' key echoed: ' + KEY + ' tail'
    with caplog.at_level(logging.INFO):
        line = SettingsDialog._short_network_error184(fake, trace, secrets=(KEY,))
    assert KEY[:12] not in line and KEY[:12] not in caplog.text
    assert len(line) <= 300


def test_screen_help_scrub_uses_general_credential_policy():                    # R3
    assert 'sk_live' not in scrub('Error: api_key=sk_live_ab_cd failed')
    reading = Reading(lines=['Fatal: api_key=sk_live_ab_cd ' + 'z' * 200], app='Tool')
    assert 'sk_live' not in build_query(reading, 'как исправить', 'ru')

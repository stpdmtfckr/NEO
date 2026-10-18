"""Pure date formatting and extracted hotkey notification behavior, no Qt needed."""
from __future__ import annotations
import ast
from datetime import datetime
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jarvis.integrations.daily_briefing import date_and_time_line, greeting_and_wish, SUPPORTED_LANGUAGES


def test_exact_reported_russian_date_and_time():
    assert date_and_time_line(datetime(2026,9,25,1,19),'ru') == 'Сегодня пятница, 25 сентября 2026 года. Сейчас 01:19.'


@pytest.mark.parametrize('month,expected',list(enumerate(['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'],1)))
def test_russian_months_and_year(month,expected):
    out=date_and_time_line(datetime(2026,month,1,0,0),'ru',include_time=False)
    assert f'1 {expected} 2026 года.' in out
    assert 'Сейчас' not in out and '00:00' not in out


@pytest.mark.parametrize('code',SUPPORTED_LANGUAGES)
def test_localized_time_can_be_omitted(code):
    now=datetime(2024,2,29,23,59)
    yes=date_and_time_line(now,code)
    no=date_and_time_line(now,code,include_time=False)
    assert no.endswith('.') and yes.startswith(no+' ')
    assert '2024' in no and '23:59' not in no and '23:59' in yes
    if code!='ru': assert 'Сейчас' not in yes and 'года' not in yes


def test_greeting_reuses_formatter_not_hardcoded_date(tmp_path):
    values={'user_name_enabled':False,'app.language':'ru','briefing.include_time':True}
    config=SimpleNamespace(paths=SimpleNamespace(root=tmp_path), get=lambda k,d=None:values.get(k,d))
    out=greeting_and_wish(config,datetime(2026,9,25,1,19))
    assert 'Сегодня пятница, 25 сентября 2026 года. Сейчас 01:19.' in out
    assert '2026 в' not in out
    values['briefing.include_time']=False
    assert '01:19' not in greeting_and_wish(config,datetime(2026,9,25,1,19))


def test_hotkey_conflicts_are_logged_and_retained_but_never_sent_to_chat(caplog):
    path=Path(__file__).resolve().parents[1]/'jarvis/ui/main_window.py'
    module=ast.parse(path.read_text(encoding='utf-8'))
    cls=next(x for x in module.body if isinstance(x,ast.ClassDef) and x.name=='MainWindow')
    fn=next(x for x in cls.body if isinstance(x,ast.FunctionDef) and x.name=='_notify_hotkey_conflict')
    isolated=ast.Module(body=[fn],type_ignores=[])
    ns={'logger':logging.getLogger('neo151.test.hotkeys')}
    exec(compile(ast.fix_missing_locations(isolated),str(path),'exec'),ns)
    window=SimpleNamespace(_append_assistant=Mock(side_effect=AssertionError('no chat')),
                           _set_status=Mock(side_effect=AssertionError('no unsolicited status')))
    with caplog.at_level(logging.WARNING):
        for key in ('Ctrl+Alt+M','F12','Ctrl+Alt+M'):
            ns['_notify_hotkey_conflict'](window,key)
    assert window._hotkey_conflicts=={'Ctrl+Alt+M','F12'}
    assert window._reported_hotkey_conflicts=={'Ctrl+Alt+M','F12'}
    window._append_assistant.assert_not_called()
    assert len(caplog.records)==2

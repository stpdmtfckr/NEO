"""Calendar filters must change actual search results, not just the query text."""
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from jarvis.runtime.file_search import parse_file_time, search_files
from jarvis.runtime.cancellation import cancellation_manager


@pytest.mark.parametrize("today,yesterday", [
    ("сегодня", "вчера"), ("today", "yesterday"), ("heute", "gestern"),
    ("aujourd’hui", "hier"), ("hoy", "ayer"), ("oggi", "ieri"), ("hoje", "ontem"),
])
def test_actual_results_follow_local_day_in_all_languages(tmp_path, today, yesterday):
    cancellation_manager().reset()
    now = datetime.now().astimezone()
    dates = (now, now-timedelta(days=1), now-timedelta(days=2))
    for i, when in enumerate(dates):
        path = tmp_path / f"report-{i}.txt"
        path.write_text("content")
        os.utime(path, (when.timestamp(), when.timestamp()))
    with patch("jarvis.runtime.file_index.index_enabled", return_value=False):
        assert [r['name'] for r in search_files(f"report {today}", roots=[tmp_path])] == ["report-0.txt"]
        assert [r['name'] for r in search_files(f"report {yesterday}", roots=[tmp_path])] == ["report-1.txt"]


@pytest.mark.parametrize("query", [
    '"Yesterday"', "'hier'", '«вчера»', '“oggi”', "yesterday.txt", "today-report.docx",
    "reports/ontem", r"C:\reports\gestern.txt", "устройство.txt", "вечеринка.txt", "vorgestern.txt",
])
def test_literal_names_and_substrings_are_never_dates(query):
    clean, temporal = parse_file_time(query, now=datetime(2026, 9, 18))
    assert clean == query
    assert not temporal.constrained
    assert not temporal.recent


@pytest.mark.parametrize("phrase", ["вчера вечером", "yesterday evening", "gestern am Abend", "hier soir", "ayer por la noche", "ieri sera", "ontem à noite"])
def test_date_and_daypart_constraints_compose(phrase):
    _, rule = parse_file_time(phrase, now=datetime(2026, 9, 18, 10))
    assert rule.matches(datetime(2026, 9, 17, 19).timestamp())
    assert not rule.matches(datetime(2026, 9, 17, 10).timestamp())
    assert not rule.matches(datetime(2026, 9, 16, 19).timestamp())


@pytest.mark.parametrize("phrase", ["на прошлой неделе", "last week", "letzte Woche", "la semaine dernière", "la semana pasada", "la settimana scorsa", "semana passada"])
def test_previous_calendar_week_is_not_rolling_seven_days(phrase):
    _, rule = parse_file_time(phrase, now=datetime(2026, 9, 18))
    assert rule.matches(datetime(2026, 9, 7, 0).timestamp())
    assert rule.matches(datetime(2026, 9, 13, 23, 59).timestamp())
    assert not rule.matches(datetime(2026, 9, 14, 0).timestamp())


def test_quoted_title_stays_searchable_when_another_date_is_present(tmp_path):
    cancellation_manager().reset()
    for name in ("Yesterday.txt", "unrelated.txt"):
        (tmp_path/name).write_text("text")
    with patch("jarvis.runtime.file_index.index_enabled", return_value=False):
        rows = search_files('find "Yesterday" from today', roots=[tmp_path])
    assert [r['name'] for r in rows] == ["Yesterday.txt"]


def test_conflicting_dates_cannot_silently_choose_one():
    _, rule = parse_file_time("report today yesterday", now=datetime(2026, 9, 18))
    assert not rule.matches(datetime(2026, 9, 18).timestamp())
    assert not rule.matches(datetime(2026, 9, 17).timestamp())


@pytest.mark.skipif(not hasattr(time, 'tzset'), reason='Test host cannot switch process local timezone')
def test_local_date_and_dst_boundary_do_not_use_utc_day():
    old = os.environ.get('TZ')
    try:
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        _, rule = parse_file_time('today', now=datetime(2026, 3, 8, 10))
        # The local spring-forward day has 23 hours; late evening is UTC tomorrow.
        assert rule.matches(datetime(2026, 3, 8, 23, 59).timestamp())
        assert not rule.matches(datetime(2026, 3, 9, 0).timestamp())
        assert not rule.matches(datetime(2026, 3, 7, 23, 59).timestamp())
    finally:
        if old is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old
        time.tzset()

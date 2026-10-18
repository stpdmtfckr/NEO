"""NEO 187 REC-SETTINGS-SEARCH-SOURCE-02 / TZ-SEARCH-01: typos, exact-first, fresh path."""
from PySide6.QtCore import QCoreApplication

from jarvis.ui.settings_dialog import _edit_distance187
from tests.test_neo186_settings_groups import app, dialog  # noqa: F401


def test_bounded_edit_distance():
    assert _edit_distance187('чувствителность', 'чувствительность', 2) == 1
    assert _edit_distance187('микрфоон', 'микрофон', 2) <= 2         # one swap + one drop
    assert _edit_distance187('абвгд', 'ежзик', 1) > 1


def test_typo_finds_the_setting_page(dialog):
    tabs = dialog.tabs
    tabs._search.setText('чувствителность')
    tabs._apply_search()
    assert not tabs._buttons[dialog.page_index186('microphone')].isHidden()
    assert tabs._no_results.isHidden()


def test_exact_matches_win_over_typo_matches(dialog):
    tabs = dialog.tabs
    tabs._search.setText('микрофон')
    assert not tabs._search_mode187('микрофон')                            # exact exists → no fuzzy
    tabs._search.setText('zzzzqqq')
    tabs._apply_search()
    assert not tabs._no_results.isHidden()


def test_path_follows_the_new_query_on_the_same_page(dialog):
    tabs = dialog.tabs
    tabs._search.setText('Профиль производительности')
    tabs._jump_to_first_hit()
    QCoreApplication.processEvents()
    first = tabs._search_path186.text()
    assert first.startswith('Система → Дополнительно')
    tabs._search.setText('Язык')
    tabs._apply_search()
    if tabs._stack.currentIndex() == dialog.page_index186('advanced'):
        assert tabs._search_path186.isHidden() or tabs._search_path186.text() != first

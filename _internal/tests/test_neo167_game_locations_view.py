from jarvis.runtime.game_locations import _collapse
from jarvis.tools.desktop_features import _grouped_locations


def row(kind, path, confirmed=False):
    return {'kind': kind, 'path': path, 'confirmed': confirmed, 'source': 'local_directory_search'}


def test_nested_mod_folders_fold_into_the_mods_folder():
    base = '/games/Darktide'
    rows = [row('install', base, True), row('mods', base + '/mods')]
    rows += [row('mods', base + '/mods/' + name) for name in ('dmf', 'base', 'NumericUI')]
    rows += [row('mods', base + '/mods/dmf/scripts'), row('mods', base + '/mods/base/function')]
    out = _collapse(rows)
    assert [r['path'] for r in out] == [base, base + '/mods']
    assert sorted(out[1]['children']) == ['NumericUI', 'base', 'dmf']


def test_related_folder_that_only_contains_results_is_dropped_but_verified_rows_stay():
    root = '/appdata/Fatshark/Darktide'
    rows = [row('config', root + '/user_settings.config', True), row('related', root),
            row('logs', root + '/console_logs', True), row('logs', root + '/console_logs/old', True)]
    out = _collapse(rows)
    assert [r['kind'] for r in out] == ['config', 'logs', 'logs']


def test_grouped_text_has_headings_marks_and_no_source_tags():
    rows = [row('saves', '/s/remote'), row('install', '/g', True)]
    text = _grouped_locations(rows, {'install': 'установка', 'saves': 'сохранения'}, 'ru')
    assert text.index('Установка') < text.index('Сохранения')
    assert '  ✓ /g' in text and '  • /s/remote' in text
    assert 'local_directory_search' not in text and 'подтверждено' in text

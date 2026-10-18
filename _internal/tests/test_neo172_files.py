"""NEO 172: «разбери загрузки» (plan → «да» → moved → «верни как было»), «найди большие файлы»."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from jarvis.runtime import tidy172


def _downloads(tmp_path):
    folder = tmp_path / 'Downloads'
    folder.mkdir()
    for name in ('фото.jpg', 'скан.PNG', 'договор.pdf', 'отчёт.docx', 'архив.zip', 'setup.exe', 'клип.mp4', 'песня.mp3', 'data.bin',
                 'большой.iso.crdownload', 'desktop.ini', '~$временный.docx'):
        (folder / name).write_bytes(b'x' * 10)
    (folder / 'Старая папка').mkdir()
    (folder / 'Старая папка' / 'внутри.txt').write_text('не трогать', encoding='utf-8')
    (folder / 'Документы').mkdir()
    (folder / 'Документы' / 'договор.pdf').write_bytes(b'already here')
    return folder


def test_plan_moves_nothing_and_groups_by_kind(tmp_path):
    folder = _downloads(tmp_path)
    before = sorted(p.name for p in folder.iterdir())
    plan = tidy172.plan(folder, 'ru')
    assert sorted(p.name for p in folder.iterdir()) == before                 # a plan only
    assert plan.counts == {'images': 2, 'documents': 2, 'archives': 1, 'installers': 1, 'video': 1, 'music': 1, 'other': 1}
    assert plan.skipped == 3                                                   # partial download, desktop.ini, ~$ file
    targets = {Path(m.source).name: Path(m.target) for m in plan.moves}
    assert targets['договор.pdf'] == folder / 'Документы' / 'договор (2).pdf'    # never overwrites
    assert targets['setup.exe'].parent.name == 'Установщики' and 'Старая папка' not in targets
    text = tidy172.plan_text(plan, 'ru')
    assert text.startswith(f'Вот план для папки «{folder}» (файлов: 9). Ничего ещё не перемещено:')
    assert '• Изображения: 2 — скан.PNG, фото.jpg' in text and 'Не трогаю: 3' in text and text.endswith('«верни как было».')


def test_apply_and_undo_restore_everything_even_after_restart(tmp_path):
    folder = _downloads(tmp_path)
    data = tmp_path / 'data'
    plan = tidy172.plan(folder, 'en')
    assert (folder / 'Documents').exists() is False
    moved, errors = tidy172.apply(plan, data)
    assert moved == 9 and errors == []
    assert (folder / 'Images' / 'фото.jpg').is_file() and not (folder / 'фото.jpg').exists()
    assert (folder / 'Старая папка' / 'внутри.txt').is_file()
    assert tidy172.journal_path(data).is_file()
    restored, errors, where = tidy172.undo(data)                                 # a fresh call: only the journal on disk
    assert restored == 9 and errors == [] and where == str(folder)
    assert (folder / 'фото.jpg').is_file() and (folder / 'договор.pdf').is_file()
    assert not (folder / 'Images').exists()                                      # empty folders removed again
    assert (folder / 'Документы' / 'договор.pdf').read_bytes() == b'already here'
    assert tidy172.undo(data) == (0, [], '')


def test_desktop_keeps_shortcuts(tmp_path):
    desk = tmp_path / 'Desktop'
    desk.mkdir()
    (desk / 'Игра.lnk').write_bytes(b'l')
    (desk / 'сайт.url').write_bytes(b'u')
    (desk / 'заметка.txt').write_text('x', encoding='utf-8')
    plan = tidy172.plan(desk, 'ru', desktop=True)
    assert [Path(m.source).name for m in plan.moves] == ['заметка.txt'] and plan.skipped == 2


def test_big_files_lists_the_largest_and_never_deletes(tmp_path):
    root = tmp_path / 'home'
    (root / 'Videos' / 'deep').mkdir(parents=True)
    (root / 'AppData').mkdir()
    for path, size in (('Videos/deep/film.mkv', 900), ('Videos/small.mp4', 10), ('iso.iso', 500), ('AppData/cache.bin', 2000)):
        with open(root / path, 'wb') as handle:
            handle.truncate(size)
    rows = tidy172.big_files([root], min_size=100)
    assert [(size, Path(path).name) for size, path in rows] == [(900, 'film.mkv'), (500, 'iso.iso')]      # AppData skipped
    assert all(Path(p).exists() for _s, p in rows)
    assert tidy172.size_text(3 * 1024 ** 3, 'ru') == '3,0 ГБ' and tidy172.size_text(512 * 1024 ** 2, 'en') == '512 MB'


def test_through_the_agent(tmp_path, monkeypatch):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    folder = _downloads(tmp_path)
    monkeypatch.setattr(tidy172, 'known_folder', lambda key: folder if key == 'downloads' else tmp_path / 'none')
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            session.reset_case('ru')
            plan = session.agent.handle('разбери загрузки')
            assert plan.tool_details == ['files.tidy_plan'] and (folder / 'фото.jpg').exists()
            done = session.agent.handle('да')
            assert done.text.startswith('Готово: разложил 9 файлов') and (folder / 'Изображения' / 'фото.jpg').exists()
            back = session.agent.handle('верни как было')
            assert back.text.startswith('Вернул 9 файлов') and (folder / 'фото.jpg').exists()
            session.agent.handle('разбери загрузки')
            assert session.agent.handle('нет').text == 'Хорошо, ничего не трогаю.' and (folder / 'фото.jpg').exists()
            with open(folder / 'большой.iso', 'wb') as handle:
                handle.truncate(60 * 1024 * 1024)
            big = session.agent.handle('найди большие файлы в загрузках')
            assert big.text.startswith('Самые большие файлы (Downloads):\n1. большой.iso — 60,0 МБ')
            assert session.calls == []
        finally:
            session.finish()


@pytest.mark.parametrize('text', ['разбери загрузки', 'наведи порядок в загрузках', 'рассортируй рабочий стол', 'sort my downloads',
                                  'tidy up my desktop', 'räume meine downloads auf', 'range mes téléchargements', 'ordena mis descargas',
                                  'riordina i miei download', 'organiza as minhas transferências'])
def test_tidy_phrases(text):
    from jarvis.runtime.everyday172 import _TIDY, _n
    assert _TIDY.match(_n(text))

"""NEO 188 NEO-OFFICE-001 / TZ-OFFICE-EDITABLE-CHART-01 (synthetic workbooks; nothing is opened in Office)."""
import zipfile
from types import SimpleNamespace

import pytest

openpyxl = pytest.importorskip('openpyxl')

from jarvis.runtime import office_chart188 as chart
from jarvis.runtime import office178

CONFIG = SimpleNamespace(get=lambda key, default=None: default)


@pytest.fixture
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(office178, 'user_folders', lambda: [tmp_path])
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'Итоги'
    ws.append(['Месяц', 'Выручка', 'Расходы'])
    for month, a, b in (('Янв', 100, 60), ('Фев', 120, 70), ('Мар', 90, 65)):
        ws.append([month, a, b])
    path = tmp_path / 'Продажи.xlsx'
    wb.save(path)
    return path


def args(**extra):
    return {'action': 'chart', 'name': 'Продажи.xlsx', 'sheet': 'Итоги', 'range': 'A1:C4', 'chart': 'bar', **extra}


def test_och01_preview_shows_everything_and_writes_nothing(book, tmp_path):
    before = book.read_bytes()
    spec = chart.preview(CONFIG, args(), 'ru')
    assert spec['ok'] and 'лист «Итоги», A1:C4' in spec['text'] and '«Выручка», «Расходы»' in spec['text']
    assert 'исходный файл не изменяется' in spec['text'] and spec['destination'].endswith('Продажи — диаграмма.xlsx')
    assert sorted(p.name for p in tmp_path.iterdir()) == ['Продажи.xlsx'] and book.read_bytes() == before


def test_och02_creates_a_new_workbook_with_an_editable_chart_and_keeps_the_source(book):
    before = book.read_bytes()
    spec = chart.preview(CONFIG, args(), 'ru')
    ok, text, data = chart.create(CONFIG, args(fingerprint=spec['fingerprint'], destination=spec['destination'], path=spec['path']), 'ru')
    assert ok and data['outcome'] == 'created' and 'Исходный файл не менял' in text
    assert book.read_bytes() == before
    with zipfile.ZipFile(data['path']) as archive:
        names = archive.namelist()
        assert any(n.startswith('xl/charts/chart') for n in names)
        xml = archive.read(next(n for n in names if n.startswith('xl/charts/chart'))).decode('utf-8')
        assert 'barChart' in xml and "'Итоги'!" in xml and '<numRef>' in xml   # cell references, not a picture
    copy = openpyxl.load_workbook(data['path'])
    assert [c.value for c in copy.active[1]] == ['Месяц', 'Выручка', 'Расходы'] and copy.active['B3'].value == 120


def test_och03_source_changed_after_preview_creates_nothing(book):
    spec = chart.preview(CONFIG, args(), 'ru')
    wb = openpyxl.load_workbook(book)
    wb.active['B2'] = 999
    wb.save(book)
    ok, text, data = chart.create(CONFIG, args(fingerprint=spec['fingerprint'], destination=spec['destination'], path=spec['path']), 'ru')
    assert not ok and data['outcome'] == 'source_changed'
    assert not chart.Path(spec['destination']).exists()


def test_och04_existing_destination_is_never_overwritten(book):
    spec = chart.preview(CONFIG, args(), 'ru')
    chart.Path(spec['destination']).write_text('mine', encoding='utf-8')
    ok, _, data = chart.create(CONFIG, args(fingerprint=spec['fingerprint'], destination=spec['destination'], path=spec['path']), 'ru')
    assert not ok and chart.Path(spec['destination']).read_text(encoding='utf-8') == 'mine'


@pytest.mark.parametrize('mutate,needle', [
    (lambda ws: ws.__setitem__('B3', '=B2*2'), 'формула'),
    (lambda ws: ws.__setitem__('C2', None), 'пуста'),
    (lambda ws: ws.__setitem__('B4', '90'), 'не число'),
    (lambda ws: ws.__setitem__('B2', True), 'не число'),
    (lambda ws: ws.__setitem__('C1', None), 'пустой заголовок'),
    (lambda ws: ws.merge_cells('A3:A4'), 'объединённые'),
])
def test_och05_unsupported_data_is_refused_with_the_exact_cell(book, mutate, needle):
    wb = openpyxl.load_workbook(book)
    mutate(wb.active)
    wb.save(book)
    spec = chart.preview(CONFIG, args(), 'ru')
    assert not spec['ok'] and needle in spec['text']


def test_och06_missing_parameters_are_asked_and_bad_ranges_refused(book):
    spec = chart.preview(CONFIG, {'action': 'chart', 'name': 'Продажи.xlsx', 'range': 'A1:C4'}, 'ru')
    assert not spec['ok'] and spec.get('clarify') and 'лист' in spec['text'] and 'тип' in spec['text']
    assert not chart.preview(CONFIG, args(range='A1:A4'), 'ru')['ok']
    assert not chart.preview(CONFIG, args(range='A:C'), 'ru')['ok']
    assert not chart.preview(CONFIG, args(sheet='Нет'), 'ru')['ok']


def test_och07_same_name_in_two_folders_asks(book, tmp_path, monkeypatch):
    other = tmp_path / 'sub'
    other.mkdir()
    (other / 'Продажи.xlsx').write_bytes(book.read_bytes())
    spec = chart.preview(CONFIG, args(), 'ru')
    assert not spec['ok'] and 'несколько' in spec['text']


def test_och08_macro_and_csv_files_are_not_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(office178, 'user_folders', lambda: [tmp_path])
    (tmp_path / 'a.xlsm').write_bytes(b'PK')
    (tmp_path / 'b.csv').write_text('a,b', encoding='utf-8')
    assert not chart.preview(CONFIG, args(name='a.xlsm'), 'en')['ok']
    assert not chart.preview(CONFIG, args(name='b.csv'), 'en')['ok']


@pytest.mark.parametrize('code,phrase', [
    ('ru', 'В файле Продажи.xlsx, лист Итоги, по A1:B13 создай столбчатую диаграмму в новом файле'),
    ('en', 'Create a bar chart in a new file from A1:B13 on sheet Summary in Sales.xlsx'),
    ('de', 'Erstelle aus A1:B13 im Blatt Übersicht der Datei Umsatz.xlsx ein Säulendiagramm in einer neuen Datei'),
    ('fr', 'Crée un graphique en colonnes dans un nouveau fichier à partir de A1:B13, feuille Résumé, fichier Ventes.xlsx'),
    ('es', 'Crea un gráfico de columnas en un archivo nuevo con A1:B13 de la hoja Resumen de Ventas.xlsx'),
    ('it', 'Crea un grafico a colonne in un nuovo file usando A1:B13 del foglio Riepilogo di Vendite.xlsx'),
    ('pt', 'Cria um gráfico de colunas num novo ficheiro com A1:B13 da folha Resumo de Vendas.xlsx'),
])
def test_och_phrases_in_seven_languages(code, phrase):
    tool, parsed = chart.parse(phrase, code)
    assert tool == 'office' and parsed['range'] == 'A1:B13' and parsed['chart'] == 'bar' and parsed['name'].endswith('.xlsx') and parsed['sheet']


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_och_reasons_are_localized(code):
    text = chart.reason('missing_file', code, name='x.xlsx')
    assert 'x.xlsx' in text and (code in {'en'} or 'the file' not in text)


def test_och_agent_flow_preview_then_yes_creates_once(book, tmp_path, monkeypatch):
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = AtlasSession(tmp_path / 'run')
        try:
            asked = session.agent.handle('В файле Продажи.xlsx, лист Итоги, по A1:C4 создай столбчатую диаграмму в новом файле')
            assert asked.pending is not None and 'Предпросмотр диаграммы' in asked.text and 'исходный файл не изменяется' in asked.text
            assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == '.xlsx') == ['Продажи.xlsx']
            session._permitted = [('office', dict(asked.pending.arguments))]
            done = session.agent.confirm_pending(True)
            assert done.success and 'редактируемой диаграммой' in done.text
            created = [p.name for p in tmp_path.iterdir() if p.suffix == '.xlsx']
            assert set(created) == {'Продажи.xlsx', 'Продажи — диаграмма.xlsx'}
        finally:
            session.finish()

"""NEO 188 NEO-DOC-003: aligned translation of one plain .docx into a separate copy."""
import hashlib
import io
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from jarvis.runtime import doc_translate188 as dt
from jarvis.runtime import doc_translate_control188 as ctl
from jarvis.testing.atlas_session import AtlasSession
from jarvis.testing.isolation import external_io_blocked

CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
LONG = ('Это длинное предложение номер один для проверки разбиения. ' * 40).strip()


def p(text, style=''):
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ''
    return f'<w:p>{ppr}<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def cell(text):
    return f'<w:tc><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>' if text else '<w:tc><w:p/></w:tc>'


def docx(body, *, extra=None, rels='', types_extra=''):
    files = {
        '[Content_Types].xml': f'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               f'<Default Extension="xml" ContentType="application/xml"/>{types_extra}</Types>',
        '_rels/.rels': '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        'word/document.xml': f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}<w:sectPr/></w:body></w:document>',
        'word/_rels/document.xml.rels': f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>',
        'word/styles.xml': '<?xml version="1.0"?><w:styles ' + W + '/>',
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data if isinstance(data, bytes) else data.encode('utf-8'))
    return buffer.getvalue()


BODY = (p('Отчёт за квартал', 'Heading1') + p('Выручка выросла на 15% по данным https://example.com/report.')
        + '<w:tbl><w:tr>' + cell('Регион') + cell('') + '</w:tr><w:tr>' + cell('Север') + cell('Север') + '</w:tr></w:tbl>'
        + '<w:p><w:fldSimple w:instr="DATE"><w:r><w:t>01.01.2026</w:t></w:r></w:fldSimple></w:p>'
        + p(LONG) + p('Север'))


def fake(source, target):
    return lambda text: '[EN] ' + text


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / 'Отчёт.docx'
    path.write_bytes(docx(BODY, rels='<Relationship Id="r9" Type="x" Target="http://example.com" TargetMode="External"/>'))
    return path


def test_dtr02_fragments_keep_body_order_and_one_status_each(doc):
    inv = dt.inspect(str(doc), doc.read_bytes())
    ids = [f.ident for f in inv.fragments]
    assert ids == ['p1', 'p2', 't1.r1.c1.p0', 't1.r2.c1.p0', 't1.r2.c2.p0', 'p3', 'p4', 'p5']
    assert inv.fragments[0].kind == 'heading' and inv.fragments[2].kind == 'cell'
    assert inv.fragments[5].status == 'unsupported' and ('field', '¶3') in inv.unsupported
    assert inv.external_links == 1
    assert len(dt.parts(inv.fragments[6].text)) > 1                     # long paragraph: several linked parts
    job = dt.Job(inv, 'ru', 'en')
    dt.run_batch(job, fake('ru', 'en'))
    long = inv.fragments[6]
    assert long.status == 'done' and long.translation.count('[EN]') == len(dt.parts(long.text))
    assert [f.status for f in inv.fragments].count('done') == 7 and job.state == 'review'


def test_dtr03_new_copy_original_untouched_and_never_overwritten(doc):
    before = doc.read_bytes()
    inv = dt.inspect(str(doc), before)
    job = dt.Job(inv, 'ru', 'en')
    dt.run_batch(job, fake('ru', 'en'))
    taken = doc.with_name('Отчёт (EN).docx')
    taken.write_bytes(b'existing')
    saved = dt.save(job)
    assert saved.name == 'Отчёт (EN 2).docx' and taken.read_bytes() == b'existing'
    assert doc.read_bytes() == before
    new = zipfile.ZipFile(saved)
    old = zipfile.ZipFile(io.BytesIO(before))
    for name in old.namelist():
        if name != 'word/document.xml':
            assert new.read(name) == old.read(name)                     # every other part byte for byte
    xml = new.read('word/document.xml').decode('utf-8')
    assert '[EN] Отчёт за квартал' in xml and '<w:b/>' in xml and 'w:pStyle w:val="Heading1"' in xml
    assert '01.01.2026' in xml and 'fldSimple' in xml                    # the unsupported field is kept as it was
    assert not list(saved.parent.glob('*.neo188.tmp'))


def test_dtr04_unsafe_or_unsupported_containers_stop_before_anything(tmp_path):
    macro = tmp_path / 'm.docx'
    macro.write_bytes(docx(p('x y'), extra={'word/vbaProject.bin': b'\x00' * 10}))
    with pytest.raises(dt.DocError) as e:
        dt.inspect(str(macro), macro.read_bytes())
    assert e.value.key == 'macros'
    with pytest.raises(dt.DocError) as e:
        dt.inspect('x.docx', b'not a zip at all')
    assert e.value.key == 'damaged'
    with pytest.raises(dt.DocError) as e:
        dt.inspect('x.doc', docx(p('a')))
    assert e.value.key == 'not_docx'
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('word/document.xml', '<a>' + 'A' * 3_000_000 + '</a>')
    with pytest.raises(dt.DocError) as e:
        dt.inspect('b.docx', bomb.getvalue())
    assert e.value.key == 'too_large'
    doctype = docx(p('a'), extra={'word/document.xml': '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><w:document ' + W + '><w:body>' + p('&a;') + '</w:body></w:document>'})
    with pytest.raises(dt.DocError):
        dt.inspect('d.docx', doctype)
    rich = docx(p('Текст') + '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>Слито</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                + '<w:p><w:r><w:drawing/></w:r><w:r><w:t>С картинкой</w:t></w:r></w:p>',
                extra={'word/header1.xml': '<w:hdr ' + W + '><w:p><w:r><w:t>Шапка</w:t></w:r></w:p></w:hdr>',
                       'word/comments.xml': '<w:comments ' + W + '><w:comment><w:p><w:r><w:t>Комментарий</w:t></w:r></w:p></w:comment></w:comments>'})
    inv = dt.inspect('r.docx', rich)
    kinds = {k for k, _w in inv.unsupported}
    assert {'table', 'drawing', 'header', 'comments'} <= kinds


def test_dtr09_empty_or_failed_answers_are_not_success_and_limits_are_explicit(doc, tmp_path):
    inv = dt.inspect(str(doc), doc.read_bytes())
    job = dt.Job(inv, 'ru', 'en')
    answers = iter(['', 'ok'] * 50)
    dt.run_batch(job, lambda text: next(answers))
    assert inv.fragments[0].status == 'error' and inv.fragments[0].translation == ''
    def boom(text):
        raise TimeoutError('slow')
    job2 = dt.Job(dt.inspect(str(doc), doc.read_bytes()), 'ru', 'en')
    dt.run_batch(job2, boom)
    assert all(f.status in {'error', 'unsupported'} for f in job2.inventory.fragments)
    huge = tmp_path / 'huge.docx'
    huge.write_bytes(docx(''.join(p(f'Абзац номер {i} с текстом') for i in range(1001))))
    with pytest.raises(dt.DocError) as e:
        dt.inspect(str(huge), huge.read_bytes())
    assert e.value.key == 'limit' and e.value.values['fragments'] == 1001
    partial = dt.Job(dt.inspect(str(doc), doc.read_bytes()), 'ru', 'en')
    dt.run_batch(partial, fake('ru', 'en'), limit=2)
    assert partial.state == 'translating' and len(partial.pending()) == 5


def test_dtr10_numbers_and_links_are_checked_literally():
    assert dt.check_flags('Выручка 15% и 2026 год', 'Revenue 15% and the year 2026') == ''
    assert dt.check_flags('Выручка 15%', 'Revenue fifteen percent') == 'numbers'
    assert dt.check_flags('см. https://example.com/a', 'see the site') == 'links'


@pytest.mark.parametrize('code,phrase,target', [
    ('ru', 'переведи документ Отчёт.docx на английский, сохрани отдельную копию', 'en'), ('ru', 'переведи Отчёт.docx на немецкий', 'de'),
    ('en', 'translate the document Отчёт.docx into German', 'de'), ('de', 'übersetze das Dokument Отчёт.docx ins Englische', 'en'),
    ('fr', 'traduis le document Отчёт.docx en anglais', 'en'), ('es', 'traduce el documento Отчёт.docx al inglés', 'en'),
    ('it', 'traduci il documento Отчёт.docx in inglese', 'en'), ('pt', 'traduz o documento Отчёт.docx para inglês', 'en'),
])
def test_dtr10_start_in_seven_languages(code, phrase, target):
    assert ctl.parse_start(phrase, code) == {'file': 'Отчёт.docx', 'target': target}


def test_phrase_translation_is_not_taken_over():
    assert ctl.parse_start('переведи привет на английский', 'ru') is None
    assert ctl.parse_start('translate hello into German', 'en') is None


@pytest.mark.parametrize('code', CODES)
def test_dtr10_controls_and_texts_in_seven_languages(code):
    words = {'ru': ('покажи фрагмент 3', 'исправь фрагмент 3: Новый текст', 'пропусти фрагмент 3', 'сохрани перевод', 'отмени перевод'),
             'en': ('show fragment 3', 'fix fragment 3: New text', 'skip fragment 3', 'save the translation', 'cancel the translation'),
             'de': ('zeig Fragment 3', 'korrigiere Fragment 3: Neuer Text', 'überspringe Fragment 3', 'speichere die Übersetzung', 'brich die Übersetzung ab'),
             'fr': ('montre le fragment 3', 'corrige le fragment 3: Nouveau texte', 'passe le fragment 3', 'sauvegarde la traduction', 'annule la traduction'),
             'es': ('muéstrame el fragmento 3', 'corrige el fragmento 3: Texto nuevo', 'salta el fragmento 3', 'guarda la traducción', 'cancela la traducción'),
             'it': ('mostrami il frammento 3', 'correggi il frammento 3: Testo nuovo', 'salta il frammento 3', 'salva la traduzione', 'annulla la traduzione'),
             'pt': ('mostra o fragmento 3', 'corrige o fragmento 3: Texto novo', 'salta o fragmento 3', 'guarda a tradução', 'cancela a tradução')}[code]
    assert [ctl.command(w, code)[0] for w in words] == ['show', 'fix', 'skip', 'save', 'cancel']
    for key, row in ctl._T.items():
        if key != 'errors':
            assert row[CODES.index(code)]
    for row in ctl._T['errors'].values():
        assert row[CODES.index(code)]


@pytest.fixture
def agent_session(tmp_path, monkeypatch, doc):
    with external_io_blocked():
        s = AtlasSession(tmp_path / 'run')
        s.reset_case('ru')
        from jarvis.runtime import office178
        monkeypatch.setattr(office178, 'user_folders', lambda: [doc.parent])
        s.agent._doc_translator188 = fake
        s.doc = doc
        yield s
        s.finish()


def test_dtr05_dtr08_review_edit_retry_skip_and_save_through_the_agent(agent_session):
    s = agent_session
    journal = Mock()
    s.agent.conversation_journal = journal
    plan = s.agent.handle('переведи документ Отчёт.docx на английский')
    assert plan.tool_details == ['doctr188'] and 'только локальный движок' in plan.text and 'field' in plan.text
    assert 'Отчёт (EN).docx' in plan.text and 'готов к проверке' in plan.text
    shown = s.agent.handle('покажи фрагмент 2').text
    assert 'Оригинал: Выручка' in shown and 'Перевод: [EN] Выручка' in shown
    fixed = s.agent.handle('исправь фрагмент 2: Revenue grew by 15% (https://example.com/report.)').text
    assert 'исправлено вами' in fixed
    assert '[EN] Отчёт' in s.agent.handle('покажи фрагмент 1').text          # the neighbour is unchanged
    assert 'пропущен' in s.agent.handle('пропусти фрагмент 8').text
    assert 'не поддерживается' in s.agent.handle('исправь фрагмент 6: x').text
    saved = s.agent.handle('сохрани перевод').text
    assert 'Сохранил отдельную копию' in saved and 'частичный перевод' in saved
    xml = zipfile.ZipFile(s.doc.with_name('Отчёт (EN).docx')).read('word/document.xml').decode('utf-8')
    assert 'Revenue grew by 15%' in xml and '>Север<' in xml                 # skipped fragment kept the original
    assert not journal.record_turn.called


def test_dtr08_pause_cancel_and_late_work_create_no_file(agent_session, monkeypatch):
    s = agent_session
    monkeypatch.setattr(dt, 'BATCH_FRAGMENTS', 2)
    first = s.agent.handle('переведи документ Отчёт.docx на английский')
    assert 'Переведено 2 из 7' in first.text
    assert 'приостановлен' in s.agent.handle('приостанови перевод').text
    job = s.agent._doc_jobs188['desktop']
    before = len(job.pending())
    assert 'не закончен' in s.agent.handle('сохрани перевод').text and len(job.pending()) == before
    assert 'Переведено 4 из 7' in s.agent.handle('продолжи перевод').text
    assert 'отменён' in s.agent.handle('отмени перевод').text
    assert not list(s.doc.parent.glob('Отчёт (EN*'))


def test_dtr06_local_only_no_model_means_nothing_is_sent(agent_session):
    s = agent_session
    s.agent._doc_translator188 = None
    s.agent.config.data.setdefault('ollama', {})['base_url'] = 'http://203.0.113.5:11434'
    reply = s.agent.handle('переведи документ Отчёт.docx на английский').text
    assert 'никуда не отправляю' in reply
    assert 'desktop' not in getattr(s.agent, '_doc_jobs188', {})


def test_dtr01_same_named_documents_are_listed_not_guessed(agent_session, tmp_path, monkeypatch):
    s = agent_session
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'Отчёт.docx').write_bytes(s.doc.read_bytes())
    from jarvis.runtime import office178
    monkeypatch.setattr(office178, 'user_folders', lambda: [s.doc.parent, other])
    reply = s.agent.handle('переведи документ Отчёт на английский').text
    assert 'несколько документов' in reply and str(other / 'Отчёт.docx') in reply


def test_dtr01_source_changed_after_reading_is_not_saved(agent_session):
    s = agent_session
    s.agent.handle('переведи документ Отчёт.docx на английский')
    s.doc.write_bytes(docx(p('Другой текст документа')))
    assert 'изменился' in s.agent.handle('сохрани перевод').text
    assert not list(s.doc.parent.glob('Отчёт (EN*'))

"""NB33 entrypoint regressions: paragraph-preserving saves in the real fact editor."""
import json
import time
from types import SimpleNamespace
import pytest
from PySide6.QtGui import QTextCursor
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QDialog
from jarvis.runtime import memory169 as memory
from jarvis.ui.quality_memory184 import FactEditDialog, MemoryFactsDialog
from tests.test_quality_memory184 import Config, app, db, seed


@pytest.fixture(autouse=True)
def dispose_editor_test_widgets(app):
    yield
    # These tests deliberately retain large legacy documents until deleteLater.
    # Drain their deferred deletion here, not during a later settings-ready wait.
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def note_row(db, value):
    db.set_memory('note:reviewed', value)
    row=next(row for row in memory.user_fact_rows(db) if row['key']=='note:reviewed')
    if row['value']!=value:
        assert db.change_memory_snapshot([row],value=value)
    return next(row for row in memory.user_fact_rows(db) if row['key']=='note:reviewed')


def test_original_editor_untouched_mixed_cr_lf_preserves_exact_note(app,db):
    source='  first\r\n\r\nsecond\rthird\n\nfourth\tline  \n'
    row=note_row(db,source)
    editor=FactEditDialog(db,Config(),row)
    assert '\n\n' in editor.value.toPlainText()
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])==source
    reopened=MemoryFactsDialog(db,Config())
    assert reopened.table.item(0,1).text()==source
    reopened.deleteLater();editor.deleteLater()


def test_original_editor_edit_preserves_paragraphs_and_field_whitespace(app,db):
    row=note_row(db,'first\r\n\r\nsecond\rthird\n\nfourth')
    editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor);editor.value.insertPlainText('\n\n  final\tline  ')
    expected=editor.value.toPlainText()
    assert expected=='first\n\nsecond\nthird\n\nfourth\n\n  final\tline  '
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])==expected
    editor.deleteLater()


def test_note_domain_edit_preserves_supplied_mixed_newlines_verbatim(db):
    row=note_row(db,'initial')
    value='  one\r\n\r\ntwo\rthree\n\tfour  \n'
    assert memory.edit_user_fact(db,Config(),row,value,'en')[0]
    assert db.get_memory(row['key'])==value


def test_note_editor_cancel_failure_stale_and_retry_preserve_draft(app,db,monkeypatch):
    source='one\r\n\r\ntwo';row=note_row(db,source)
    editor=FactEditDialog(db,Config(),row);editor.value.setPlainText('cancel\n\nme');editor.reject()
    assert db.get_memory(row['key'])==source;editor.deleteLater()
    editor=FactEditDialog(db,Config(),row);editor.value.setPlainText(' keep\n\n  this\ttext ')
    expected=editor.value.toPlainText();original=db.change_memory_snapshot
    monkeypatch.setattr(db,'change_memory_snapshot',lambda *a,**k:(_ for _ in ()).throw(OSError('disk full')))
    editor.save_button.click()
    assert editor.result()!=QDialog.Accepted and editor.value.toPlainText()==expected
    assert db.get_memory(row['key'])==source
    monkeypatch.setattr(db,'change_memory_snapshot',original)
    editor.save_button.click();assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])==expected;editor.deleteLater()
    stale=FactEditDialog(db,Config(),row);stale.value.setPlainText('stale\n\ntext');stale.save_button.click()
    assert stale.result()!=QDialog.Accepted and db.get_memory(row['key'])==expected
    assert stale.value.toPlainText()=='stale\n\ntext';stale.deleteLater()


def test_note_layout_counts_toward_limit_and_blank_input_is_refused(app,db):
    row=note_row(db,'one\r\n\r\ntwo');editor=FactEditDialog(db,Config(),row)
    oversized='A'+'\n'*memory.NOTE_MAX
    editor.value.setPlainText(oversized);editor.save_button.click()
    assert editor.result()!=QDialog.Accepted
    assert editor.value.toPlainText()==oversized and db.get_memory(row['key'])==row['value']
    editor.value.setPlainText('\r\n\n\t  ');editor.save_button.click()
    assert editor.result()!=QDialog.Accepted and db.get_memory(row['key'])==row['value']
    editor.deleteLater()


def test_long_repetitive_note_save_is_bounded_and_never_truncates(app,db):
    source=('same\r\n\r\n'*25000)+'end';row=note_row(db,source)
    editor=FactEditDialog(db,Config(),row);start=time.monotonic();editor.save_button.click()
    assert time.monotonic()-start<3
    assert editor.result()!=QDialog.Accepted and db.get_memory(row['key'])==source
    editor.value.setPlainText('short\n\nnote');editor.save_button.click()
    assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])=='short\n\nnote'
    editor.deleteLater()


@pytest.mark.parametrize('code',('ru','en','de','fr','es','it','pt'))
def test_mixed_newline_editor_all_languages(app,db,code):
    row=note_row(db,'Здравствуйте\r\n\r\nCafé\remoji 😀\nlast')
    editor=FactEditDialog(db,Config(code),row)
    editor.value.setPlainText('  Новый\n\nCafé\n😀\tlast  ');expected=editor.value.toPlainText()
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==expected
    editor.deleteLater()


def test_preference_normalization_is_unchanged(app,db):
    row=seed(db,'tea','green');editor=FactEditDialog(db,Config(),row)
    editor.value.setPlainText('  black\n  tea!  ');editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert json.loads(db.get_memory(row['key']))['value']=='black tea'
    editor.deleteLater()


def test_edit_undo_returns_exact_legacy_note_and_redo_keeps_paragraphs(app,db):
    source='first\r\n\r\nsecond\rthird\n\nfourth';row=note_row(db,source)
    editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor);editor.value.insertPlainText('\n\nlast')
    editor.value.undo();editor.save_button.click()
    assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==source
    editor.deleteLater()
    row=memory.user_fact_rows(db)[0];editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor);editor.value.insertPlainText('\n\nlast')
    expected=editor.value.toPlainText();editor.value.undo();editor.value.redo();editor.save_button.click()
    assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==expected
    editor.deleteLater()


def test_mixed_newline_paste_preserves_editor_paragraphs(app,db):
    from PySide6.QtCore import QMimeData
    row=note_row(db,'first\r\n\r\nsecond');editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor)
    mime=QMimeData();mime.setText('\r\n\r\n  pasted\ttext\rfinal  ')
    editor.value.insertFromMimeData(mime)
    expected=editor.value.toPlainText()
    assert expected=='first\n\nsecond\n\n  pasted\ttext\nfinal  '
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==expected
    editor.deleteLater()


def test_disabled_memory_and_scope_still_protect_note_edits(app,db):
    row=note_row(db,'first\r\n\r\nsecond');editor=FactEditDialog(db,Config(enabled=False),row)
    editor.value.setPlainText('new\n\ntext');editor.save_button.click()
    assert editor.result()!=QDialog.Accepted and db.get_memory(row['key'])==row['value']
    assert editor.value.toPlainText()=='new\n\ntext';editor.deleteLater()
    db.set_memory('internal.not-a-note','private')
    hidden=next(r for r in db.list_memories() if r['key']=='internal.not-a-note')
    assert not memory.edit_user_fact(db,Config(),hidden,'new\n\ntext','en')[0]
    assert db.get_memory(hidden['key'])=='private'


def test_generated_mixed_separators_never_glue_paragraphs(app,db):
    from itertools import product
    from PySide6.QtCore import QCoreApplication, QEvent
    separators=('\n','\r','\r\n','\n\r','\r\r\n','\r\n\n')
    for a,b,c in product(separators,repeat=3):
        source='  α😀'+a+'\tBeta'+b+'Gamma'+c+'Last  '
        row=note_row(db,source);editor=FactEditDialog(db,Config(),row)
        initial_display=editor.value.toPlainText();editor.save_button.click()
        assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==source
        editor.deleteLater()
        row=memory.user_fact_rows(db)[0];editor=FactEditDialog(db,Config(),row)
        editor.value.setPlainText(initial_display.replace('Gamma','Edited\n\nGamma'))
        expected=editor.value.toPlainText();editor.save_button.click()
        assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==expected
        editor.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def test_real_save_keeps_nbsp_and_literal_whitespace_when_note_is_edited(app,db):
    source='Price: 10\u00a0EUR\r\n\r\nkeep\t  spaces'
    row=note_row(db,source);editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor);editor.value.insertPlainText('\nAdded')
    assert '\u00a0' in editor.value.document().toRawText()
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])=='Price: 10\u00a0EUR\n\nkeep\t  spaces\nAdded'
    editor.deleteLater()


def test_replacing_nbsp_with_space_is_not_mistaken_for_untouched_note(app,db):
    row=note_row(db,'Price: 10\u00a0EUR\r\n\r\nkeep')
    editor=FactEditDialog(db,Config(),row);editor.value.setPlainText('Price: 10 EUR\n\nkeep')
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])=='Price: 10 EUR\n\nkeep'
    editor.deleteLater()


def test_nbsp_paste_preserves_literal_unicode_spaces(app,db):
    from PySide6.QtCore import QMimeData
    row=note_row(db,'start\r\n\r\nline');editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor)
    mime=QMimeData();mime.setText('\n\n20\u00a0EUR\t  total\u202fX\u2007Y\u2009Z')
    editor.value.insertFromMimeData(mime);editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])=='start\n\nline\n\n20\u00a0EUR\t  total\u202fX\u2007Y\u2009Z'
    editor.deleteLater()


def test_nbsp_undo_preserves_unchanged_original_mixed_endings(app,db):
    source='Price: 10\u00a0EUR\r\n\r\nkeep\t  spaces\rfinal'
    row=note_row(db,source);editor=FactEditDialog(db,Config(),row)
    cursor=editor.value.textCursor();cursor.movePosition(QTextCursor.End)
    editor.value.setTextCursor(cursor);editor.value.insertPlainText('\nAdded');editor.value.undo()
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted and db.get_memory(row['key'])==source
    editor.deleteLater()


def test_unicode_paragraph_separators_become_lf_without_normalizing_spaces(app,db):
    row=note_row(db,'old');editor=FactEditDialog(db,Config(),row)
    editor.value.setPlainText('10\u00a0EUR\u2028line\u2029\u2029next\t  end')
    editor.save_button.click()
    assert editor.result()==QDialog.Accepted
    assert db.get_memory(row['key'])=='10\u00a0EUR\nline\n\nnext\t  end'
    editor.deleteLater()

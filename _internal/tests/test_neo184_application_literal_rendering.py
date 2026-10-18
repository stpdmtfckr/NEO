"""NB35: application labels remain literal from the first streamed character."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt, QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from jarvis.ui.chat_view import ChatBodyLabel, ChatView
from jarvis.desktop_resolver import ResolvedTarget
from jarvis.testing.atlas_session import AtlasConfig, AuditAgent
from jarvis.testing.isolation import external_io_blocked


@pytest.fixture
def app(tmp_path, monkeypatch):
    with external_io_blocked():
        agent=AuditAgent(AtlasConfig(tmp_path/'profile'))
        state=SimpleNamespace(agent=agent,candidates=[
            ResolvedTarget('app',str(tmp_path/'alpha.exe'),'Orbit Alpha',.80),
            ResolvedTarget('app',str(tmp_path/'beta.exe'),'Orbit Beta',.79)])
        monkeypatch.setattr('jarvis.tools.implementations.application_candidates',
                            lambda query,limit=5:list(state.candidates[:limit]))
        yield state
        agent.database.close()


@pytest.fixture
def chat(monkeypatch):
    qt=QApplication.instance() or QApplication([])
    view=ChatView()
    render_attempts=[]
    original=ChatBodyLabel.setText
    def guarded(label, text):
        # Reject *before* handing any resource-capable markup to Qt. This catches
        # transient AutoText on initial construction and on reused/streamed rows.
        render_attempts.append((label.textFormat(), text))
        if '<img' in text:
            assert label.textFormat()==Qt.PlainText, 'Resource-capable app text reached rich rendering'
        original(label,text)
    monkeypatch.setattr(ChatBodyLabel,'setText',guarded)
    yield view,render_attempts
    view.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)


def assert_literal(label, text):
    assert label.textFormat()==Qt.PlainText
    label._select_all()
    assert label.selectedText()==text


def menu(app):
    app.candidates=[replace(app.candidates[0],label='<img src="https://invalid.test/pixel">'),
                    replace(app.candidates[1],label='<b>Calculator</b>')]
    result=app.agent.handle('открой приложение Orbit')
    assert result.success is not True
    assert all(row.label in result.text for row in app.candidates)
    return result.text


def test_real_application_menu_initial_bubble_never_parses_resources(app,chat):
    text=menu(app)
    view,attempts=chat
    bubble=view.add_assistant(text)
    assert_literal(bubble.body_label,text)
    assert attempts and all(fmt==Qt.PlainText for fmt,_ in attempts)


def test_streamed_menu_and_reused_bubble_are_literal_before_setting_text(app,chat):
    text=menu(app)
    view,attempts=chat
    bubble=view.add_assistant_pending('')
    for end in range(1,len(text)+1,7):
        view.update_bubble(bubble,text[:end])
    view.update_bubble(bubble,text)
    assert_literal(bubble.body_label,text)
    replacement='<b>Second result</b>\nПривет 😀 世界\nhttps://example.invalid/'+'x'*180
    view.update_bubble(bubble,replacement)
    assert_literal(bubble.body_label,replacement)
    assert all(fmt==Qt.PlainText for fmt,_ in attempts)


def test_explicit_trusted_rich_label_keeps_rendered_copy_then_resets_plain():
    label=ChatBodyLabel()
    label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
    label.set_message('<b>Hello</b>',plain=False)
    label._select_all()
    assert label.selectedText()=='Hello'
    label.set_message('<b>Literal now</b>',plain=True)
    assert_literal(label,'<b>Literal now</b>')
    label.set_message('<b>Rich again</b>',plain=False)
    label._select_all()
    assert label.selectedText()=='Rich again'


def test_plain_resource_text_is_removed_before_enabling_trusted_rich(monkeypatch):
    label=ChatBodyLabel()
    label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
    label.set_message('<img src="x">',plain=True)
    events=[]
    original_format=ChatBodyLabel.setTextFormat
    original_clear=ChatBodyLabel.clear
    def change_format(widget,fmt):
        events.append(('format',widget.textFormat(),widget.text(),fmt))
        if fmt==Qt.RichText:
            assert not widget.text(), 'Old untrusted text would be parsed before replacement'
        original_format(widget,fmt)
    def clear(widget):
        events.append(('clear',widget.textFormat(),widget.text()))
        original_clear(widget)
    monkeypatch.setattr(ChatBodyLabel,'setTextFormat',change_format)
    monkeypatch.setattr(ChatBodyLabel,'clear',clear)
    label.set_message('<b>Trusted replacement</b>',plain=False)
    assert events[0]==('clear',Qt.PlainText,'<img src="x">')
    assert events[1]==('format',Qt.PlainText,'',Qt.RichText)
    label._select_all()
    assert label.selectedText()=='Trusted replacement'

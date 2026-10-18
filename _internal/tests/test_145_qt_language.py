"""Application-language Qt chrome, without starting the assistant or audio."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, Qt
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QFileDialog, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QTextEdit

from jarvis.ui.qt_localization import install_ui_language, localize_standard_buttons, LocalizedFileDialog, LocalizedColorDialog


@pytest.fixture(scope="module")
def app():
    value = QApplication.instance() or QApplication([])
    yield value
    install_ui_language(value, "en")


@pytest.mark.parametrize("code,yes,no,copy,cancel", [
    ("ru", "Да", "Нет", "Копировать", "Отмена"),
    ("en", "Yes", "No", "Copy", "Cancel"),
    ("de", "Ja", "Nein", "Kopieren", "Abbrechen"),
    ("fr", "Oui", "Non", "Copier", "Annuler"),
    ("es", "Sí", "No", "Copiar", "Cancelar"),
    ("it", "Sì", "No", "Copia", "Annulla"),
    ("pt", "Sim", "Não", "Copiar", "Cancelar"),
])
@pytest.mark.parametrize("native_catalog", [False, True])
def test_stock_buttons_context_menu_and_file_dialog(app, code, yes, no, copy, cancel, native_catalog, tmp_path):
    install_ui_language(app, code, load_native=native_catalog)
    box = QMessageBox()
    box.setText("Yes Save Мир.txt")
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.show()
    app.processEvents()
    assert box.button(QMessageBox.Yes).text().replace("&", "") == yes
    assert box.button(QMessageBox.No).text().replace("&", "") == no
    assert box.text() == "Yes Save Мир.txt"
    for kind in (QLineEdit, QPlainTextEdit, QTextEdit):
        edit = kind("Yes Save Мир.txt")
        menu = edit.createStandardContextMenu()
        captions = [action.text().replace("&", "").split("\t")[0] for action in menu.actions()]
        assert copy in captions, kind.__name__
        value = edit.text() if isinstance(edit, QLineEdit) else edit.toPlainText()
        assert value == "Yes Save Мир.txt"
        menu.deleteLater()
    dialog = LocalizedFileDialog()
    dialog.setDirectory(str(tmp_path))
    dialog.selectFile("Yes Save Мир.txt")
    dialog.show()
    app.processEvents()
    buttons = dialog.findChild(QDialogButtonBox)
    assert buttons.button(QDialogButtonBox.Cancel).text().replace("&", "") == cancel
    assert dialog.selectedFiles()[0].endswith("Yes Save Мир.txt")
    assert dialog.testOption(QFileDialog.Option.DontUseNativeDialog)
    assert not app.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
    assert QCoreApplication.translate("NEO_user_payload", "Yes") == "Yes"
    box.close()
    dialog.close()


def test_live_apply_updates_existing_buttons_and_preserves_custom_text(app):
    install_ui_language(app, "ru", load_native=False)
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    custom = box.addButton("Yes report.txt", QMessageBox.ActionRole)
    box.show()
    app.processEvents()
    assert box.button(QMessageBox.Yes).text().replace("&", "") == "Да"
    state = install_ui_language(app, "de", load_native=False)
    app.processEvents()
    assert box.button(QMessageBox.Yes).text().replace("&", "") == "Ja"
    assert box.button(QMessageBox.No).text().replace("&", "") == "Nein"
    assert custom.text() == "Yes report.txt"
    assert QLocale().name() == "de_DE"
    assert install_ui_language(app, "de", load_native=False) is state
    assert len(state.translators) == 1
    box.close()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_explicit_button_helper_without_catalog(app):
    box = QMessageBox()
    box.setStandardButtons(QMessageBox.Save | QMessageBox.Cancel)
    localize_standard_buttons(box, "it")
    assert box.button(QMessageBox.Save).text().replace("&", "") == "Salva"
    assert box.button(QMessageBox.Cancel).text().replace("&", "") == "Annulla"


def test_owned_color_picker_has_native_fallback_labels(app):
    install_ui_language(app, "ru", load_native=False)
    picker = LocalizedColorDialog()
    picker.show()
    app.processEvents()
    assert picker.windowTitle() == "Выберите цвет"
    captions = {x.text().replace("&", "") for x in picker.findChildren(QLabel)}
    assert {"Красный:", "Зелёный:", "Синий:"} <= captions
    picker.close()


def test_many_hotkey_conflicts_remain_in_settings_without_chat_notice():
    from types import SimpleNamespace
    from jarvis.ui.main_window import MainWindow
    messages = []
    window = SimpleNamespace(_language_code=lambda: "ru", _append_assistant=lambda text, **kwargs: messages.append((text, kwargs)))
    for key in ("F12", "Ctrl+LTM", "F12", "Ctrl+Alt+B"):
        MainWindow._notify_hotkey_conflict(window, key)
    assert messages == []  # 151: explicit request to keep startup diagnostics out of chat.
    assert window._hotkey_conflicts == {"F12", "Ctrl+LTM", "Ctrl+Alt+B"}


def test_catalogue_parity_and_explicit_russian_overrides():
    from jarvis.i18n import _ui_catalog, tr
    rows = {code: _ui_catalog(code) for code in ("ru", "en", "de", "fr", "es", "it", "pt")}
    assert all(set(value) == set(rows["ru"]) for value in rows.values())
    assert all(value.strip() for catalogue in rows.values() for value in catalogue.values())
    assert tr("Latency profiler", "ru") == "Измерение задержек"
    assert tr("Self-Test пройден.", "ru") == "Самопроверка пройдена."
    assert tr("/home/User/Latency profiler.txt", "ru") == "/home/User/Latency profiler.txt"
    assert tr("Provider returned 403: forbidden", "ru") == "Provider returned 403: forbidden"


def test_language_apply_never_translates_chat_payload_or_assistant_name(app):
    from jarvis.i18n import translate_widget_tree
    from jarvis.ui.chat_view import ChatView
    chat = ChatView()
    chat.set_assistant_name("Снег")
    bubble = chat.add_assistant("Стиль")
    user = chat.add_user("Прозрачность фона окна таймера.")
    translate_widget_tree(chat, "de")
    chat.set_language("de")
    assert bubble.author_label.text() == "Снег"
    assert bubble.body_label.text() == "Стиль"
    assert user.body_label.text() == "Прозрачность фона окна таймера."
    assert user.author_label.text() != "Вы"
    chat.deleteLater()

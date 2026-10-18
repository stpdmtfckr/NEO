"""NEO 178: the settings window fits its contents to its size (wrap, flow, shorten — no cut-off text)."""
import time

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QCheckBox, QScrollArea

from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
from jarvis.ui.responsive178 import WrapCheckBox
from jarvis.testing.isolation import external_io_blocked
from tests.test_neo158_settings_io import app, config  # noqa: F401


def settle(app, seconds=0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


@pytest.mark.parametrize('code', ['ru', 'en', 'de', 'fr', 'es', 'it', 'pt'])
def test_every_page_fits_a_small_window(app, config, monkeypatch, code):  # noqa: F811
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: pytest.fail(str(args[2:])))
    config.data.setdefault('app', {})['language'] = code
    config.data['language'] = code
    with external_io_blocked():
        dialog = SettingsDialog(config)
        dialog.resize(600, 420)
        dialog.show()
        settle(app)
        assert dialog.width() <= 620
        try:
            for index in range(dialog.tabs._stack.count()):
                dialog.tabs.setCurrentIndex(index)
                settle(app, 0.12)
                page = dialog.tabs._stack.currentWidget()
                areas = [page] if isinstance(page, QScrollArea) else page.findChildren(QScrollArea)
                for area in areas:
                    if area.isVisible() and area.widget() is not None:
                        bar = area.horizontalScrollBar()
                        assert not bar.isVisible() or bar.maximum() == 0, (code, index, dialog.tabs._buttons[index].text(), bar.maximum())
                for box in page.findChildren(QCheckBox):
                    if box.isVisible() and box.text():
                        assert isinstance(box, WrapCheckBox), box.text()
                        assert box.height() >= box.heightForWidth(box.width()) - 1, (code, box.text(), box.height(), box.heightForWidth(box.width()))
            nav = dialog.tabs._nav.width()
            assert 140 <= nav <= 200
        finally:
            dialog.reject()
            dialog.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()


def test_wrapped_check_box_keeps_its_text_and_toggles_on_any_line(app):  # noqa: F811
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    box = WrapCheckBox('Очень длинная подпись, которая обязательно перенесётся на вторую и третью строку')
    box.resize(160, 10)
    box.show()
    settle(app, 0.05)
    assert box.heightForWidth(160) > box.fontMetrics().lineSpacing() * 2
    assert box.minimumHeight() >= box.heightForWidth(160) - 1
    QTest.mouseClick(box, Qt.LeftButton, Qt.NoModifier, QPoint(box.width() - 10, box.height() - 4))
    assert box.isChecked() and box.text().startswith('Очень')
    box.close()

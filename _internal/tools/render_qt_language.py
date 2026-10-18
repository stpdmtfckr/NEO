"""Render actual Qt language chrome without starting NEO services.

QT_QPA_PLATFORM=offscreen python tools/render_qt_language.py --output PATH
Screenshots are evidence of Qt rendering only, not Windows acoustic/OS tests.
"""
from __future__ import annotations
import argparse
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
from jarvis.ui.qt_localization import install_ui_language, LocalizedFileDialog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    captions = {
        "ru": ("NEO · Подтверждение", "Удалить заметку?", "Выберите файл"),
        "en": ("NEO · Confirmation", "Delete the note?", "Choose a file"),
        "de": ("NEO · Bestätigung", "Notiz löschen?", "Datei auswählen"),
        "fr": ("NEO · Confirmation", "Supprimer la note ?", "Choisir un fichier"),
        "es": ("NEO · Confirmación", "¿Eliminar la nota?", "Elegir archivo"),
        "it": ("NEO · Conferma", "Eliminare la nota?", "Scegli un file"),
        "pt": ("NEO · Confirmação", "Eliminar a nota?", "Escolher ficheiro"),
    }
    captures = []
    for code, (title, body, _) in captions.items():
        # Both production Qt catalogues and the explicit fallback are tested by
        # pytest; render fallback here to verify the no-catalogue path visually.
        install_ui_language(app, code, load_native=False)
        box = QMessageBox(QMessageBox.Question, title, body, QMessageBox.Yes | QMessageBox.No)
        box.show()
        app.processEvents()
        pixmap = box.grab()
        pixmap.save(str(args.output / f"qt_confirmation_{code}.png"))
        captures.append((code, pixmap))
        box.close()
    sheet = QImage(840, 720, QImage.Format_ARGB32)
    sheet.fill(QColor("#e9edf2"))
    painter = QPainter(sheet)
    for i, (code, pixmap) in enumerate(captures):
        x, y = 20 + (i % 2) * 420, 24 + (i // 2) * 175
        painter.setPen(QColor("#152238"))
        painter.drawText(QRect(x, y, 390, 24), Qt.AlignLeft, code.upper())
        painter.drawPixmap(QPoint(x, y + 30), pixmap)
    painter.end()
    sheet.save(str(args.output / "qt_confirmations_all7.png"))
    install_ui_language(app, "ru", load_native=False)
    payload = args.output / "Yes Save Мир.txt"
    payload.write_text("UI language test fixture\n", encoding="utf-8")
    picker = LocalizedFileDialog(None, captions["ru"][2], str(args.output), "*.txt")
    picker.resize(720, 480)
    picker.selectFile(payload.name)
    picker.show()
    app.processEvents()
    picker.grab().save(str(args.output / "qt_file_picker_ru.png"))
    picker.close()
    edit = QLineEdit("Yes Save Мир.txt")
    edit.selectAll()
    menu = edit.createStandardContextMenu()
    menu.show()
    app.processEvents()
    menu.grab().save(str(args.output / "qt_editor_menu_ru.png"))
    menu.close()
    print(args.output)


if __name__ == "__main__":
    main()

"""Construct Settings in disposable profiles and inspect all seven UI languages.

Includes hidden integration controls and menus/options present in the widget
tree. No microphone discovery, Agent, network authorization or tool dispatch.
External provider errors and future dynamically created branches are excluded.
"""
from __future__ import annotations
import argparse
import json
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import re
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QAbstractButton, QComboBox, QGroupBox, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QWidget
from jarvis.config import ConfigManager
from jarvis.i18n import GENERIC_HELP
from jarvis.runtime.localized_text import _FALLBACK
from jarvis.ui.qt_localization import install_ui_language
from jarvis.ui.settings_dialog import SettingsDialog

CODES = ("ru", "en", "de", "fr", "es", "it", "pt")


def collect_widgets(dialog):
    rows = []
    for widget in dialog.findChildren(QWidget):
        if isinstance(widget, (QLabel, QAbstractButton)):
            rows.append([type(widget).__name__, "caption", str(widget.property("i18n_text_source") or ""), widget.text()])
        elif isinstance(widget, QGroupBox):
            rows.append(["QGroupBox", "title", str(widget.property("i18n_title_source") or ""), widget.title()])
        if isinstance(widget, QLineEdit) and widget.placeholderText():
            rows.append(["QLineEdit", "placeholder", str(widget.property("i18n_placeholder_source") or ""), widget.placeholderText()])
        if isinstance(widget, QComboBox) and not widget.isEditable():
            for i in range(widget.count()):
                rows.append(["QComboBox", "option", str(widget.itemData(i, int(Qt.UserRole) + 77) or ""), widget.itemText(i)])
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            for kind, value in (("prefix", widget.prefix()), ("suffix", widget.suffix()), ("special_value", widget.specialValueText())):
                if value:
                    rows.append([type(widget).__name__, kind, str(widget.property("i18n_" + kind + "_source") or ""), value])
        if widget.toolTip():
            rows.append([type(widget).__name__, "tooltip", str(widget.property("i18n_tooltip_source") or ""), widget.toolTip()])
    return rows


def findings(rows, code):
    generic = set(GENERIC_HELP.values()) | {text for pair in _FALLBACK.values() for text in pair}
    return {
        "count": len(rows),
        "generic_fallbacks": sorted({source or text for _, _, source, text in rows if text in generic}),
        "cyrillic_leaks": sorted({text for _, _, _, text in rows if code != "ru" and text != "Русский" and re.search("[А-Яа-яЁё]", text)}),
        "unchanged_latin_candidates": sorted({source for _, _, source, text in rows if source == text and re.search("[A-Za-z]{4}", text) and not re.search("[А-Яа-яЁё]", text)}),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    SettingsDialog._refresh_microphones_async = lambda self: None
    report = {"schema": 1, "method": __doc__, "findings": {}, "sources": {}}
    with tempfile.TemporaryDirectory(prefix="neo-ui-inventory-") as directory:
        os.environ["APPDATA"] = directory + "/roaming"
        os.environ["LOCALAPPDATA"] = directory
        for code in CODES:
            config = ConfigManager(Path(__file__).resolve().parents[1] / "config/default_settings.json")
            config.data["app"]["language"] = code
            install_ui_language(app, code)
            dialog = SettingsDialog(config)
            app.processEvents()
            rows = collect_widgets(dialog)
            report["findings"][code] = findings(rows, code)
            for kind, field, source, value in rows:
                if source:
                    record = report["sources"].setdefault(source, {"locations": [], "values": {}})
                    if [kind, field] not in record["locations"]:
                        record["locations"].append([kind, field])
                    record["values"][code] = value
            dialog.close()
            dialog.deleteLater()
            app.processEvents()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({code: {"count": row["count"], "generic_fallbacks": len(row["generic_fallbacks"]), "cyrillic_leaks": len(row["cyrillic_leaks"]), "unchanged_latin_candidates": len(row["unchanged_latin_candidates"])} for code, row in report["findings"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()

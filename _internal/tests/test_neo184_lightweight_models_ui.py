"""Actual Settings Save preserves custom bases and migrated lightweight defaults."""
from unittest.mock import patch

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from tests.test_neo184_settings import app
from tests.test_neo184_lightweight_models import isolated, config, legacy, SMALL, assert_small


@pytest.mark.parametrize("custom", [False, True])
def test_settings_save_and_restart_preserve_model_selection(app, monkeypatch, custom):
    from jarvis.ui.settings_dialog import SettingsDialog, QMessageBox
    monkeypatch.setattr(SettingsDialog, "_refresh_microphones_async", lambda *a: None)
    monkeypatch.setattr(SettingsDialog, "_start_game_cache_stats158", lambda *a: None)
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args))
    payload = legacy()
    if custom:
        payload["llm_agent"].update(model="my/planner:7b", base_model="my/base:7b", fine_tuned="")
    c = config(payload)
    d = SettingsDialog(c)
    try:
        assert d._settings_ready160 and not d._settings_build_error160
        assert d.llm_agent_size170.currentData() == ("1.5b" if custom else "0.5b")
        assert d.chat_model.currentText() == SMALL
        with patch.object(QTimer, "singleShot", lambda *args: None):
            d._save(close=False)
        assert not errors
        again = config()
        if custom:
            assert again.get("llm_agent.model") == "my/planner:7b"
            assert again.get("llm_agent.base_model") == "my/base:7b"
            assert again.get("llm_agent.size") == "1.5b"
        else:
            assert_small(again)
            assert again.get("local_chat.model") == SMALL
    finally:
        d.reject()
        d.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()

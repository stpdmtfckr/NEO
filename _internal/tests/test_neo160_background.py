"""Selectable fireflies and cloud-free avatar, with real Qt paint/settings paths."""
import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QMessageBox

from jarvis.config import ConfigManager
from jarvis.i18n import translate_widget_tree
from jarvis.integrations.telegram_settings import SETTINGS, display_value
from tests.test_145_settings_language import make_dialog
from tests.test_neo158_robot import app, robot, render, settled

LABELS = {'ru': 'Светлячки', 'en': 'Fireflies', 'de': 'Glühwürmchen',
          'fr': 'Lucioles', 'es': 'Luciérnagas', 'it': 'Lucciole', 'pt': 'Pirilampos'}


def background_frame(robot, elapsed):
    robot._elapsed = elapsed
    image = QImage(robot.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        robot._draw_active_animation(painter)
    finally:
        painter.end()
    return image


def test_fireflies_fill_background_and_drift_with_bounded_density(robot):
    robot.set_active_animation('fireflies')
    first = background_frame(robot, 1)
    second = background_frame(robot, 6)
    assert first != second
    occupied = set()
    lit_pixels = 0
    for x in range(0, first.width(), 2):
        for y in range(0, first.height(), 2):
            if first.pixelColor(x, y).alpha() > 30:
                lit_pixels += 1
                occupied.add((int(x * 4 / first.width()), int(y * 3 / first.height())))
    assert len(occupied) >= 9  # Across the stage, not a single robot-side scene.
    assert 30 < lit_pixels < first.width() * first.height() / 30
    robot.set_active_animation('off')
    assert all(background_frame(robot, 6).pixelColor(x, 50).alpha() == 0
               for x in range(first.width()))


def test_fireflies_safe_ui_is_static_and_hidden_widget_has_no_clock(robot, app):
    robot._safe_ui = True
    robot.set_active_animation('fireflies')
    first = background_frame(robot, 1)
    assert first == background_frame(robot, 120)
    assert not robot._timer.isActive()
    assert len(robot.findChildren(QTimer)) == 1
    robot.show(); app.processEvents()
    assert robot._timer.interval() >= 33
    robot.hide()
    assert not robot._timer.isActive()


@pytest.mark.parametrize('state', ['idle', 'thinking', 'typing', 'speaking', 'sleeping'])
def test_background_survives_normal_robot_state_changes_without_speech_cloud(robot, state):
    robot.set_active_animation('fireflies')
    robot.set_state(state)
    settled(robot, 5)
    background = background_frame(robot, 5)
    assert robot._active_animation == 'fireflies'
    assert not hasattr(robot, '_draw_reply_bubble')
    robot.set_reply_activity(True, 'Do not display this beside the avatar')
    assert background == background_frame(robot, 5)
    assert not render(robot).isNull()


def test_single_firefly_is_not_an_idle_scene(robot, monkeypatch):
    choices = []
    def choose(options):
        choices.extend(options)
        return options[0]
    monkeypatch.setattr('jarvis.ui.robot_widget.random.choice', choose)
    robot.start_idle_scene(scene='firefly')  # Retired scene falls back normally.
    assert 'firefly' not in choices and robot._idle_action != 'firefly'
    robot._idle_action = 'none'
    robot._idle_action_until = 0
    robot._next_idle_action = -1
    robot._advance_animation()
    assert 'firefly' not in choices


@pytest.mark.parametrize('code', list(LABELS))
def test_fireflies_choice_localized_in_desktop_and_remote_settings(code, make_dialog):
    dialog = make_dialog(code)
    combo = dialog.robot_active_animation
    index = combo.findData('fireflies')
    assert index >= 0 and combo.itemText(index) == LABELS[code]
    combo.setCurrentIndex(index)
    translate_widget_tree(dialog, 'en')
    assert combo.currentData() == 'fireflies' and combo.currentText() == 'Fireflies'
    translate_widget_tree(dialog, code)
    assert combo.currentData() == 'fireflies' and combo.currentText() == LABELS[code]
    spec = SETTINGS['ui_robot_active_animation']
    assert 'fireflies' in spec.choices
    assert display_value(spec, 'fireflies', code) == LABELS[code]


def test_fireflies_settings_save_and_reload_keeps_other_ambient_options(make_dialog, monkeypatch):
    dialog = make_dialog('ru')
    monkeypatch.setattr(QMessageBox, 'critical', lambda *args: pytest.fail(str(args[2:])))
    combo = dialog.robot_active_animation
    existing = {combo.itemData(i) for i in range(combo.count())}
    assert existing == {'off', 'rain', 'snow', 'starfall', 'cloudy', 'starry_sky', 'fireflies'}
    combo.setCurrentIndex(combo.findData('fireflies'))
    dialog._save(close=False)
    saved = json.loads(dialog.config.paths.settings.read_text(encoding='utf-8'))
    assert saved['ui']['robot_active_animation'] == 'fireflies'
    reloaded = ConfigManager(Path(__file__).parents[1] / 'config/default_settings.json')
    assert reloaded.get('ui.robot_active_animation') == 'fireflies'
    second = make_dialog('ru')
    assert second.robot_active_animation.currentData() == 'fireflies'
    second.robot_active_animation.setCurrentIndex(second.robot_active_animation.findData('rain'))
    second._save(close=False)
    assert second.config.get('ui.robot_active_animation') == 'rain'

"""NEO185 robot animation: slower cigarette, ash flick, smoke, stream exhale, no smile, sip lips."""
from __future__ import annotations
import os, random
from unittest.mock import MagicMock, patch
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPoint, QCoreApplication, QEvent
from PySide6.QtGui import QImage, QPainter, QColor


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ('HOME', 'LOCALAPPDATA', 'APPDATA', 'XDG_DATA_HOME'):
        monkeypatch.setenv(key, str(tmp_path / key))
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        yield


def render(widget):
    im = QImage(widget.width(), widget.height(), QImage.Format_ARGB32_Premultiplied); im.fill(Qt.transparent)
    p = QPainter(im); widget.render(p, QPoint()); p.end(); return im


def make(app, coffee, cigarette, seed=185):
    from jarvis.ui.robot_widget import RobotWidget
    random.seed(seed)
    r = RobotWidget(); r.resize(520, 340); r._timer.stop(); r.set_accessories(coffee=coffee, cigarette=cigarette)
    render(r)
    return r


def close(r):
    r.close(); r.deleteLater(); QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def step(r, seconds, paint=False, every=6):
    for i in range(round(seconds * 60)):
        r._elapsed += 1 / 60; r._advance_accessories177(1 / 60); r._smooth_face(1 / 60)
        if paint and i % every == 0:
            render(r)


def test_cigarette_comes_out_slower_and_eases_in(app):
    r = make(app, False, True)
    try:
        rise, hold, lower = r._ACC_TIMING177['cigarette']
        assert rise >= 2.0 and lower >= 1.6 and hold >= 3.5
        r._acc177['cigarette']['start'] = r._elapsed
        samples = []
        for _ in range(round(rise * 60)):
            r._elapsed += 1 / 60
            samples.append(r._accessory_lift177('cigarette'))
        # gentle start and end, no jumps between frames
        assert samples[5] < 0.01 and samples[-1] > 0.99
        assert max(b - a for a, b in zip(samples, samples[1:])) < 0.02
    finally:
        close(r)


@pytest.mark.parametrize('coffee', [False, True])
def test_ash_flick_once_per_break_with_falling_ash(app, coffee):
    r = make(app, coffee, True)
    try:
        r._acc177['cigarette']['next'] = 0.0; r._acc177['coffee']['next'] = 0.0 if coffee else 1e9; r._wisp_next181 = 1e9
        flicked = []
        ash_seen = 0
        for i in range(round(12 * 60)):
            r._elapsed += 1 / 60; r._advance_accessories177(1 / 60); r._smooth_face(1 / 60)
            if i % 3 == 0:
                render(r)                       # paint updates the ember position
            if r._flick_shape185() != 0.0:
                flicked.append(r._elapsed)
            ash_seen = max(ash_seen, sum(1 for p in r._particles177 if p[10] == 2.0))
        assert flicked, 'no ash flick happened during the smoke break'
        assert flicked[-1] - flicked[0] < r._FLICK_SECONDS185 + 0.05   # one flick per break
        assert 4 <= ash_seen <= 6
    finally:
        close(r)


def test_stream_exhale_is_slow_and_even(app):
    r = make(app, False, True)
    try:
        render(r)
        assert r._mouth177 is not None
        r._begin_exhale184('stream')
        assert r._exhale_until177 - r._elapsed >= 2.6
        r._particles177.clear()
        for _ in range(round(2.8 * 60)):
            r._elapsed += 1 / 60
            r._emit_particles177(1 / 60, {'coffee': False, 'cigarette': False})
        jets = [p for p in r._particles177 if p[10] == 1.0]
        scale = r._mouth177[2]
        assert len(jets) >= 30
        assert max((p[2] ** 2 + p[3] ** 2) ** 0.5 for p in jets) <= 19.0 * scale
    finally:
        close(r)


def test_ember_smoke_more_visible_and_slower(app):
    r = make(app, False, True)
    try:
        render(r)
        r._particles177.clear(); r._exhale_until177 = 0.0
        for _ in range(120):
            r._elapsed += 1 / 60; r._emit_particles177(1 / 60, {'coffee': False, 'cigarette': True})
        puffs = [p for p in r._particles177 if p[10] == 1.0]
        assert puffs and min(p[8] for p in puffs) >= 50.0 and max(abs(p[3]) for p in puffs) <= 11.0 * puffs[0][11]
    finally:
        close(r)


SMILE = (180 * 16, 180 * 16)


def _smile_calls(painter):
    return [c for c in painter.drawArc.call_args_list if tuple(c.args[1:3]) == SMILE]


STATES = ['idle', 'happy', 'success', 'playful', 'dancing', 'excited', 'curious', 'confused', 'thinking',
          'listening', 'speaking', 'typing', 'sleeping', 'working', 'sad', 'error', 'alert']


@pytest.mark.parametrize('coffee,cigarette', [(True, False), (False, True), (True, True)])
def test_no_smile_with_cigarette_or_coffee_in_any_state_or_transition(app, coffee, cigarette):
    r = make(app, coffee, cigarette)
    try:
        r._acc177['cigarette']['next'] = 0.0; r._acc177['coffee']['next'] = 0.0; r._wisp_next181 = 0.0
        for previous in ('happy', 'idle'):
            for state in STATES:
                r.set_state(previous); step(r, 0.3)
                r.set_state(state)
                for action in ('none', 'smile', 'music_bop'):
                    r._idle_action = action
                    assert r._face_targets()['mouth'] <= 0.0
                    for _ in range(4):               # mid-transition frames
                        step(r, 0.25)
                        painter = MagicMock()
                        r._draw_expression(painter, 100.0, 100.0, 1.0, QColor('#57D7FF'))
                        assert not _smile_calls(painter), (state, action)
    finally:
        close(r)


def test_without_accessories_the_smile_still_exists(app):
    r = make(app, False, False)
    try:
        r.set_state('happy'); step(r, 1.0)
        painter = MagicMock()
        r._draw_expression(painter, 100.0, 100.0, 1.0, QColor('#57D7FF'))
        assert _smile_calls(painter)
    finally:
        close(r)


def test_lips_follow_the_coffee_sip(app):
    r = make(app, True, False)
    try:
        r._acc177['coffee']['start'] = r._elapsed
        rise, hold, lower = r._ACC_TIMING177['coffee']
        heights = []
        for _ in range(round((rise + hold + lower) * 60)):
            r._elapsed += 1 / 60
            painter = MagicMock()
            r._draw_expression(painter, 100.0, 100.0, 1.0, QColor('#57D7FF'))
            ellipses = painter.drawEllipse.call_args_list
            lip = [c for c in ellipses if abs(c.args[0].y() - 125.0) < 0.01]
            heights.append(lip[-1].args[2] if lip else 0.0)
            assert not _smile_calls(painter)
        at_rim = heights[round((rise + 0.2) * 60):round((rise + hold - 0.1) * 60)]
        assert at_rim and min(at_rim) > 1.0                     # pursed lips at the rim
        assert max(at_rim) - min(at_rim) > 0.3                  # they move with the swallow
        assert heights[0] == 0.0 and heights[-1] == 0.0         # relaxed before and after
    finally:
        close(r)


def test_frames_render_through_a_full_combined_break(app):
    r = make(app, True, True)
    try:
        r._acc177['cigarette']['next'] = 0.0; r._acc177['coffee']['next'] = 0.0; r._wisp_next181 = 1e9
        step(r, 16.0, paint=True, every=4)
        assert len(r._particles177) <= 150
    finally:
        close(r)


def test_accessories_use_60hz_only_while_something_moves(app):
    from jarvis.ui.robot_widget import RobotWidget
    random.seed(5)
    r = RobotWidget(); r.resize(520, 340); r.show(); app.processEvents()
    try:
        r.set_target_fps(60) if hasattr(r, 'set_target_fps') else r.set_frame_rate(60)
        r.set_accessories(coffee=True, cigarette=True)
        r.set_state('idle'); r._idle_action = 'none'; r._next_idle_action = 1e9
        r._acc177['cigarette']['next'] = 1e9; r._acc177['coffee']['next'] = 1e9; r._wisp_next181 = 1e9
        r._exhale_until177 = 0.0; r._flick_started185 = -10.0; r._elapsed = 50.0
        r._advance_accessories177(1 / 60)
        # 186 ROBOT185-TIMING-REVIEW: drifting smoke between breaks keeps the agreed 60 Hz.
        assert r._timer.interval() == 16
        r._acc177['cigarette']['next'] = 0.0; r._acc177['coffee']['next'] = 0.0
        step(r, 0.2)
        assert r._timer.interval() == 16          # the break (hand, exhale, cup) runs at 60 Hz
    finally:
        close(r)

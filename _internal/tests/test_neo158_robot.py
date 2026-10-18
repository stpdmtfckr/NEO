"""Real Qt rendering and lifecycle checks for the lightweight avatar additions."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from jarvis.ui.robot_widget import RobotWidget
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def robot(app):
    widget = RobotWidget()
    widget.resize(520, 300)
    yield widget
    widget.hide()
    widget.deleteLater()
    app.processEvents()


def render(robot, *, dpr=1.0):
    image = QImage(int(robot.width() * dpr), int(robot.height() * dpr), QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(dpr)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        robot.render(painter, QPoint())
    finally:
        painter.end()
    return image


def settled(robot, elapsed=3.0):
    robot._elapsed = elapsed
    robot._phase = elapsed * 0.7
    for _ in range(24):
        robot._smooth_pose(0.05)
        robot._smooth_face(0.05)


def test_no_hidden_render_timer_and_no_extra_animation_clocks(robot, app):
    assert not robot._timer.isActive()
    assert len(robot.findChildren(QTimer)) == 1
    robot.show()
    robot.set_state("typing")
    app.processEvents()
    assert robot._timer.isActive()
    assert robot._timer.interval() >= 16
    robot.hide()
    assert not robot._timer.isActive()
    robot.set_reply_activity(True, "A public reply")
    assert not robot._timer.isActive()
    assert len(robot.findChildren(QTimer)) == 1


def test_sleeping_uses_lower_frame_budget_and_respects_user_cap(robot, app):
    robot.show()
    robot.set_state("sleeping")
    assert robot._timer.interval() == 33
    robot.set_state("working")
    assert robot._timer.interval() == 16
    robot.set_frame_rate(30)
    robot.set_state("speaking")
    assert robot._timer.interval() == 33


@pytest.mark.parametrize("stop_state", ["idle", "listening", "error", "sleeping"])
def test_legacy_reply_hook_is_inert_and_retains_no_response(robot, stop_state):
    robot.set_state("typing")
    robot.set_reply_activity(True, "Do not retain a long reply " * 1000)
    assert robot.state() in {"typing", "speaking"}
    assert not any(isinstance(value, str) and "Do not retain" in value for value in vars(robot).values())
    robot.set_state(stop_state)
    assert not hasattr(robot, "_draw_reply_bubble")
    assert robot.state() not in {"typing", "speaking"}
    robot.set_state("thinking")
    assert robot.state() not in {"typing", "speaking"}  # No stale cloud in an unrelated turn.


def test_typing_and_speaking_keep_existing_face_animation(robot):
    for state in ("typing", "speaking"):
        robot.set_state(state)
        settled(robot)
        assert robot.state() in {"typing", "speaking"}
        picture = render(robot)
        robot.set_state("idle")
        settled(robot)
        assert picture != render(robot)


@pytest.mark.parametrize("size,dpr", [((520, 300), 1.0), ((240, 130), 1.5), ((160, 72), 2.0), ((1000, 600), 2.0)])
def test_typing_robot_renders_inside_stage_at_small_and_high_dpi_sizes(robot, size, dpr):
    robot.resize(*size)
    robot.set_state("typing")
    settled(robot)
    picture = render(robot, dpr=dpr)
    # Removing the cloud must not break the robot's stage fitting.
    bg = robot._background.rgb()
    assert all(picture.pixel(picture.width() - 1, y) == bg for y in range(picture.height()))
    assert picture != QImage(picture.size(), picture.format())


@pytest.mark.parametrize("scene", ["paper_plane", "shoulder_roll"])
def test_new_idle_scenes_are_short_animated_and_interruptible(robot, scene):
    robot.start_idle_scene(scene=scene, duration=60.0)
    assert robot._idle_action == scene
    assert robot._idle_action_until - robot._idle_action_started_at <= 12.0
    settled(robot, 2.5)
    before = render(robot)
    settled(robot, 4.0)
    assert before != render(robot)
    robot.set_state("listening")
    assert robot._idle_action == "none"


def test_custom_theme_and_transparent_background_render(robot):
    robot.set_transparent_background(True)
    robot.set_colors(**{key: "#EEEEEE" if key in {"screen", "eye"} else color.name()
                        for key, color in robot._colors.items()})
    robot.set_state("typing")
    settled(robot)
    picture = render(robot, dpr=2.0)
    assert picture.pixelColor(0, 0).alpha() == 0
    # The light theme remains opaque on the robot while the stage stays transparent.
    dark_pixels = sum(picture.pixelColor(x, y).alpha() > 150 and picture.pixelColor(x, y).lightnessF() > 0.65
                      for x in range(200, 850, 2) for y in range(140, 420, 2))
    assert dark_pixels > 10


def test_work_variations_and_sleep_breathing_draw_without_io(robot, monkeypatch):
    import builtins
    monkeypatch.setattr(builtins, "open", lambda *a, **k: pytest.fail("painting attempted file I/O"))
    robot.set_state("working")
    settled(robot, 2.0)
    gears = render(robot)
    settled(robot, 10.0)
    assert gears != render(robot)
    robot.set_state("sleeping")
    settled(robot, 11.0)
    dreaming = render(robot)
    settled(robot, 14.0)
    assert dreaming != render(robot)

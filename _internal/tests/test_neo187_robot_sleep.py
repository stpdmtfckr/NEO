"""NEO 187 HREQ-006: sleep waits for the current idle scene; no glow pool under the hand."""
import inspect

import pytest


@pytest.fixture
def robot(qapp):
    from jarvis.ui.robot_widget import RobotWidget
    widget = RobotWidget()
    yield widget
    widget.deleteLater()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_sleep_waits_for_running_scene_then_falls_asleep(robot):
    robot.set_state("idle")
    robot.start_idle_scene(scene="hologram", duration=40)
    robot.set_state("sleeping")
    assert robot.state() == "idle" and robot._sleep_deferred187
    assert robot._idle_action_until <= robot._elapsed + 6.0 + 1e-6  # long scene ends through its outro
    robot.finish_idle_scene()
    assert robot.state() == "sleeping" and not robot._sleep_deferred187


def test_other_state_cancels_pending_sleep(robot):
    robot.set_state("idle")
    robot.start_idle_scene(scene="galaxy", duration=30)
    robot.set_state("sleeping")
    robot.set_state("listening")
    assert robot.state() == "listening" and not robot._sleep_deferred187


def test_sleep_without_scene_is_immediate(robot):
    robot.set_state("idle")
    robot.finish_idle_scene()
    robot.set_state("sleeping")
    assert robot.state() == "sleeping"


def test_no_glow_pool_under_the_magnetic_hand():
    from jarvis.ui.robot_widget import RobotWidget
    source = inspect.getsource(RobotWidget._draw_mag_hand181)
    assert "center.y() + 9 * u" not in source


def test_hist078_cloud_layer_is_painted_over_the_moon(robot, monkeypatch):
    from PySide6.QtGui import QImage
    from jarvis.ui import ambience180
    calls = []
    original = ambience180.draw
    monkeypatch.setattr(ambience180, "draw", lambda r, p, mode, w, h, t: (calls.append((mode, p.hasClipping())), original(r, p, mode, w, h, t)))
    robot.resize(520, 300)
    robot._active_animation = "cloudy"
    robot.set_state("idle"); robot.finish_idle_scene(); robot.set_state("sleeping")
    image = QImage(520, 300, QImage.Format_ARGB32_Premultiplied)
    robot.render(image)
    assert ("cloudy", True) in calls   # the extra pass is clipped to the moon

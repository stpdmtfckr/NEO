"""NEO 180: crisp window icons, app icon frames, robot hands/cigarette, new animations,
Settings «Apply» without needless restyling, Discord /play now|next|last."""
from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _opaque(image: QImage) -> int:
    return sum(1 for x in range(image.width()) for y in range(image.height()) if image.pixelColor(x, y).alpha() > 120)


@pytest.mark.parametrize("kind", ["logs", "settings", "minimize", "close", "mic", "mic_muted", "send", "stop"])
@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5])
def test_window_icons_are_drawn_for_the_exact_size_and_scale(app, kind, scale):
    from jarvis.ui.icons180 import icon
    pix = icon(kind, QColor("#EAF2FF"), QColor("#57D7FF")).pixmap(QSize(14, 14), scale)
    assert abs(pix.devicePixelRatio() - scale) < 1e-6
    assert pix.width() == round(14 * scale)              # rendered at device pixels, not scaled down from 64 px
    assert _opaque(pix.toImage()) >= 6


def test_main_window_buttons_carry_no_glyph_text():
    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    for glyph in ('QPushButton("⚙")', 'QPushButton("−")', 'QPushButton("×")'):
        assert glyph not in source


def test_app_icon_small_frames_are_classic_bitmaps():
    data = (ROOT / "assets" / "neo_jarvis.ico").read_bytes()
    count = struct.unpack("<H", data[4:6])[0]
    sizes = []
    for i in range(count):
        w, _h, _c, _r, _p, bpp, size, offset = struct.unpack("<BBBBHHII", data[6 + 16 * i:22 + 16 * i])
        w = w or 256
        sizes.append(w)
        head = data[offset:offset + 4]
        assert head == (b"\x89PNG" if w >= 256 else b"(\x00\x00\x00"), w   # BITMAPINFOHEADER below 256 px
        assert bpp == 32
    assert {16, 24, 32, 48, 256} <= set(sizes)


def test_style_sheet_is_only_reapplied_when_it_really_changes(app):
    from jarvis.ui.main_window import _set_qss180
    widget = QWidget()
    calls = []
    original = widget.setStyleSheet
    widget.setStyleSheet = lambda text: (calls.append(text), original(text))
    _set_qss180(widget, "QWidget { color:#EAF2FF; }")
    _set_qss180(widget, "QWidget { color:#eaf2ff; }")          # same colour, other case: no restyle
    _set_qss180(widget, "QWidget { color:#EAF2FF; }")
    assert len(calls) == 1
    _set_qss180(widget, "QWidget { color:#FF0000; }")
    assert len(calls) == 2


def test_main_window_retranslation_skips_the_open_settings_dialog(app):
    from jarvis.i18n import translate_widget_tree
    root = QWidget()
    own = QLabel("Настройки", root)
    dialog = QDialog(root)
    inner = QLabel("Настройки", dialog)
    translate_widget_tree(root, "en", exclude=(dialog,))
    assert own.text() == "Settings"
    assert inner.text() == "Настройки"
    source = (ROOT / "jarvis" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert 'translate_widget_tree(self, code, exclude=(getattr(self, "_settings_dialog", None),))' in source


def _robot(app, w=520, h=340):
    from jarvis.ui.robot_widget import RobotWidget
    robot = RobotWidget()
    robot.resize(w, h)
    robot.set_background("#0C1017")
    return robot


def _run(robot, seconds, paint=True):
    image = QImage(robot.width(), robot.height(), QImage.Format_ARGB32_Premultiplied)
    for _ in range(int(seconds * 60)):
        robot._last_tick -= 1 / 60
        robot._advance_animation()
        if paint:
            image.fill(QColor("#0C1017"))
            painter = QPainter(image)
            robot.render(painter, QPoint())
            painter.end()
    return image


def test_the_cigarette_is_in_the_mouth_not_in_a_hand(app):
    robot = _robot(app)
    robot.set_accessories(coffee=False, cigarette=True)
    _run(robot, 1.0)
    head_x, head_y, _tilt = robot._head_frame177
    scale = robot._last_scale177
    ember_x, ember_y, _s = robot._ember177
    assert head_x < ember_x < head_x + 80 * scale          # sticks out of the right corner of the mouth
    assert head_y + 15 * scale < ember_y < head_y + 60 * scale
    source = (ROOT / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
    block = source[source.index("    def _draw_cigarette177("):source.index("    def _draw_expression(")]
    assert "_draw_arm177" not in block                      # no arm holds it any more
    assert "_draw_tech_cigarette181" in block and "_draw_mag_hand181" in block


def test_the_coffee_arm_is_mechanical(app):
    source = (ROOT / "jarvis" / "ui" / "robot_widget.py").read_text(encoding="utf-8")
    # NEO 181: no arm any more — a levitating magnetic manipulator holds the mug
    assert "def _draw_mag_hand181(" in source and "def _capsule180(" not in source and "def _draw_gripper180(" not in source
    robot = _robot(app)
    robot.set_accessories(coffee=True, cigarette=False)
    assert not _run(robot, 0.5).isNull()


@pytest.mark.parametrize("mode", ["rain", "snow", "starfall", "starry_sky", "cloudy", "fireflies"])
def test_every_stage_weather_paints_and_moves(app, mode):
    robot = _robot(app)
    robot.set_active_animation(mode)

    def frame(t):
        robot._elapsed = t
        image = QImage(robot.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        robot._draw_active_animation(painter)
        painter.end()
        return image

    a, b = frame(2.0), frame(5.3)
    assert a != b
    lit = sum(1 for x in range(0, a.width(), 3) for y in range(0, a.height(), 3) if a.pixelColor(x, y).alpha() > 8)
    assert lit > 10


@pytest.mark.parametrize("state", ["listening", "thinking", "working", "searching", "browsing", "reading",
                                   "scanning", "coding", "happy", "confused", "typing", "sleeping"])
def test_every_state_has_a_lit_effect(app, state):
    from jarvis.ui import state_fx180
    robot = _robot(app)
    robot._elapsed = 3.0
    image = QImage(520, 340, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    assert state_fx180.draw(robot, painter, state, 260.0, 160.0, 0.9, QColor("#57D7FF"))
    painter.end()
    assert _opaque(image.copy(0, 0, 520, 340).scaled(260, 170)) > 3


class _Nav:
    """Only what _handle_music_panel_action needs, recording the queue calls."""

    def __init__(self, *, playing=True):
        self.config = SimpleNamespace(get=lambda key, default=None: default)
        self.calls = []
        self.playing = playing

    def _background_profile(self, server):
        return {"name": server}

    def music_queue_status(self, server=""):
        return {"connected": True, "playing": self.playing}

    def music_queue_add(self, query, server="", *, next_up=False, requester=""):
        self.calls.append(("add", query, next_up))
        if not self.playing:
            return {"queue_started_immediately": True, "item": {"title": query}}
        return {"queued": True, "position": 1 if next_up else 4, "item": {"title": query}}

    def music_skip(self, server=""):
        self.calls.append(("skip",))
        return {"verified": True}


@pytest.mark.parametrize("mode,calls,start", [
    ("last", [("add", "song", False)], "Добавлено в очередь"),
    ("next", [("add", "song", True)], "Следующей в очереди"),
    ("now", [("add", "song", True), ("skip",)], "Включаю сейчас"),
])
def test_play_now_next_last_place_the_song(mode, calls, start, monkeypatch):
    from jarvis.integrations import discord_navigation
    monkeypatch.setattr(discord_navigation, "publish_now_playing", lambda *a, **k: None)
    nav = _Nav()
    result = discord_navigation.ReliableDiscordAdapter._handle_music_panel_action(
        nav, {"name": "Server"}, "play", {"query": "song", "requester": "Ann", "mode": mode})
    assert nav.calls == calls
    assert result["message"].startswith(start)


def test_play_now_with_an_empty_queue_just_starts_the_song(monkeypatch):
    from jarvis.integrations import discord_navigation
    monkeypatch.setattr(discord_navigation, "publish_now_playing", lambda *a, **k: None)
    nav = _Nav(playing=False)
    nav.music_queue_status = lambda server="": {"connected": True}
    result = discord_navigation.ReliableDiscordAdapter._handle_music_panel_action(
        nav, {"name": "Server"}, "play", {"query": "song", "mode": "now"})
    assert nav.calls == [("add", "song", True)]                # nothing to skip
    assert result["message"].startswith("Включаю сейчас")


def test_discord_offers_play_now_next_last(tmp_path):
    import discord
    from jarvis.integrations.background.discord_bot import DiscordBackgroundBot
    bot = DiscordBackgroundBot(SimpleNamespace(get=lambda key, default=None: default, paths=SimpleNamespace(root=tmp_path)))
    seen = []
    bot.set_music_control_handler(lambda profile, action, payload: seen.append((action, payload["mode"], payload["query"])))
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        client = bot._create_gateway_client()
        bot._client = client
        tree = client._connection._command_tree
        group = tree.get_command("play")
        assert isinstance(group, discord.app_commands.Group)
        assert {c.name for c in group.commands} == {"now", "next", "last"}

        class Response:
            async def defer(self, **_):
                return None

        interaction = SimpleNamespace(response=Response(), guild=SimpleNamespace(id=1, name="S"),
                                      user=SimpleNamespace(display_name="Ann"))

        async def deleted():
            return None
        interaction.delete_original_response = deleted
        for name in ("now", "next", "last"):
            loop.run_until_complete(group.get_command(name).callback(interaction, "song"))
    finally:
        loop.close()
        asyncio.set_event_loop(None)
    assert seen == [("play", "now", "song"), ("play", "next", "song"), ("play", "last", "song")]


def test_smoke_breaks_vary_and_sleep_keeps_the_cigarette_in_the_mouth(app, monkeypatch):
    """NEO 181: awake, the magnetic hand takes the cigarette out and the robot breathes out rings,
    a cloud or a jet; asleep, it never takes it out."""
    import jarvis.ui.robot_widget as rw
    styles = set()
    for style in ("rings", "cloud", "stream"):
        monkeypatch.setattr(rw.random, "choice", lambda options, st=style: st if st in options else options[0])
        robot = _robot(app)
        robot.set_accessories(coffee=False, cigarette=True)
        robot._acc177["cigarette"]["next"] = 0.0
        robot._wisp_next181 = 10 ** 6
        _run(robot, 2.0)
        assert robot._cig_out181 > 0.9                       # out of the mouth, in the hand
        styles.add(robot._exhale_style181)
        if style == "rings":
            assert any(p[10] == 3.0 for p in robot._particles177)
        else:
            assert any(p[10] == 1.0 for p in robot._particles177)
    assert styles == {"rings", "cloud", "stream"}
    robot = _robot(app)
    robot.set_accessories(coffee=False, cigarette=True)
    robot.set_state("sleeping")
    robot._acc177["cigarette"]["next"] = 0.0
    _run(robot, 3.0)
    assert robot._accessory_lift177("cigarette") == 0.0 and robot._cig_out181 == 0.0
    assert robot._ember177 is not None                        # still in the mouth, still smouldering

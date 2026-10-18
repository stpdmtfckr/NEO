"""Current requirements shared by migrated historical source contracts.

The immutable pre-release tests remain in the original project archive. These
helpers retain their invariants while accepting the explicitly requested update.
"""
from pathlib import Path
import ast
import re

INTERNAL = Path(__file__).resolve().parents[1]


def assert_release(test, version):
    header = version.splitlines()[0]
    test.assertRegex(header, r"^NEO v4\.0\.20\.(\d+)-[A-Z0-9-]+$")
    test.assertGreaterEqual(int(header.split(".")[3].split("-")[0]), 144)
    test.assertNotIn("JARVIS", header)


def assert_existing_window_visibility(test, source):
    body = source[source.index("def _set_runtime_topmost"):source.index("def _try_local_voice_control")]
    # Explicit show raises once; it must not silently pin NEO permanently.
    for required in ("self.hide()", "self.show()", "HWND_TOP", "SWP_NOACTIVATE", "Qt.WA_ShowWithoutActivating"):
        test.assertIn(required, body)
    for forbidden in ("HWND_TOPMOST", "self.setWindowFlags(", "self.close()", "showMinimized", "_hide_to_tray", ".quit("):
        test.assertNotIn(forbidden, body)
    test.assertIn("_window_toggle_guard_until", source)


def assert_headphone_geometry(test, source):
    # Read painter constants without importing Qt or pinning a particular artwork.
    left = float(re.search(r"left_cup = QRectF\(cx - ([\d.]+) \* scale", source)[1])
    right = float(re.search(r"right_cup = QRectF\(cx \+ ([\d.]+) \* scale", source)[1])
    width = float(re.search(r"cup_w = ([\d.]+) \* scale", source)[1])
    test.assertLessEqual(-left, -126.0)  # both cups cover the real outer ears
    test.assertGreaterEqual(-left + width, -98.0)
    test.assertLessEqual(right, 98.0)
    test.assertGreaterEqual(right + width, 126.0)
    test.assertGreater(-left + width, -left)
    test.assertLess(-left + width, 0.0)  # cups never cover the central face
    test.assertGreater(right, 0.0)
    test.assertIn("_draw_idle_toy(painter, head_x, head_y", source)
    return left, right, width


def assert_youtube_menu(test, source):
    menu = source.split("def _show_youtube_menu", 1)[1].split("def _show_youtube_quality_menu", 1)[0]
    for allowed in ("Поиск видео", "Пауза", "Продолжить", "Перейти на время", "На весь экран", "Растянутый"):
        test.assertIn(allowed, menu)
    for unsupported in ("Скорость", "Качество", "Следующее видео", "Предыдущее видео", "Следующий Shorts", "Предыдущий Shorts"):
        test.assertNotIn(unsupported, menu)


def home_callbacks(source):
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_home_keyboard")
    return [node.value for node in ast.walk(function) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith(("ui:", "a:", "i:"))]


def run_discord_wave_refill(queued: int = 2, rotor_tracks: int = 20, minimum: int = 8):
    """Run the real Discord My Wave background refill with a fake Yandex Rotor.

    Returns (queue state after the refill, Rotor call kwargs). Replaces the old
    source-text markers of HOTFIX129/132 («minimum = max(8, min(12», «del items[12:]»)
    with the behaviour they protected: a cushion of at least 8 queued tracks and a
    hard bound of 12, without the current or recently played tracks.
    """
    import threading
    import time
    from jarvis.integrations.discord_navigation import ReliableDiscordAdapter

    adapter = object.__new__(ReliableDiscordAdapter)
    adapter._queue_lock = threading.RLock()
    profile = {"server": "test"}
    key = ReliableDiscordAdapter._profile_key(profile)
    items = [{"track_id": f"q{i}", "title": f"Queued {i}", "artist": "A"} for i in range(queued)]
    adapter._queues = {key: {
        "source_mode": "wave", "source_epoch": 1, "items": items,
        "current": {"track_id": "now", "title": "Now", "artist": "A"},
        "source_data": {"last_completed_id": "done", "recent_ids": ["recent"], "requester": "me"},
    }}
    calls = []

    def wave_tracks(**kw):
        calls.append(kw)
        tracks = [{"track_id": "now", "title": "Now", "artist": "A"}, {"track_id": "recent", "title": "Recent", "artist": "A"}]
        tracks += [{"track_id": f"r{i}", "title": f"Rotor {i}", "artist": "B"} for i in range(rotor_tracks)]
        return {"tracks": tracks, "batch_id": "b"}

    adapter._yandex = type("Rotor", (), {"wave_tracks": staticmethod(wave_tracks)})()
    adapter._wave_avoid_history = lambda recent: list(recent)
    adapter._wave_entries = lambda wave, requester="": [dict(t) for t in wave.get("tracks") or []]
    adapter._broadcast_queue_snapshot = lambda *a, **kw: None
    adapter._prefetch_next_queue_item = lambda state: None
    adapter._ensure_wave_queue_depth_background(profile, minimum)
    deadline = time.time() + 3.0
    while adapter._queues[key].get("wave_background_refill_active") and time.time() < deadline:
        time.sleep(0.01)
    return adapter._queues[key], calls

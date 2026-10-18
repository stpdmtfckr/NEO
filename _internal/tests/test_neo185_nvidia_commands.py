"""NEO185: replay/clip/record commands may start NVIDIA App in the background (only when the setting is on)."""
from __future__ import annotations
from types import SimpleNamespace

from jarvis.integrations.nvidia_startup184 import CaptureAppStartup, InstalledApp, ensure_overlay_for_command
from jarvis.integrations.nvidia_capture import NvidiaCapture
from jarvis.integrations.instant_replay169 import InstantReplay


class Config:
    def __init__(self, **values):
        self.values = {'app.language': 'en', 'gaming.enabled': True, **values}
    def get(self, key, default=None):
        return self.values.get(key, default)


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, seconds): self.t += seconds


def startup(launches, running=False):
    return CaptureAppStartup(running=lambda: running, installed=lambda: InstalledApp('NVIDIA App', 'C:/x/NVIDIA app.exe'),
                             launch=lambda app: launches.append(app.name), is_windows=True)


def test_nothing_happens_when_the_setting_is_off():
    launches = []
    assert not ensure_overlay_for_command(Config(), lambda: False, startup=startup(launches))
    assert launches == []


def test_launches_minimised_app_and_waits_for_overlay():
    launches, clock = [], Clock()
    overlay = lambda: clock.t >= 4
    ok = ensure_overlay_for_command(Config(**{'gaming.instant_replay.launch_for_commands': True}), overlay,
                                    startup=startup(launches), sleep=clock.sleep, clock=clock)
    assert ok and launches == ['NVIDIA App'] and 4 <= clock.t < 6


def test_gives_up_after_the_wait_limit():
    launches, clock = [], Clock()
    ok = ensure_overlay_for_command(Config(**{'gaming.instant_replay.launch_for_commands': True,
                                              'gaming.instant_replay.overlay_wait_seconds': 10}),
                                    lambda: False, startup=startup(launches), sleep=clock.sleep, clock=clock)
    assert not ok and launches == ['NVIDIA App'] and clock.t >= 10


def test_clip_command_presses_hotkey_after_background_start():
    pressed, state = [], {'overlay': False}
    def ensure():
        state['overlay'] = True
        return True
    capture = NvidiaCapture(Config(), press=pressed.append, overlay=lambda: state['overlay'], is_windows=True,
                            wait=lambda *a, **k: None, sleep=lambda s: None, ensure_overlay=ensure)
    capture.run('clip')
    assert pressed == ['alt+f10']


def test_clip_command_still_refuses_without_overlay():
    pressed = []
    capture = NvidiaCapture(Config(), press=pressed.append, overlay=lambda: False, is_windows=True,
                            wait=lambda *a, **k: None, sleep=lambda s: None, ensure_overlay=lambda: False)
    ok, text, data = capture.run('clip')
    assert not ok and pressed == [] and data['error_kind'] == 'disabled'


def test_replay_on_command_starts_app_then_turns_replay_on():
    pressed, state = [], {'overlay': False, 'replay': None}
    def ensure():
        state.update(overlay=True, replay=False)
        return True
    def press(key):
        pressed.append(key); state['replay'] = not state['replay']
    replay = InstantReplay(Config(), press=press, overlay=lambda: state['overlay'], reader=lambda: state['replay'],
                           is_windows=True, sleep=lambda s: None, ensure_overlay=ensure)
    ok, text, data = replay.run('replay_on')
    assert ok and pressed == ['alt+shift+f10'] and state['replay'] is True


def test_game_watcher_path_does_not_use_the_command_launch():
    calls = []
    replay = InstantReplay(Config(), press=lambda k: None, overlay=lambda: False, reader=lambda: None,
                           is_windows=True, sleep=lambda s: None, ensure_overlay=lambda: calls.append(1) or False)
    replay.run('replay_on', guard=lambda: True)
    assert calls == []

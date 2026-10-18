"""NEO 187 REC-AUTO-RESTORE-OMITTED-REVISION-01 (inert player, fake clock, no network)."""
import pytest

from jarvis.integrations import yandex_music as ym
from tests.test_neo186_music_queue import _start, _titles, io_guard, rig  # noqa: F401


def _failing_candidates(rig, edit):
    original = rig.player.play

    def play(url, **kw):
        if kw.get('title') in {'B', 'C'}:
            if edit is not None and not getattr(rig, 'edited', False):
                rig.edited = True
                edit()
            raise RuntimeError('candidate could not be prepared')
        return original(url, **kw)
    rig.player.play = play


@pytest.mark.parametrize('edited', [True, False])
def test_restored_current_keeps_newer_confirmed_queue_order(rig, edited):           # ARQ-R01 / R02
    _start(rig, ['A', 'B', 'C'])
    rig.clock.t += 30

    def edit():
        with rig.obj._playback_lock:
            rig.obj._station_queue = list(reversed(rig.obj._station_queue))
            rig.obj._bump_queue_revision()
    _failing_candidates(rig, edit if edited else None)
    with pytest.raises(Exception):
        rig.obj._next_background(auto=True)
    assert ym._track_title(rig.obj._current_track_obj) == 'A'          # continuity restore happened
    assert _titles(rig.obj._station_queue) == (['C', 'B'] if edited else ['B', 'C'])

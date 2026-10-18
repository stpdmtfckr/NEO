"""NEO 187 REC-MONITOR-PERSISTENT-EOF-RETRY-DEADLINE-01 (inert player, fake clock)."""
from tests.test_neo186_music_queue import Stop, _start, io_guard, rig  # noqa: F401


def test_persistent_eof_waits_for_the_scheduled_retry_deadline(rig, monkeypatch):    # MER-R01/R02/R04
    rig.player.duration_ms = 60000
    _start(rig, ['A', 'B', 'C'])
    calls = []
    stop = Stop(rig.clock, 1200)

    def auto_next(serial, epoch):
        calls.append(rig.clock.t)
        if len(calls) < 3:
            raise RuntimeError('provider failed before any effect')
        stop.set()
        return {'verified': True}
    monkeypatch.setattr(rig.obj, '_observed_auto_next', auto_next)
    rig.obj._station_monitor(stop)
    assert len(calls) == 3, calls                       # EOF polls never add attempts
    first, second = calls[1] - calls[0], calls[2] - calls[1]
    assert first >= 1.2 - 1e-6 and second >= 2.4 - 1e-6   # count grows: 1.2 s, then 2.4 s backoff

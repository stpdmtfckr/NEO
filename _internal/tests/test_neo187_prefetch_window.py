"""NEO 187 REC-PREFETCH-WINDOW-START-OWNERSHIP-01: one prefetch-window owner."""
import pytest

from jarvis.integrations import yandex_music as ym
from tests.test_media_reliability import Config, Track, local_adapter  # noqa: F401


def _start(yandex, tracks):
    type(yandex)._start_prefetch_window(yandex, tracks)


class _FakeThread:
    started = []

    def __init__(self, target=None, name='', daemon=None):
        self.target, self.alive = target, False
        self.on_start = None

    def is_alive(self):
        return self.alive

    def start(self):
        if self.on_start:
            self.on_start()
        self.alive = True
        _FakeThread.started.append(self)


@pytest.fixture
def adapter(local_adapter, monkeypatch):
    yandex, _ = local_adapter
    _FakeThread.started = []
    created = []

    def factory(*args, **kwargs):
        thread = _FakeThread(*args, **kwargs)
        created.append(thread)
        return thread
    monkeypatch.setattr(ym.threading, 'Thread', factory)
    return yandex, created


def test_reentrant_start_does_not_create_a_second_worker(adapter):                # PWS-R01
    yandex, created = adapter
    first, second = [Track('A')], [Track('B')]
    original = _FakeThread.start

    def start_with_callback(self):
        if len(created) == 1 and not getattr(self, '_reentered', False):
            self._reentered = True
            _start(yandex, second)                         # B while A is STARTING
        original(self)
    _FakeThread.start = start_with_callback
    try:
        _start(yandex, first)
    finally:
        _FakeThread.start = original
    assert len(created) == 1 and len(_FakeThread.started) == 1
    assert yandex._prefetch_window_thread is created[0]
    assert [ym._track_id(t) for t in yandex._prefetch_window_items] == ['B']        # last batch wins
    assert yandex._prefetch_window_starting is None


def test_repeat_requests_keep_one_owner_and_last_batch(adapter):                  # PWS-R02
    yandex, created = adapter
    for name in 'ABC':
        _start(yandex, [Track(name)])
    assert len(created) == 1
    assert [ym._track_id(t) for t in yandex._prefetch_window_items] == ['C']


def test_failed_start_releases_only_its_reservation(adapter):                     # PWS-R03 / R04
    yandex, created = adapter
    original = _FakeThread.start

    def boom(self):
        raise RuntimeError('cannot start')
    _FakeThread.start = boom
    try:
        with pytest.raises(RuntimeError):
            _start(yandex, [Track('A')])
    finally:
        _FakeThread.start = original
    assert yandex._prefetch_window_starting is None and yandex._prefetch_window_thread is None
    _start(yandex, [Track('B')])
    assert len(_FakeThread.started) == 1 and yandex._prefetch_window_thread is created[-1]

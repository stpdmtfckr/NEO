"""NEO 168: fewer wake decodes while the microphone hears ordinary speech."""
from jarvis.runtime.wake_budget168 import ConversationBackoff, decode_window_seconds


def test_wake_decode_window_follows_the_audio_length():
    assert decode_window_seconds(0.4) == 4
    assert decode_window_seconds(3.08) == 5
    assert decode_window_seconds(12.2) == 14
    assert decode_window_seconds(100) == 30
    assert decode_window_seconds(float('nan')) == 4


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def wake_like(text):
    return text.split()[0].casefold().startswith(('джарв', 'jarv'))


def test_backoff_starts_only_after_several_long_rejections():
    clock = Clock()
    b = ConversationBackoff(clock=clock)
    b.remember_probe('c1', 'ну и вот')
    for _ in range(2):
        b.record_rejected('ну и вот потом мы пошли туда', 2.4)
        clock.now += 2
    assert not b.active() and not b.should_skip('c1', 2.4, wake_like)
    b.record_rejected('и тогда он сказал что всё', 2.4)
    assert b.active() and b.should_skip('c1', 2.4, wake_like)


def test_backoff_never_skips_short_or_wake_like_or_unprobed_utterances():
    clock = Clock()
    b = ConversationBackoff(clock=clock)
    for _ in range(3):
        b.record_rejected('long sentence from a podcast nearby', 2.4)
    b.remember_probe('short', 'клип')
    b.remember_probe('wake', 'Джарвис открой')
    assert not b.should_skip('short', 0.9, wake_like)          # quick commands stay
    assert not b.should_skip('wake', 2.4, wake_like)           # the name at the onset
    assert not b.should_skip('no-probe', 2.4, wake_like)       # no evidence -> decode
    b.remember_probe('talk', 'и потом')
    assert b.should_skip('talk', 2.4, wake_like) and b.skipped == 1


def test_backoff_ends_after_hold_or_a_real_wake():
    clock = Clock()
    b = ConversationBackoff(clock=clock)
    for _ in range(3):
        b.record_rejected('one two three four five', 2.4)
    assert b.active()
    clock.now += 16
    assert not b.active()
    for _ in range(3):
        b.record_rejected('one two three four five', 2.4)
    b.record_wake()
    assert not b.active()


def test_short_rejections_do_not_count():
    b = ConversationBackoff(clock=Clock())
    for _ in range(5):
        b.record_rejected('да', 0.6)
        b.record_rejected('ok then', 2.4)
    assert not b.active()


def test_disabled_backoff_never_skips():
    b = ConversationBackoff(clock=Clock(), enabled=False)
    for _ in range(4):
        b.record_rejected('one two three four five', 2.4)
    b.remember_probe('c', 'и потом')
    assert not b.should_skip('c', 2.4, wake_like)

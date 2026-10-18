"""Every packaged wake spelling through the public matcher and real Qt adapters.

No microphone/ASR accuracy claim: recognizer outputs are deterministic fixtures.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from jarvis.runtime import wake178
from jarvis.wake_matcher import (match_wake_phrase, looks_wake_like,
    wake_candidate_similarity, wake_alias_is_strong, wake_alias_requires_standalone)

DEFAULTS = json.loads((Path(__file__).resolve().parents[1] / 'config/default_settings.json').read_text())
ALIASES = tuple(DEFAULTS['wake_word']['aliases'])
SUFFIXES = ('', '.', '!', '?', '...', '…')


def variants():
    for item in ALIASES:
        base = item.rstrip('.!?…')
        mixed = ''.join(c.upper() if i % 2 else c.lower() for i, c in enumerate(base))
        for case in (base.lower(), base.upper(), base.title(), mixed):
            for suffix in SUFFIXES:
                yield case + suffix


def test_all_packaged_aliases_all_case_punctuation_and_wrappers():
    assert set(wake178.aliases()) == set(ALIASES)
    for text in variants():
        for heard in (text, "['" + text + "']", '["' + text + '"]', [text]):
            assert match_wake_phrase(heard, 'NEO', aliases=ALIASES) == (True, '', 1.0), heard
            assert looks_wake_like(heard, 'NEO', aliases=ALIASES), heard
            assert wake_candidate_similarity(heard, 'NEO', aliases=ALIASES) == 1.0, heard
        assert wake_alias_is_strong(text, 'NEO'), text


@pytest.mark.parametrize('text', [
    'ну давай', 'ну, включи музыку', 'Ну... включи музыку', 'ну и что', 'ну привет',
    'Не, ну а что', 'я скажу ну', 'мы говорили: ну', 'ну ну', 'ну-ка', 'а ну-ка',
    'ну_ка', 'ну2', '2ну', 'НУЖНО', 'нуль', 'потянул', 'менуэт', 'минуту',
    'нет', 'не', 'неа', 'не-а', 'нету', 'но', 'на', 'ни', 'не о', 'неоновый',
    'нелепо', 'нелепый', 'тоннел', 'нелокальный', 'потемнело', 'позеленело',
    'знаю', 'узнаю', 'начинаю', 'вспоминаю', 'пинаю', 'нео-нацизм', 'x-Нело',
    'Нело-test', 'xxНело', 'Нелоxx', 'Нео_файл', 'файл_Нео',
    'No', 'Nah', 'nu', 'nope', 'nein', 'non', 'não',
    "['ну давай']", "['нет', 'Нело']", "['Нело', 'нет']", '[["Нело"]]',
    "['Нело'] + ['удали файл']", "['Нело',]", '[Нело]', "['Нело\\n']",
    "[__import__('os').system('echo Нело')]", {'text': 'Нело'}, ['Нело', 'нет'], [],
    'Нело' + ' ' * 4096, 'x' * 10000 + ' Нело',
])
def test_ordinary_discourse_substrings_malformed_representations_rejected(text):
    assert not match_wake_phrase(text, 'NEO', aliases=ALIASES)[0], text


@pytest.mark.parametrize('text', ['н е л о', 'Н-е-л-о', 'Н_Е_Л_О', "'ну'", '«ну»', 'ну давай', 'не о'])
def test_similarity_gates_do_not_glue_tokens_or_strip_quotes(text):
    assert not looks_wake_like(text, 'NEO', aliases=ALIASES)
    assert wake_candidate_similarity(text, 'NEO', aliases=ALIASES) == 0


def test_ambiguous_nu_needs_explicit_alias_and_standalone_context():
    assert not match_wake_phrase('Ну!', 'NEO', aliases=('NEO',))[0]
    assert not match_wake_phrase('Ну!', 'NEO', aliases=ALIASES, allow_standalone_aliases=False)[0]
    assert match_wake_phrase('Ну!', 'NEO', aliases=ALIASES)[0]
    from jarvis.runtime.utterance_purpose import strip_assistant_address
    from jarvis.integrations.intents import _strip_wake
    for text in ('ну', 'ну!', 'ну, привет', 'Ну... удали файл', 'мы сказали ну'):
        assert strip_assistant_address(text, ALIASES) == text
        assert _strip_wake(text, wake_phrase='NEO', wake_aliases=ALIASES) == text.casefold().strip(' .,!?:;"\'«»')


def test_exact_tail_keeps_payload_punctuation_and_case():
    for alias in ALIASES:
        if wake_alias_requires_standalone(alias):
            continue
        tail = 'напиши Тест, Да!'
        assert match_wake_phrase(alias + ' ' + tail, 'NEO', aliases=ALIASES)[1] == tail
    assert match_wake_phrase("['Наю! напиши Тест, Да!']", 'NEO', aliases=ALIASES)[1] == 'напиши Тест, Да!'


from tests.test_neo184_settings_mainwindow import app, window, settle


def test_actual_qt_partition_keeps_every_unicode_alias_and_wake_dispatch(window, monkeypatch):
    from jarvis.ui.main_window import _configured_wake_terms
    for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
        window.config.data['app']['language'] = code
        active = tuple(_configured_wake_terms(window.config))
        for text in variants():
            assert match_wake_phrase(text, 'NEO', aliases=active) == (True, '', 1.0), (code, text)
    window.config.data['app']['language'] = 'ru'
    accepted = []
    monkeypatch.setattr(window, '_start_command_after_acoustic_wake', lambda: accepted.append(True))
    for text in ("['Ну...']", "['Наю!']", "['НЕЛО']", 'нел!', 'НІО…', 'NÉO…'):
        window._voice_recognized(text)
    assert len(accepted) == 6
    for text in ('ну стоп', 'стоп ну', 'Ну... перестань слушать'):
        assert not window._is_continuous_stop_phrase(text)


def test_language_scoped_aliases_stay_in_their_locale(tmp_path):
    from jarvis.testing.atlas_session import AtlasConfig
    config = AtlasConfig(tmp_path / 'profile')
    config.data['wake_word']['language_aliases'] = {'ru': ['РУТЕСТ'], 'en': ['ENTEST']}
    config.data['wake_word']['aliases'].extend(['РУТЕСТ!', 'ENTEST?'])
    for code, yes, no in [('ru', 'РУТЕСТ!', 'ENTEST?'), ('en', 'ENTEST?', 'РУТЕСТ!')]:
        config.data['app']['language'] = code
        aliases = config.active_wake_aliases()
        assert match_wake_phrase(yes, 'NEO', aliases=aliases)[0]
        assert not match_wake_phrase(no, 'NEO', aliases=aliases)[0]


@pytest.mark.parametrize('text', ['neon lights', 'NEOнацизм', 'NEO_file', 'NEO-нацизм', 'ну, какой сегодня день'])
def test_actual_qt_rejected_wake_prefix_keeps_whole_transcript(window, monkeypatch, text):
    received = []
    monkeypatch.setattr(window, '_normalize_recognized_command', lambda value, **kwargs: received.append(value) or '')
    monkeypatch.setattr(window, '_start_command_after_acoustic_wake', lambda: pytest.fail('Substring triggered wake'))
    window._voice_recognized(text)
    assert received == [text]


def test_wake_tail_brackets_and_quotes_remain_literal_payload():
    for text in ('напиши [Нео]', 'напиши "Тест!"', "напиши ['ну']", 'запиши: Да?'):
        assert match_wake_phrase('Нело, ' + text, 'NEO', aliases=ALIASES)[1] == text


@pytest.mark.parametrize('text', ['Ne\u0301o!', 'NE\u0301O…', 'ＮＥＯ！', 'ＮＥＯ？'])
def test_unicode_compatibility_and_combining_marks_keep_token_boundaries(text):
    assert match_wake_phrase(text, 'NEO', aliases=ALIASES) == (True, '', 1.0)
    assert looks_wake_like(text, 'NEO', aliases=ALIASES)
    assert not match_wake_phrase(text.rstrip('!…！？') + 'n', 'NEO', aliases=ALIASES)[0]


def _weak_alias_variants():
    return [case + suffix for case in ('ну', 'НУ', 'Ну', 'нУ') for suffix in SUFFIXES]


def _inert_wake_entrypoints(window, monkeypatch):
    monkeypatch.setattr(window, '_probable_system_playback_echo', lambda *a, **k: False)
    monkeypatch.setattr(window, '_output_stop_from_voice', lambda *a: False)
    monkeypatch.setattr(window, '_voice_unlock_phrase_matches', lambda *a: False)
    monkeypatch.setattr(window, '_response_active', lambda: False)
    monkeypatch.setattr(window, '_acoustic_wake_enabled_for_current_phrase', lambda: False)
    monkeypatch.setattr(window, '_wake_hypothesis_needs_verification', lambda *a: False)
    window._voice_mode = 'wake'
    window._busy = False
    window.config.data['wake_word']['quick_commands_enabled'] = False
    window.config.data['wake_word']['quick_capture_enabled'] = False
    window._wake_rescue_pending_generation = -1
    window._wake_rescue_pending_at = 0.0


def test_actual_rolling_probe_never_promotes_nu_before_ordinary_sentence(window, monkeypatch):
    from tests.test_neo161_voice import recording
    _inert_wake_entrypoints(window, monkeypatch)
    promoted = []
    monkeypatch.setattr(window, '_wake_transcribed', lambda *args: promoted.append(args))
    rec = recording(.5)
    for text in _weak_alias_variants() + ["['Ну…']", 'ну да', 'ну включи музыку', 'ну привет', 'Не, ну а что']:
        window._fast_wake_probe_transcribed(text, window._wake_generation, rec)
    assert not promoted
    # Even a mislabelled rolling callback is not proof of utterance completion.
    rec.completed_utterance = True
    window._fast_wake_probe_transcribed('Ну!', window._wake_generation, rec)
    assert not promoted
    for text in ('Нел!', 'Нело…', 'Наю?', 'NEO', 'НІО', 'NÉO'):
        window._fast_wake_probe_transcribed(text, window._wake_generation, rec)
    assert len(promoted) == 6


def test_actual_robust_decoder_only_accepts_completed_standalone_nu(window, monkeypatch):
    from tests.test_neo161_voice import recording
    _inert_wake_entrypoints(window, monkeypatch)
    decoded = []
    current = ['']
    def decode(*args, **kwargs):
        decoded.append(current[0]); return current[0]
    recognizer = SimpleNamespace(transcribe_fast_control_probe=decode, transcribe=decode,
        transcribe_fast_wake_probe=decode, transcribe_with_language=decode,
        _control_language=lambda: 'ru', _wake_language=lambda: 'ru', _wake_retry_languages=lambda: ('en',))
    monkeypatch.setattr(window, 'wake_recognizer', recognizer)
    for seconds in (.7, 2.0):
        rec = recording(seconds)
        for complete in (False, True):
            rec.completed_utterance = complete
            for text in _weak_alias_variants() + ["['Ну…']"]:
                current[0] = text; decoded.clear()
                result = window._transcribe_wake_robust(rec)
                assert result['matched'] is complete, (text, seconds, complete, result)
                assert len(decoded) <= (2 if seconds < 1 else 1)
            for text in ('ну да', 'ну включи музыку', 'ну привет', 'Не, ну а что'):
                current[0] = text
                assert not window._transcribe_wake_robust(rec)['matched'], (text, complete)
            for text in ('Нел!', 'Нело…', 'Наю?', 'NEO', 'НІО', 'NÉO'):
                current[0] = text
                assert window._transcribe_wake_robust(rec)['matched'], (text, complete)


def test_gui_result_fallback_cannot_reaccept_a_partial_nu(window, monkeypatch):
    from tests.test_neo161_voice import recording
    _inert_wake_entrypoints(window, monkeypatch)
    class PromotionReached(Exception): pass
    monkeypatch.setattr(window, '_wake_heard173', lambda: (_ for _ in ()).throw(PromotionReached()))
    rec = recording(.7)
    for text in _weak_alias_variants() + ['ну да', 'ну включи музыку']:
        window._wake_transcribed({'matched': False, 'text': text}, window._wake_generation, rec)
    # A completed standalone utterance enters the existing promotion path.
    rec.completed_utterance = True
    with pytest.raises(PromotionReached):
        window._wake_transcribed({'matched': False, 'text': 'Ну…'}, window._wake_generation, rec)
    rec.completed_utterance = False
    with pytest.raises(PromotionReached):
        window._wake_transcribed({'matched': False, 'text': 'Нело'}, window._wake_generation, rec)


def test_partial_nu_does_not_bypass_conversation_decode_backoff(window, monkeypatch):
    from tests.test_neo161_voice import recording
    _inert_wake_entrypoints(window, monkeypatch)
    checked = []
    def should_skip(correlation, duration, onset_is_wake_like):
        for phrase in ('ну', 'Ну…', 'ну да', 'ну включи музыку'):
            assert not onset_is_wake_like(phrase), phrase
        assert onset_is_wake_like('Нело!')
        checked.append(True)
        return True
    monkeypatch.setattr(window, '_wake_backoff', SimpleNamespace(should_skip=should_skip, skipped=1))
    window._launch_wake_transcription(recording(.5), window._wake_generation)
    assert checked == [True]

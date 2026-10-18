"""NEO 177: Yandex Music never starts on its own after a failed start; source checks."""
from pathlib import Path

import pytest

from jarvis.integrations import yandex_music as ym

SOURCE = (Path(__file__).resolve().parents[1] / 'jarvis' / 'integrations' / 'yandex_music.py').read_text(encoding='utf-8')


class _Adapter:
    def __init__(self, fail_with=None):
        self.fail_with, self.abandoned = fail_with, 0

    def _abandon_failed_start177(self):
        self.abandoned += 1

    @ym._abandon_on_failure
    def my_wave(self):
        if self.fail_with:
            raise self.fail_with
        return {'playing': True}


def test_a_failed_start_drops_the_playback_request():
    adapter = _Adapter(ConnectionError('network'))
    with pytest.raises(ConnectionError):
        adapter.my_wave()
    assert adapter.abandoned == 1


def test_a_successful_or_superseded_start_keeps_the_station():
    ok = _Adapter()
    assert ok.my_wave() == {'playing': True} and ok.abandoned == 0
    newer = _Adapter(ym.PlaybackSuperseded('newer command'))
    with pytest.raises(ym.PlaybackSuperseded):
        newer.my_wave()
    assert newer.abandoned == 0


def test_every_start_command_is_protected_and_recovery_is_bounded():
    for name in ('my_wave', 'play_my_tracks', 'play_track', 'play_playlist'):
        assert f'@_abandon_on_failure\n    def {name}(' in SOURCE, name
    assert 'network_recovery_seconds' in SOURCE and 'gave up after' in SOURCE


def test_abandon_keeps_music_that_really_plays(monkeypatch):
    calls = []
    fake = type('A', (), {'_clear_background_sequence': lambda self, keep_current=True: calls.append(keep_current)})()
    monkeypatch.setattr(ym, 'local_player', lambda: type('P', (), {'status_if_running': lambda self: {'state': 'playing'}})())
    ym.YandexMusicAdapter._abandon_failed_start177(fake)
    assert calls == []
    monkeypatch.setattr(ym, 'local_player', lambda: type('P', (), {'status_if_running': lambda self: {'state': 'stopped'}})())
    ym.YandexMusicAdapter._abandon_failed_start177(fake)
    assert calls == [False]


# ---- NEO 177: NEO remembers what a misspelled request meant ----------------------------------

from types import SimpleNamespace

from jarvis.runtime import learned177


class _Config(dict):
    def __init__(self, root, **values):
        super().__init__(values)
        self.paths = SimpleNamespace(root=root)

    def get(self, key, default=None):
        return super().get(key, default)


def test_learned_phrases_are_kept_found_and_forgotten(tmp_path):
    config = _Config(tmp_path)
    assert learned177.remember(config, 'Привте!', 'привет', 'ru', 'spelling')
    assert learned177.lookup(config, 'привте', 'ru') == 'привет'
    assert learned177.lookup(config, '  ПРИВТЕ ', 'ru') == 'привет'
    assert learned177.lookup(config, 'привте', 'en') == ''
    learned177.used(config, 'привте', 'ru')
    assert learned177.count(config) == 1
    assert learned177.forget(config, 'привте', 'ru')
    assert learned177.lookup(config, 'привте', 'ru') == ''
    learned177.remember(config, 'спасиба', 'спасибо', 'ru', 'spelling')
    assert learned177.clear(config) == 1 and not (tmp_path / 'learned_phrases.json').exists()


def test_only_short_requests_that_do_not_depend_on_the_moment_are_learned(tmp_path):
    config = _Config(tmp_path)
    assert not learned177.remember(config, 'закрой его', 'закрой discord', 'ru', 'agent')
    assert not learned177.remember(config, 'close it', 'close discord', 'en', 'agent')
    assert not learned177.remember(config, 'напиши маме «я скоро буду дома»', 'x', 'ru', 'agent')
    assert not learned177.remember(config, 'раз два три четыре пять шесть семь', 'x', 'ru', 'agent')
    assert not learned177.remember(config, 'привет', 'привет', 'ru', 'spelling')
    assert not learned177.remember(_Config(tmp_path, **{'learning.remember_resolved': False}), 'привте', 'привет', 'ru', 'spelling')
    assert learned177.count(config) == 0


def test_the_store_keeps_the_most_used_phrases(tmp_path, monkeypatch):
    config = _Config(tmp_path)
    monkeypatch.setattr(learned177, 'LIMIT', 3)
    for word in ('альфа', 'бета', 'гамма'):
        learned177.remember(config, word + 'х', word, 'ru', 'typo')
    learned177.used(config, 'альфах', 'ru')
    learned177.remember(config, 'дельтах', 'дельта', 'ru', 'typo')
    assert learned177.lookup(config, 'альфах', 'ru') == 'альфа'
    assert learned177.lookup(config, 'дельтах', 'ru') == 'дельта'
    assert learned177.count(config) == 3


@pytest.mark.parametrize('code,typed,meant', [
    ('ru', 'привте', 'привет'), ('ru', 'привте, как дила', 'привет как дела'), ('ru', 'спасиба', 'спасибо'),
    ('ru', 'как настраение', 'как настроение'), ('ru', 'мне скушно', 'мне скучно'), ('ru', 'добрый вечир', 'добрый вечер'),
    ('en', 'thnaks', 'thanks'), ('en', 'good mornign', 'good morning'), ('de', 'danek', 'danke'),
    ('fr', 'bonjuor', 'bonjour'), ('it', 'grazei', 'grazie'), ('pt', 'obrigdo', 'obrigado'),
])
def test_a_conversation_phrase_with_a_typo_is_recognised(code, typed, meant):
    assert learned177.norm(learned177.social_repair(typed, code)) == meant


@pytest.mark.parametrize('code,typed', [('ru', 'кто ты'), ('ru', 'что ты'), ('ru', 'привет'), ('ru', 'открй калькулятор'),
                                        ('en', 'opne calculator'), ('ru', 'как дела')])
def test_real_words_and_commands_are_not_turned_into_conversation(code, typed):
    assert learned177.social_repair(typed, code) == ''


def _session(tmp_path, code='ru'):
    from jarvis.testing.atlas_session import AtlasSession
    session = AtlasSession(tmp_path / 'run', locale=code)
    session.agent._llm_agent170 = lambda *a, **k: None
    return session


def test_neo_answers_a_typo_and_then_answers_it_at_once(tmp_path):
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path)
        agent = session.agent
        session.reset_case('ru')
        first = agent.handle('привте')
        assert 'typo.rescue' in (first.tool_details or []) and first.text
        assert learned177.lookup(agent.config, 'привте', 'ru') == 'привет'
        calls = []
        original = agent._spelling_repair177
        agent._spelling_repair177 = lambda *a: calls.append(a) or original(*a)
        session.reset_case('ru')
        second = agent.handle('привте')
        assert second.text and 'typo.rescue' not in (second.tool_details or []) and not calls
        session.reset_case('ru')
        agent.handle('не то')
        assert learned177.lookup(agent.config, 'привте', 'ru') == ''
        session.finish()


def test_a_request_the_model_understood_runs_without_the_model_next_time(tmp_path, monkeypatch):
    from jarvis.runtime import agent_llm170
    from jarvis.testing.isolation import external_io_blocked
    with external_io_blocked():
        session = _session(tmp_path)
        agent = session.agent
        del agent._llm_agent170
        asked = []

        def decide(_agent, text, code, on_reply=None):
            asked.append(text)
            return agent_llm170.Decision('do', commands=['открой калькулятор'])
        monkeypatch.setattr(agent_llm170, 'decide', decide)
        monkeypatch.setattr(type(agent), '_plain_chat174', lambda *a: False)
        run_steps = agent._run_steps170

        def run(*args):
            # The test session blocks real actions; on a real computer the step completes.
            turn = run_steps(*args)
            turn.tool_details = ['task.done']
            return turn
        agent._run_steps170 = run
        # Exercise learning on an explicitly authorized action. A desire to
        # calculate alone no longer grants authority to launch an application.
        phrase = 'пожалуйста открой калькулятор'
        original_handle = agent._handle_stream_owned
        first = [True]
        def planner_once(text, **kwargs):
            if first[0] and text == phrase:
                first[0] = False
                return agent._llm_agent170(text, 'ru', kwargs.get('on_text'))
            return original_handle(text, **kwargs)
        agent._handle_stream_owned = planner_once
        session.reset_case('ru')
        agent.handle(phrase)
        assert asked and session.calls and session.calls[0]['tool'] == 'application'
        assert learned177.lookup(agent.config, phrase, 'ru') == 'открой калькулятор'
        asked.clear()
        session.reset_case('ru')
        agent.handle(phrase)
        assert not asked and session.calls and session.calls[0]['tool'] == 'application'
        session.finish()


def test_the_users_own_text_is_never_learned(tmp_path):
    from jarvis.agent import Agent
    from jarvis.agent_types import AgentTurn
    agent = Agent.__new__(Agent)
    agent.config = _Config(tmp_path, language='ru')
    agent._turn_input177 = 'text'
    agent._turn_recorded172 = True
    agent._last_executed172 = ('напши маме привет', ('telegram', {'action': 'send', 'message': 'привет'}))
    agent._learn177('напши маме привет', 'напиши маме привет', 'typo', AgentTurn('Отправлено'))
    assert learned177.count(agent.config) == 0


# ---- NEO 177: robot animation (springs, accessories, smoke) -------------------------------------

def _robot(size=(520, 300)):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from jarvis.ui.robot_widget import RobotWidget
    QApplication.instance() or QApplication([])
    robot = RobotWidget()
    robot.resize(*size)
    return robot


def _frames(robot, seconds, *, paint=True):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage, QPainter
    image = QImage(robot.width(), robot.height(), QImage.Format_ARGB32_Premultiplied)
    for _ in range(int(seconds * 60)):
        robot._last_tick -= 1 / 60
        robot._advance_animation()
        if paint:
            image.fill(Qt.transparent)
            painter = QPainter(image)
            robot.render(painter, QPoint())
            painter.end()
    return image


def test_pose_springs_overshoot_a_little_and_settle():
    robot = _robot()
    robot._pose['head_dy'] = 20.0
    robot._pose_vel.clear()
    seen = []
    for _ in range(120):
        robot._spring177(robot._pose, robot._pose_vel, {'head_dy': 0.0}, 1 / 60, robot._SPRINGS177, (7.0, 0.75))
        seen.append(robot._pose['head_dy'])
    assert min(seen) < -0.3                  # a natural overshoot, not a linear slide
    assert abs(seen[-1]) < 0.2               # and it settles


@pytest.mark.parametrize('size', [(520, 300), (240, 130), (160, 72)])
def test_coffee_and_cigarette_fit_inside_the_stage(size):
    robot = _robot(size)
    robot.set_accessories(coffee=True, cigarette=True)
    image = _frames(robot, 1.5)
    background = robot._background.rgb()
    assert all(image.pixel(image.width() - 1, y) == background for y in range(image.height()))
    assert all(image.pixel(0, y) == background for y in range(image.height()))


def test_the_robot_sips_smokes_and_the_smoke_stays_bounded():
    robot = _robot()
    robot.set_accessories(coffee=True, cigarette=True)
    robot._acc177['cigarette']['next'] = 0.0
    robot._acc177['coffee']['next'] = 10 ** 6
    lifts = []
    for _ in range(4):
        _frames(robot, 0.5)
        lifts.append(robot._accessory_lift177('cigarette'))
    assert max(lifts) > 0.9                         # the cigarette went up to the mouth
    assert any(p[10] == 1.0 for p in robot._particles177)
    _frames(robot, 6.0)
    assert len(robot._particles177) <= 150
    robot.set_accessories(coffee=True, cigarette=False)
    assert not any(p[10] == 1.0 for p in robot._particles177)
    assert robot._accessory_lift177('cigarette') == 0.0


def test_a_sip_is_put_down_when_neo_starts_talking():
    robot = _robot()
    robot.set_accessories(coffee=True)
    robot._acc177['coffee']['next'] = 0.0
    _frames(robot, 1.0, paint=False)
    assert robot._accessory_lift177('coffee') > 0.9
    robot.set_state('speaking')
    _frames(robot, 1.2, paint=False)
    assert robot._accessory_lift177('coffee') == 0.0


def test_cigarette_setting_is_offered_next_to_the_coffee():
    root = Path(__file__).resolve().parents[1]
    dialog = (root / 'jarvis' / 'ui' / 'settings_dialog.py').read_text(encoding='utf-8')
    window = (root / 'jarvis' / 'ui' / 'main_window.py').read_text(encoding='utf-8')
    assert '"robot_accessory_cigarette": self.robot_accessory_cigarette.isChecked()' in dialog
    assert 'accessories_row177_layout.addWidget(self.robot_accessory_cigarette)' in dialog
    assert 'cigarette=bool(self.config.get("ui.robot_accessory_cigarette", False))' in window


def test_settings_177_adds_only_missing_keys():
    from jarvis.runtime import settings177
    data = {'learning': {'remember_resolved': False}, 'settings_version': 175}
    settings177.migrate(data, 175)
    assert data['learning']['remember_resolved'] is False
    assert data['integrations']['yandex_music']['network_recovery_seconds'] == 60
    assert data['ui']['robot_accessory_cigarette'] is False and data['settings_version'] == 177


# ---- NEO 178: the wake word is NEO -----------------------------------------------------------

@pytest.mark.parametrize('heard,tail', [('Нео', ''), ('Нео?', ''), ('Нео!', ''), ('нео, включи музыку', 'включи музыку'),
                                        ('Эй, Нео, который час', 'который час'), ('Нио.', ''), ('Нэо', ''),
                                        ('Neo, open calculator', 'open calculator'), ('а, Нео', ''), ('Néo, ouvre', 'ouvre')])
def test_neo_wakes_on_its_name(heard, tail):
    from jarvis.runtime import wake178
    from jarvis.wake_matcher import match_wake_phrase
    matched, rest, _score = match_wake_phrase(heard, 'NEO', aliases=tuple(wake178.aliases()))
    assert matched and rest == tail


@pytest.mark.parametrize('heard', ['не-а', 'Неа.', 'не а', 'не', 'нет', 'Не о том речь', 'неоновая лампа', 'нео-нацизм',
                                   'но', 'ну давай', 'Не, ну а что', 'Nope', 'No.', 'Nah', 'Нее', 'неожиданно', 'Джарвис', 'Jarvis, open'])
def test_neo_does_not_wake_on_similar_words(heard):
    from jarvis.runtime import wake178
    from jarvis.wake_matcher import match_wake_phrase, looks_wake_like
    aliases = tuple(wake178.aliases()) + ('неа', 'Неа!')        # even a learned «неа» never wakes NEO
    assert not match_wake_phrase(heard, 'NEO', aliases=aliases)[0]
    assert not looks_wake_like(heard, 'NEO', aliases=aliases)


def test_every_wake_alias_has_period_exclamation_and_question_variants():
    from jarvis.runtime import wake178
    aliases = wake178.aliases()
    for base in wake178.BASES:
        assert {base, base + '.', base + '!', base + '?'} <= set(aliases)
    assert wake178.with_punctuation(['Нео!']) == ['Нео', 'Нео.', 'Нео!', 'Нео?']


def test_an_old_jarvis_profile_becomes_neo():
    from jarvis.runtime import wake178
    data = {'assistant_name': 'Jarvis', 'settings_version': 177,
            'wake_word': {'phrase': 'Jarvis', 'aliases': ['Jarvis', 'Джарвис!'], 'fast_probe_language': 'en',
                          'language_aliases': {'ru': ['Service']}},
            'speech': {'wake_aliases': ['Jarvis']}}
    wake178.migrate(data, 177)
    assert data['wake_word']['phrase'] == 'NEO' and data['assistant_name'] == 'NEO'
    assert data['wake_word']['aliases'] == wake178.aliases() == data['speech']['wake_aliases']
    assert data['wake_word']['language_aliases']['ru'] == [] and data['wake_word']['fast_probe_language'] == 'auto'
    custom = {'assistant_name': 'Пятница', 'wake_word': {'phrase': 'NEO'}}
    wake178.migrate(custom, 177)
    assert custom['assistant_name'] == 'Пятница'

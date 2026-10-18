"""Bounded OBS grammar and original-input authority contracts; no live I/O."""
import json
from pathlib import Path
from string import Formatter

import pytest

from jarvis.runtime.streaming_commands184 import (
    ACTIONS, LANGUAGES, confirmation_reply, obs_input_scope, obs_tool_allowed,
    original_obs_command, parse_obs_command,
)

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = json.loads((ROOT / 'config/canonical_phrases/obs184.json').read_text(encoding='utf-8'))
VARIANTS = json.loads((ROOT / 'config/command_variants/obs184.json').read_text(encoding='utf-8'))
VALUES = {'scene': '  Case Sensitive Scene!?  ', 'source': '<b> Source </b>', 'input': ' Mic ', 'volume': 37.5}


def example(template):
    fields = [name for _, name, _, _ in Formatter().parse(template) if name]
    words = {key: str(value) if key == 'volume' else '"' + value + '"' for key, value in VALUES.items()}
    return template.format(**words), {key: VALUES[key] for key in fields}


@pytest.mark.parametrize('code', LANGUAGES)
def test_all_reviewed_canonical_and_variant_templates_keep_exact_arguments(code):
    assert set(CANONICAL) == {'obs_control.' + action for action in ACTIONS}
    assert set(VARIANTS) == set(CANONICAL)
    for intent, row in CANONICAL.items():
        assert len(row) == len(LANGUAGES) and set(VARIANTS[intent]) == set(LANGUAGES)
        for template in [row[LANGUAGES.index(code)], *VARIANTS[intent][code]]:
            phrase, arguments = example(template)
            assert parse_obs_command(phrase, code) == ('obs_control', {'action': intent.split('.')[1], **arguments}), phrase


@pytest.mark.parametrize('code', LANGUAGES)
def test_negation_quotation_explanation_and_payload_cannot_authorize_obs(code):
    phrase = CANONICAL['obs_control.stream_start'][LANGUAGES.index(code)]
    prefixes = {
        'ru': ('не ', 'что означает команда ', 'напиши текст '),
        'en': ('do not ', 'what does this command mean: ', 'type this text: '),
        'de': ('nicht ', 'was bedeutet der Befehl ', 'schreibe den Text '),
        'fr': ('ne ', 'que signifie la commande ', 'écris le texte '),
        'es': ('no ', 'qué significa la orden ', 'escribe el texto '),
        'it': ('non ', 'cosa significa il comando ', 'scrivi il testo '),
        'pt': ('não ', 'o que significa o comando ', 'escreve o texto '),
    }
    for text in [*(prefix + phrase for prefix in prefixes[code]), '"' + phrase + '"', phrase + '\n' + phrase]:
        assert parse_obs_command(text, code) is None, text
        with obs_input_scope(text, code):
            assert not obs_tool_allowed('obs_control', {'action': 'stream_start'})[0]


@pytest.mark.parametrize('text', [
    'switch OBS scene to not Studio', 'switch OBS scene to Studio and start streaming',
    'switch OBS scene to "Studio', 'switch OBS scene to {scene}',
    'set input Mic volume in OBS to 101 percent', 'set input Mic volume in OBS to -1 percent',
    'set input Mic volume in OBS to nan percent', 'start streaming', 'start streaming in OBSERVER',
    'save a clip in GeForce Experience', 'change Twitch title in OBS', 'read YouTube chat in OBS',
])
def test_incomplete_unrelated_and_unsupported_commands_do_not_bind(text):
    assert parse_obs_command(text, 'en') is None


def test_quoted_names_are_literal_even_when_they_contain_command_words():
    for name in ['not Studio', 'Studio and start streaming', ' edge ', '<img src="x">']:
        quote = '«' + name + '»'
        assert parse_obs_command('switch OBS scene to ' + quote, 'en') == ('obs_control', {'action': 'scene_switch', 'scene': name})


def test_original_input_blocks_rewritten_arguments_fuzzy_commands_and_nvidia_capture():
    with obs_input_scope('switch OBS scene to " edge "', 'en'):
        assert obs_tool_allowed('obs_control', {'action': 'scene_switch', 'scene': ' edge '})[0]
        for args in ({'action': 'scene_switch', 'scene': 'edge'}, {'action': 'stream_start'},
                     {'action': 'scene_switch', 'scene': ' edge ', 'confirmed': True}):
            assert not obs_tool_allowed('obs_control', args)[0]
        assert not obs_tool_allowed('capture', {'action': 'save'})[0]
        assert obs_tool_allowed('browser', {'action': 'search'})[0]
    with obs_input_scope('start the show', 'en'):
        assert not obs_tool_allowed('obs_control', {'action': 'stream_start'}, 'start streaming in OBS')[0]
    assert original_obs_command() is None
    assert not obs_tool_allowed('obs_control', {'action': 'stream_start'})[0]


def test_language_isolation_and_assistant_address():
    for code in LANGUAGES:
        if code != 'en':
            assert parse_obs_command('start streaming in OBS', code) is None
    assert parse_obs_command('NEO, start streaming in OBS', 'en', ['NEO']) == ('obs_control', {'action': 'stream_start'})


@pytest.mark.parametrize('code,suffix', [('ru','через 10 минут'), ('en','in10minutes'),
    ('de','nach zehn Minuten'), ('fr','dans dix minutes'), ('es','dentro de diez minutos'),
    ('it','tra dieci minuti'), ('pt','daqui a dez minutos'), ('en','in 2 days'), ('en','after 1 week'),
    ('it','tra un minuto'), ('it','fra un giorno'), ('it','fra una settimana')])
def test_unquoted_delayed_names_never_become_immediate_obs_actions(code,suffix):
    template = CANONICAL['obs_control.scene_switch'][LANGUAGES.index(code)]
    name = 'Studio ' + suffix
    assert parse_obs_command(template.format(scene=name), code) is None
    assert parse_obs_command(template.format(scene='"' + name + '"'), code) == (
        'obs_control', {'action': 'scene_switch', 'scene': name})


@pytest.mark.parametrize('code,yes,no', [('ru','да','нет'), ('en','yes','no'), ('de','ja','nein'),
    ('fr','oui','non'), ('es','sí','no'), ('it','sì','no'), ('pt','sim','não')])
def test_confirmation_reply_is_exact_and_locale_owned(code, yes, no):
    assert confirmation_reply(yes, code) is True
    assert confirmation_reply(no, code) is False
    assert confirmation_reply(yes + ' start streaming in OBS', code) is None
    assert confirmation_reply('"' + yes + '"', code) is None


def test_canonical_and_feature_parsers_share_reviewed_literal_grammar():
    from jarvis.runtime.canonical_commands import parse_canonical_command
    from jarvis.runtime.feature_commands import parse_feature_command
    from jarvis.tools.obs_tool184 import register
    from types import SimpleNamespace
    definitions = []
    register(SimpleNamespace(register=definitions.append), None)
    schema = definitions[0].parameters
    for code in LANGUAGES:
        text, args = example(CANONICAL['obs_control.source_hide'][LANGUAGES.index(code)])
        expected = {'action': 'source_hide', **args}
        assert parse_feature_command(text, code) == ('obs_control', expected)
        matched = parse_canonical_command(text, code, lambda _: schema)
        assert matched.tool == 'obs_control' and matched.arguments == expected and not matched.error

"""Stage 3 of the request cascade: the NEO command model in the running assistant.

Called only after the rules and the existing matchers found nothing. The model
proposes a registered tool, its arguments and whether to ask first; it never runs
anything itself. The agent's normal executor then validates the tool and the
arguments against the tool schema, checks permissions and asks for confirmation.

Decision rules
* "not a command" or low confidence -> None (the request goes to local conversation);
* questions («почему…», «что такое…») need very high confidence to become a command;
* a confident command with all its values -> executed through the normal path;
* a less confident one -> «Вы имели в виду …?» (yes runs it);
* two close candidates, or a short word whose meaning depends on the last action
  («назад» after the browser or after music) -> resolved by that context, else a choice;
* a missing value -> NEO asks for it, like any other command.
"""
from __future__ import annotations

from functools import lru_cache
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_LOADED: dict[str, Any] = {'folder': None, 'model': None}
_DOMAINS = {
    'browser': {'browser', 'browser_nav', 'browser_advanced', 'mini_browser'},
    'music': {'music', 'yandex_music', 'spotify', 'media', 'discord'},
}
_SLOT_KIND = {'seconds': ('duration', 'duration'), 'minutes': ('duration', 'duration'), 'value': ('percent156', 'percent156'),
              'volume': ('percent156', 'percent156'), 'percent': ('percent156', 'percent156'), 'index': ('bounded_index', 'item_index'),
              'number': ('bounded_index', 'item_index'), 'text': ('text', 'text'), 'content': ('text', 'text'), 'body': ('text', 'email_body'),
              'query': ('text', 'query'), 'url': ('url', 'url'), 'folder': ('name', 'folder'), 'path': ('name', 'file'),
              'source': ('name', 'source'), 'destination': ('name', 'destination'), 'title': ('name', 'title'),
              'new_name': ('name', 'new_name'), 'playlist': ('name', 'playlist'), 'name': ('name', 'application')}
_ASK = {
    'ru': ('Вы имели в виду «{label}»? Ответьте «да» или скажите иначе.', 'Уточните, что сделать:'),
    'en': ('Did you mean “{label}”? Answer “yes” or say it differently.', 'Please choose what to do:'),
    'de': ('Meinten Sie „{label}“? Antworten Sie „ja“ oder sagen Sie es anders.', 'Bitte wählen Sie:'),
    'fr': ('Vouliez-vous dire « {label} » ? Répondez « oui » ou reformulez.', 'Choisissez ce qu’il faut faire :'),
    'es': ('¿Querías decir «{label}»? Responde «sí» o dilo de otra forma.', 'Elige qué hacer:'),
    'it': ('Intendevi «{label}»? Rispondi «sì» o dillo diversamente.', 'Scegli cosa fare:'),
    'pt': ('Queria dizer «{label}»? Responda «sim» ou diga de outra forma.', 'Escolha o que fazer:'),
}


def settings(config: Any) -> dict[str, float]:
    return {'execute': float(config.get('neo_model.execute_threshold', 0.9) or 0.9),
            'suggest': float(config.get('neo_model.suggest_threshold', 0.6) or 0.6),
            'question': float(config.get('neo_model.question_threshold', 0.97) or 0.97)}


def model(config: Any) -> Any | None:
    """The active model, loaded once on first use and reloaded only when the active version changes."""
    from jarvis.neo_model import store
    folder = store.active_folder(config)
    if folder is None:
        return None
    with _LOCK:
        if _LOADED['folder'] != str(folder):
            from jarvis.neo_model.predictor import NeoCommandModel
            started = time.perf_counter()
            _LOADED['model'] = NeoCommandModel(folder)
            _LOADED['folder'] = str(folder)
            logger.info('NEO command model %s loaded in %.0f ms', folder.name, (time.perf_counter() - started) * 1000)
        return _LOADED['model']


def unload() -> None:
    with _LOCK:
        _LOADED.update(folder=None, model=None)


def _context(agent: Any) -> str:
    try:
        state = agent.conversations.get(getattr(agent, '_conversation_key', 'desktop'))
        tool = str(getattr(state, 'last_tool', '') or '')
    except Exception:
        return ''
    for domain, tools in _DOMAINS.items():
        if tool in tools:
            return domain
    return ''


def _template_for(intent: str, code: str) -> Any | None:
    from jarvis.runtime.canonical_commands import templates
    rows = [row for row in templates(code) if row.intent == intent]
    if not rows:
        return None
    return min(rows, key=lambda row: len(re.findall(r'\{', row.phrase)) * 100 + len(row.phrase))


def label(intent: str, code: str) -> str:
    row = _template_for(intent, code)
    return re.sub(r'\{[a-z_0-9]+\}', '…', row.phrase) if row is not None else intent


def arguments(agent: Any, predictor: Any, text: str, code: str, intent: str) -> tuple[str, dict[str, Any], str]:
    """(tool, validated arguments, missing slot name or ''). Raises ValueError when invalid."""
    from jarvis.runtime.canonical_commands import _SLOT, _SLOT_NAMES, _Template, _bind
    tool = intent.split('.', 1)[0]
    registry = getattr(agent, 'tools', None)
    if registry is None or tool not in getattr(registry, '_tools', {}):
        raise ValueError(f'unknown tool {tool}')
    schema = registry.get(tool).parameters
    template = _template_for(intent, code)
    wanted = _SLOT.findall(template.phrase) if template is not None else []
    found = predictor.slots(text, intent) if wanted else {}
    captures = {name: found[name] for name in wanted if found.get(name)}
    missing = [name for name in wanted if name not in captures]
    row = _Template(intent, template.phrase if template else intent, re.compile(''), 0)
    if missing:
        action = intent.split('.', 1)[1]
        partial = {'action': action} if 'action' in schema.get('properties', {}) else {}
        for name, value in captures.items():
            partial[_SLOT_NAMES.get(intent, {}).get(name, name)] = value
        return tool, partial, missing[0]
    tool, args = _bind(row, captures, schema, text, code)
    return tool, args, ''


def _is_safe(agent: Any, tool: str, args: dict[str, Any]) -> bool:
    try:
        from jarvis.security import Risk
        return agent.tools.risk_for(tool, args) <= Risk.SAFE
    except Exception:
        return False


def _pending(agent: Any, code: str, tool: str, args: dict[str, Any], slot: Any, *, guard: dict | None = None) -> None:
    from jarvis.runtime.dialogue_tasks import PendingIntent
    if tool == 'scenario':
        from jarvis.runtime.scenario_authority184 import bind_scenario_arguments
        bind_scenario_arguments(agent.config, args)
    state = agent.conversations.get(getattr(agent, '_conversation_key', 'desktop'))
    now = time.monotonic()
    state.pending_intent = PendingIntent(tool, dict(args), (slot,), code, now + 90, now, purpose='neo_model169',
                                         guard157=guard or {})


# Requests the model must leave alone: negations, first-person narration, quoted text.
_NEGATION = re.compile(r"^(?:не|нет|ни|don'?t|do\s+not|never|not|no|nicht|kein\w*|ne|n'|no\s+|non|não|nao)\b", re.I)
_NARRATIVE = re.compile(r"^(?:я|мы|он|она|они|ты|вчера|сегодня\s+я|i|we|he|she|they|yesterday|ich|wir|er|sie|gestern|je|j'|nous|il|elle|hier|"
                        r"yo|nosotros|él|ella|ayer|io|noi|lui|lei|ieri|eu|nós|ele|ela|ontem)\b", re.I)


@lru_cache(maxsize=7)
def _vocabulary(code: str) -> frozenset[str]:
    """Words NEO's commands are made of in this language (grammar packs and the phrase atlas)."""
    words: set[str] = set()
    from jarvis.runtime.canonical_commands import templates
    for row in templates(code):
        words.update(re.findall(r'\w{3,}', re.sub(r'\{[a-z_0-9]+\}', ' ', row.phrase.casefold().replace('ё', 'е'))))
    atlas = Path(__file__).resolve().parents[1] / 'locales' / 'commands' / f'{code}.json'
    try:
        import json
        pack = json.loads(atlas.read_text(encoding='utf-8'))
        for phrases in pack.get('action_phrases', {}).values():
            for phrase in phrases:
                words.update(re.findall(r'\w{3,}', re.sub(r'\{[a-z_0-9]+\}', ' ', phrase.casefold().replace('ё', 'е'))))
    except (OSError, ValueError):
        pass
    return frozenset(words)


def _mostly_known(text: str, code: str) -> bool:
    """At least half of the words (3+ letters, no paths/numbers) are words NEO's commands use."""
    words = [w for w in re.findall(r'\w{3,}', text.casefold().replace('ё', 'е')) if not w.isdigit()]
    if not words:
        return False
    vocabulary = _vocabulary(code)
    return sum(w in vocabulary for w in words) * 2 > len(words)


# Polite openings are requests, not negations: «не мог бы ты…», «could you…».
_POLITE = re.compile(
    r"^(?:(?:будь|будьте)\s+добр\w*|пожалуйста|не\s+мог(?:ла|ли)?\s+бы\s+(?:ты|вы)|не\s+могли\s+бы\s+вы|не\s+затруднит\w*|"
    r"можешь|можете|сможешь|please|could\s+you|would\s+you(?:\s+mind)?|can\s+you|would\s+you\s+please|bitte|könntest\s+du|kannst\s+du|"
    r"s['’]il\s+te\s+pla[iî]t|peux-tu|pourrais-tu|por\s+favor|puedes|podr[ií]as|per\s+favore|puoi|potresti|podes|poderias)[\s,]+", re.I)


def _without_politeness(value: str) -> str:
    previous = None
    while previous != value:
        previous, value = value, _POLITE.sub('', value, count=1).strip(' ,')
    return value


def leave_alone(text: str, code: str) -> bool:
    from jarvis.runtime.natural_commands import negated_action
    value = _without_politeness(' '.join(str(text or '').split()))
    if not value or value[0] in '"\'«“„':
        return True
    if negated_action(value, code) or _NEGATION.match(value) or _NARRATIVE.match(value):
        return True
    return not _mostly_known(value, code)


def propose(agent: Any, text: str, code: str) -> Any | None:
    from jarvis.neo_model.dataset import NONE
    from jarvis.runtime.dialogue_tasks import Slot
    from jarvis.runtime.neo_model169 import Proposal
    from jarvis.runtime.semantics import looks_like_general_question
    config = agent.config
    if not bool(config.get('neo_model.enabled', True)) or leave_alone(text, code):
        return None
    predictor = model(config)
    if predictor is None:
        return None
    limits = settings(config)
    started = time.perf_counter()
    context = _context(agent)
    ambiguous = _context_choices(text, code) if not context else []
    if len(ambiguous) > 1:
        # «назад» with no browser or music just before: ask instead of guessing.
        options = []
        for candidate in ambiguous:
            try:
                tool_c, args_c, missing_c = arguments(agent, predictor, text, code, candidate)
            except (ValueError, KeyError, TypeError):
                continue
            if not missing_c:
                options.append((candidate, tool_c, args_c))
        if len(options) > 1:
            choices = tuple((label(candidate, code), str(i)) for i, (candidate, _, _) in enumerate(options, 1))
            guard = {'neo169': {str(i): [tool_c, args_c] for i, (_, tool_c, args_c) in enumerate(options, 1)}}
            _pending(agent, code, options[0][1], {}, Slot('_neo169', 'choice', 'choose', choices=choices), guard=guard)
            question = _ASK.get(code, _ASK['en'])[1] + '\n' + '\n'.join(f'{i}. {t}' for i, (t, _) in enumerate(choices, 1))
            return Proposal(options[0][0], options[0][1], options[0][2], 0.5, True, question)
    ranked = predictor.ranked(text, code, context, top=3)
    intent, confidence = ranked[0]
    _log(agent, text, ranked, context, started)
    if intent == NONE or confidence < limits['suggest']:
        return None
    if looks_like_general_question(text) and confidence < limits['question']:
        return None
    from jarvis.runtime.tasks169 import split
    if len(split(text, code)) > 1:
        return None       # a sequence of commands is the task planner's job, never a single guess
    try:
        tool, args, missing = arguments(agent, predictor, text, code, intent)
    except (ValueError, KeyError, TypeError) as exc:
        logger.info('NEO model proposal %s rejected: %s', intent, exc)
        return None
    if missing:
        if confidence < limits['execute']:
            return None   # never capture the next utterance as data for a guess
        kind, prompt = _SLOT_KIND.get(missing, ('', ''))
        if not kind:
            # NEO 188 atlas: a slot without a generic kind uses its command's own clarification
            # contract («какой кнопкой?»), never the free-text note question.
            from jarvis.runtime.clarification_contracts157 import CONTRACTS as _CONTRACTS157
            declared = next((row for row in _CONTRACTS157.get(intent, ()) if row[0] == missing), None)
            kind, prompt = (declared[1], declared[2]) if declared else ('text', 'text')
        from jarvis.runtime.canonical_commands import _SLOT_NAMES
        name = _SLOT_NAMES.get(intent, {}).get(missing, missing)
        slot = Slot(name, kind, prompt)
        _pending(agent, code, tool, args, slot)
        state = agent.conversations.get(getattr(agent, '_conversation_key', 'desktop'))
        return Proposal(intent, tool, args, confidence, True, state.pending_intent.question())
    runner_up, runner_conf = ranked[1] if len(ranked) > 1 else (NONE, 0.0)
    safe = _is_safe(agent, tool, args)
    if confidence >= limits['execute'] and runner_conf < 0.3 * confidence and safe:
        return Proposal(intent, tool, args, confidence)
    if not safe:
        # Anything that is not a plain safe action is only suggested, never run straight from a guess.
        runner_up, runner_conf = NONE, 0.0
    # Not sure enough: ask. Two close candidates -> a choice; otherwise confirm the best one.
    ask, choose = _ASK.get(code, _ASK['en'])
    if runner_up != NONE and runner_conf >= 0.2:
        try:
            tool2, args2, missing2 = arguments(agent, predictor, text, code, runner_up)
        except (ValueError, KeyError, TypeError):
            missing2 = 'invalid'
        if not missing2:
            choices = ((label(intent, code), '1'), (label(runner_up, code), '2'))
            guard = {'neo169': {'1': [tool, args], '2': [tool2, args2]}}
            _pending(agent, code, tool, {}, Slot('_neo169', 'choice', 'choose', choices=choices), guard=guard)
            question = choose + '\n' + '\n'.join(f'{i}. {text_}' for i, (text_, _) in enumerate(choices, 1))
            return Proposal(intent, tool, args, confidence, True, question)
    _pending(agent, code, tool, args, Slot('_confirm', 'confirm', 'confirm_correction', hint=label(intent, code)))
    return Proposal(intent, tool, args, confidence, True, ask.format(label=label(intent, code)))


def _context_choices(text: str, code: str) -> list[str]:
    """Intents a short context-dependent phrase («назад», «back») can mean in this language."""
    from jarvis.neo_model.dataset import CONTEXT_PHRASES
    from jarvis.neo_model.features import normalize
    key = normalize(text).strip(' .!?')
    return list(dict.fromkeys(intent for (phrase, _context), intent in CONTEXT_PHRASES.get(code, {}).items()
                              if normalize(phrase) == key))


def _log(agent: Any, text: str, ranked: list[tuple[str, float]], context: str, started: float) -> None:
    elapsed = (time.perf_counter() - started) * 1000
    top = [(intent, round(p, 3)) for intent, p in ranked]
    if bool(agent.config.get('diagnostics.log_content', False)):
        logger.info('NEO model %.1f ms context=%s %r -> %s', elapsed, context, text[:120], top)
    else:
        logger.info('NEO model %.1f ms context=%s -> %s', elapsed, context, top)
    try:
        from jarvis.runtime.developer import developer_bus
        developer_bus().emit('latency', 'neo_model', data={'duration_ms': elapsed, 'top': top, 'context': context}, duration_ms=elapsed)
    except Exception:
        pass

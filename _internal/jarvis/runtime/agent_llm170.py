"""NEO 170: the small agent model (Qwen 0.5–1.5B through Ollama) as one step of the cascade.

Runs after the rules, the phrase grammar, the task planner and the NEO command
model found nothing certain. One request to the local model decides:

* conversation — the model's reply is shown (``{"reply": …}``);
* a missing detail — the model's question is shown (``{"ask": …}``);
* actions — the request rewritten as NEO commands (``{"do": […]}``). Every command
  is parsed by NEO's own grammar; the steps then run through the verified task
  runner (checks after each step, confirmation before anything with consequences,
  cancel). A command NEO cannot parse is never executed.

The prompt shows only the command patterns that matter for this request: the NEO
command model ranks the likely intents, plus a fixed set of everyday computer
commands. That keeps the prompt short, so a 0.5–1.5B model answers quickly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_UNAVAILABLE_UNTIL = [0.0]


@dataclass
class Decision:
    kind: str                      # reply | ask | do
    text: str = ''
    commands: list[str] = field(default_factory=list)
    model: str = ''
    elapsed_ms: float = 0.0
    streamed: str = ''             # NEO 172: reply text already shown/spoken while it was generated


def enabled(config: Any) -> bool:
    return bool(config.get('llm_agent.enabled', True))


def candidate_intents(agent: Any, text: str, code: str, *, limit: int | None = None) -> list[str]:
    from jarvis.neo_llm.prompt import CORE_INTENTS
    config = agent.config
    limit = int(limit or config.get('llm_agent.candidates', 14) or 14)
    ranked: list[str] = []
    try:
        from jarvis.neo_model import runtime
        from jarvis.neo_model.dataset import NONE
        from jarvis.runtime import tasks169
        predictor = runtime.model(config)
        if predictor is not None:
            pieces = [text]
            verbs = agent._step_verbs170(code) if hasattr(agent, '_step_verbs170') else set()
            pieces += [p for p in tasks169.split_soft(text, code, verbs) if p != text][:MAX_PIECES]
            context = runtime._context(agent)
            per_piece = max(3, limit // max(1, len(pieces)))
            for piece in pieces:
                for intent, _p in predictor.ranked(piece, code, context, top=per_piece + 1):
                    if intent != NONE and intent not in ranked:
                        ranked.append(intent)
    except Exception:
        logger.debug('Candidate ranking failed', exc_info=True)
    ordered = ranked[:limit] + [i for i in CORE_INTENTS if i not in ranked[:limit]]
    tools = getattr(getattr(agent, 'tools', None), '_tools', None)
    if tools is not None:
        ordered = [i for i in ordered if i.split('.', 1)[0] in tools]
    return ordered


MAX_PIECES = 6


def decide(agent: Any, text: str, code: str, *, on_reply: Callable[[str], None] | None = None) -> Decision | None:
    """The model's decision, or None when the agent model is unavailable (the caller falls back)."""
    config = agent.config
    ollama = getattr(agent, 'ollama', None)
    if not enabled(config) or ollama is None or not hasattr(ollama, 'agent_json'):
        return None
    if time.monotonic() < _UNAVAILABLE_UNTIL[0]:
        return None
    from jarvis.neo_llm import prompt
    from jarvis.runtime import local_chat169
    intents = candidate_intents(agent, text, code)
    patterns = [p for p in (prompt.pattern_for(i, code) for i in intents) if p]
    facts: list[str] = []
    try:
        from jarvis.runtime import memory169
        facts = memory169.facts_for_prompt(agent.database, config)
    except Exception:
        pass
    scope = getattr(agent, '_conversation_key', 'desktop')
    messages = prompt.messages(text, code, patterns, facts=facts, past=local_chat169.history(scope),
                               persona=str(config.get('assistant_prompt', '') or ''))
    started = time.perf_counter()
    streamed: list[str] = []

    def stream(delta: str) -> None:
        streamed.append(delta)
        on_reply(delta)
    try:
        if on_reply is not None and bool(config.get('local_chat.stream', True)):
            try:
                value = ollama.agent_json(messages, prompt.SCHEMA, on_reply=stream)
            except TypeError:
                value = ollama.agent_json(messages, prompt.SCHEMA)     # a client without streaming
        else:
            value = ollama.agent_json(messages, prompt.SCHEMA)
    except Exception as exc:
        from jarvis.runtime.cancellation import OperationCancelled
        if isinstance(exc, OperationCancelled):
            raise
        from jarvis.ollama_client import IncompleteModelOutput
        if isinstance(exc, IncompleteModelOutput) or 'duplicate key' in str(getattr(exc, '__cause__', '') or ''):
            # NEO 188 NEO-TOOL-001: an incomplete/ambiguous object runs nothing, but the
            # model itself is available — no cooldown for the next request.
            logger.info('Agent model answer incomplete or ambiguous; nothing executed')
            return None
        _UNAVAILABLE_UNTIL[0] = time.monotonic() + 60.0
        logger.info('Agent model unavailable: %s', str(exc)[:200])
        # Missing command models are an actionable settings state, never permission
        # to download optional weights during an ordinary conversation.
        return None
    elapsed = (time.perf_counter() - started) * 1000
    decision = parse(value)
    if decision.kind == 'invalid':
        logger.info('Agent model answer has invalid fields; nothing executed')
        return None
    decision.elapsed_ms = elapsed
    decision.streamed = ''.join(streamed)
    if bool(config.get('diagnostics.log_content', False)):
        logger.info('Agent model %s %.0f ms: %r -> %s %r', decision.model, elapsed, text[:160], decision.kind,
                    decision.commands or decision.text[:160])
    else:
        logger.info('Agent model %s %.0f ms: %s, %d commands, %d candidates', decision.model, elapsed, decision.kind,
                    len(decision.commands), len(patterns))
    try:
        from jarvis.runtime.developer import developer_bus
        developer_bus().emit('latency', 'llm_agent', data={'duration_ms': elapsed, 'kind': decision.kind,
                                                            'commands': len(decision.commands), 'model': decision.model},
                             duration_ms=elapsed)
    except Exception:
        pass
    if decision.kind == 'reply' and not decision.text:
        return None
    return decision


MAX_COMMAND_CHARS = 240


def parse(value: dict[str, Any]) -> Decision:
    """The model's whole object → one decision.

    NEO 188 NEO-TOOL-001: a field that is present must have its contract type —
    ``do`` a list of non-empty strings of at most MAX_STEPS items and
    MAX_COMMAND_CHARS characters each, ``reply``/``ask`` strings. An explicit null,
    a wrong type, a too long step or more steps than allowed make the object
    ``invalid``: nothing is truncated, converted or silently dropped and nothing runs.
    """
    from jarvis.neo_llm.prompt import MAX_STEPS
    model = str(value.get('_model', '') or '')
    for key in ('reply', 'ask'):
        if key in value and not isinstance(value[key], str):
            return Decision('invalid', model=model)
    if 'do' in value:
        commands = value['do']
        if not isinstance(commands, list) or len(commands) > MAX_STEPS:
            return Decision('invalid', model=model)
        cleaned: list[str] = []
        for command in commands:
            if not isinstance(command, str):
                return Decision('invalid', model=model)
            text = ' '.join(command.split())
            if not text:
                continue
            if len(text) > MAX_COMMAND_CHARS:
                return Decision('invalid', model=model)
            cleaned.append(text)
        if cleaned:
            return Decision('do', commands=cleaned, model=model)
    if isinstance(value.get('ask'), str) and value['ask'].strip():
        return Decision('ask', text=value['ask'].strip()[:600], model=model)
    reply = value.get('reply')
    return Decision('reply', text=str(reply).strip()[:4000] if isinstance(reply, str) else '', model=model)


_TEXT = {
    'unparsed': ('Я понял просьбу так: {commands}. Но шаг «{step}» не является командой NEO, поэтому ничего не выполнял. Скажите этот шаг иначе.',
                 'I understood it as: {commands}. But “{step}” is not a NEO command, so I did nothing. Please say that step differently.',
                 'Ich habe es so verstanden: {commands}. „{step}“ ist aber kein NEO-Befehl, daher habe ich nichts ausgeführt. Sag diesen Schritt bitte anders.',
                 'J’ai compris : {commands}. Mais « {step} » n’est pas une commande NEO, je n’ai donc rien fait. Reformulez cette étape.',
                 'Lo entendí así: {commands}. Pero «{step}» no es un comando de NEO, así que no hice nada. Di ese paso de otra forma.',
                 'Ho capito così: {commands}. Ma «{step}» non è un comando NEO, quindi non ho fatto nulla. Riformula quel passo.',
                 'Percebi assim: {commands}. Mas «{step}» não é um comando do NEO, por isso não fiz nada. Diz esse passo de outra forma.'),
}
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def reset_availability() -> None:
    _UNAVAILABLE_UNTIL[0] = 0.0

"""NEO 188 NEO-SCENARIO-001 / TZ-SCENARIO-TYPED-RUN-INPUTS-01.

A saved scenario MAY declare named typed run inputs and bind them to
compatible fields of its existing steps::

    "inputs": [{"name": "screen", "type": "monitor", "default": 2, "label": "монитор"},
               {"name": "pause", "type": "duration", "default": 1.5, "label": "пауза"},
               {"name": "doc", "type": "target", "label": "файл",
                "choices": ["C:\\\\Work\\\\plan.docx", "C:\\\\Work\\\\notes.txt"]}],
    "steps": [{"type": "open", "path": "C:\\\\Work\\\\plan.docx", "bind": {"monitor": "screen", "launch_wait": "pause", "path": "doc"}},
              {"type": "wait", "seconds": 1, "bind": {"seconds": "pause"}}]

* types: ``monitor`` (0–32; the number is structural, it does not prove the
  monitor exists), ``duration`` (seconds; every bound field keeps its own bound:
  ``wait.seconds`` 0–30, ``open.launch_wait`` 0–2) and ``target`` (one of the
  existing objects the template itself lists in ``choices`` — no arbitrary
  path, traversal or guessed replacement);
* only ``open.monitor``, ``open.launch_wait``, ``open.path`` and ``wait.seconds``
  can be bound; command steps, executables, argv, failure policy and enabled
  flags never take a parameter; nothing is interpolated or evaluated;
* values belong to ONE run: they are resolved into a deep copy, the template
  (and its saved defaults) is never written; the approval fingerprint covers
  the template revision AND the resolved values;
* legacy scenarios without ``inputs``/``bind`` behave exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

TYPES = ('monitor', 'duration', 'target')
MAX_INPUTS = 16
MAX_CHOICES = 32
MAX_MONITOR = 32
# field → (step kind, input type, upper bound)
FIELDS = {('open', 'monitor'): ('monitor', MAX_MONITOR), ('open', 'launch_wait'): ('duration', 2.0),
          ('open', 'path'): ('target', None), ('wait', 'seconds'): ('duration', 30.0)}
_NAME = re.compile(r'^[\w\-]{1,40}$', re.U)
_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _kind(step: dict) -> str:
    return str(step.get('type', 'open') or 'open').strip().casefold()


def declared(raw: dict) -> list[dict]:
    inputs = raw.get('inputs') if isinstance(raw, dict) else None
    return [row for row in inputs if isinstance(row, dict)] if isinstance(inputs, list) else []


def has_inputs(raw: dict) -> bool:
    return bool(declared(raw))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _valid_text(value: Any, limit: int = 8192) -> bool:
    return isinstance(value, str) and value.strip() != '' and len(value) <= limit and not any(ord(c) < 32 for c in value)


def check_value(spec: dict, value: Any, bounds: list[float | None]) -> str:
    """'' when the value is acceptable for the input and every field it is bound to."""
    kind = spec.get('type')
    if kind == 'monitor':
        number = _number(value)
        if number is None or int(number) != number or not 0 <= number <= MAX_MONITOR:
            return 'monitor'
        return ''
    if kind == 'duration':
        number = _number(value)
        upper = min([b for b in bounds if b is not None] or [30.0])
        if number is None or not 0 <= number <= upper:
            return f'duration:{upper:g}'
        return ''
    if kind == 'target':
        choices = spec.get('choices') if isinstance(spec.get('choices'), list) else []
        return '' if isinstance(value, str) and value in choices else 'target'
    return 'type'


def bindings(raw: dict) -> dict[str, list[tuple[int, str]]]:
    """input name → [(step number, field)] for every bound field."""
    out: dict[str, list[tuple[int, str]]] = {}
    for index, step in enumerate(raw.get('steps', []) or [], 1):
        bind = step.get('bind') if isinstance(step, dict) else None
        if isinstance(bind, dict):
            for fld, name in bind.items():
                out.setdefault(str(name), []).append((index, str(fld)))
    return out


def validation_issues(raw: dict) -> list[tuple[str, str, int, str]]:
    """Pure schema checks for inputs/bindings: (code, field, step, value)."""
    issues: list[tuple[str, str, int, str]] = []
    inputs = raw.get('inputs', None)
    if inputs is not None and (not isinstance(inputs, list) or len(inputs) > MAX_INPUTS):
        return [('inputs', 'inputs', 0, str(MAX_INPUTS))]
    specs: dict[str, dict] = {}
    for row in inputs or []:
        if not isinstance(row, dict):
            issues.append(('object', 'inputs', 0, ''))
            continue
        name = row.get('name')
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name in specs:
            issues.append(('input_name', 'inputs', 0, str(name)[:40]))
            continue
        if row.get('type') not in TYPES:
            issues.append(('input_type', 'inputs', 0, name))
            continue
        if 'label' in row and not (_valid_text(row['label'], 80)):
            issues.append(('text', 'inputs.label', 0, name))
        if row['type'] == 'target':
            choices = row.get('choices')
            if (not isinstance(choices, list) or not 1 <= len(choices) <= MAX_CHOICES
                    or not all(_valid_text(c) for c in choices) or len(set(choices)) != len(choices)):
                issues.append(('input_choices', 'inputs', 0, name))
                continue
        elif 'choices' in row:
            issues.append(('input_choices', 'inputs', 0, name))
        specs[name] = row
    for index, step in enumerate(raw.get('steps', []) or [], 1):
        if not isinstance(step, dict) or 'bind' not in step:
            continue
        bind = step['bind']
        if not isinstance(bind, dict) or len(bind) > 4:
            issues.append(('bind', 'bind', index, ''))
            continue
        for fld, name in bind.items():
            target = FIELDS.get((_kind(step), str(fld)))
            if target is None:
                issues.append(('bind_field', 'bind', index, str(fld)[:40]))      # command/argv/policy/enabled never bind
                continue
            spec = specs.get(name) if isinstance(name, str) else None
            if spec is None:
                issues.append(('bind_input', 'bind', index, str(name)[:40]))
                continue
            if spec['type'] != target[0]:
                issues.append(('bind_type', 'bind', index, f'{fld}:{name}'))
    for name, spec in specs.items():
        if 'default' in spec:
            bounds = [FIELDS[(_kind(raw['steps'][i - 1]), f)][1] for i, f in bindings(raw).get(name, [])
                      if isinstance(raw.get('steps'), list) and 1 <= i <= len(raw['steps']) and isinstance(raw['steps'][i - 1], dict)
                      and (_kind(raw['steps'][i - 1]), f) in FIELDS]
            if check_value(spec, spec['default'], bounds):
                issues.append(('input_default', 'inputs', 0, name))
    return issues


@dataclass
class Resolution:
    values: dict[str, Any]
    missing: list[str]
    invalid: list[tuple[str, str, Any]]
    effective: dict
    affected: dict[str, list[int]] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.missing and not self.invalid


def resolve(raw: dict, given: dict | None) -> Resolution:
    """Resolved values for THIS run applied to a deep copy; the template is untouched."""
    template = deepcopy(raw)
    specs = {row['name']: row for row in declared(template) if isinstance(row.get('name'), str)}
    given = dict(given or {}) if isinstance(given, dict) else {}
    links = bindings(template)
    values: dict[str, Any] = {}
    missing: list[str] = []
    invalid: list[tuple[str, str, Any]] = []
    for name in given:
        if name not in specs:
            invalid.append((str(name), 'unknown', given[name]))
    for name, spec in specs.items():
        if name in given:
            value = given[name]
        elif 'default' in spec:
            value = spec['default']
        else:
            missing.append(name)
            continue
        steps = template.get('steps', []) or []
        bounds = [FIELDS[(_kind(steps[i - 1]), f)][1] for i, f in links.get(name, [])
                  if 1 <= i <= len(steps) and isinstance(steps[i - 1], dict) and (_kind(steps[i - 1]), f) in FIELDS]
        problem = check_value(spec, value, bounds)
        if problem:
            invalid.append((name, problem, value))
            continue
        values[name] = int(value) if spec['type'] == 'monitor' else (float(value) if spec['type'] == 'duration' else value)
    effective = deepcopy(template)
    affected: dict[str, list[int]] = {}
    for index, step in enumerate(effective.get('steps', []) or [], 1):
        bind = step.get('bind') if isinstance(step, dict) else None
        if not isinstance(bind, dict):
            continue
        for fld, name in bind.items():
            if name in values and (_kind(step), str(fld)) in FIELDS:
                step[str(fld)] = values[name]
                affected.setdefault(name, []).append(index)
    return Resolution(values, missing, invalid, effective, affected)


def inputs_fingerprint(template_fingerprint: str, values: dict) -> str:
    """Approval binds the template revision and the resolved values together."""
    encoded = json.dumps({'template': str(template_fingerprint), 'values': values}, sort_keys=True,
                         ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def all_target_paths(raw: dict) -> list[str]:
    """Every object a target input may choose (risk is classified over all of them)."""
    return [c for row in declared(raw) if row.get('type') == 'target' for c in (row.get('choices') or []) if isinstance(c, str)]


# ----------------------------------------------------------------- language
_RUN = {'ru': r'(?:(?:пожалуйста\s+)?(?:запусти|запустить|выполни|включи|начни)\s+(?:сценарий\s+)?)?',
        'en': r'(?:(?:please\s+)?(?:run|start|launch)\s+(?:the\s+)?(?:scenario\s+)?)?',
        'de': r'(?:(?:bitte\s+)?(?:starte|führe)\s+(?:das\s+)?(?:szenario\s+)?)?',
        'fr': r'(?:(?:lance|exécute|démarre)\s+(?:le\s+)?(?:scénario\s+)?)?',
        'es': r'(?:(?:ejecuta|inicia|lanza)\s+(?:el\s+)?(?:escenario\s+)?)?',
        'it': r'(?:(?:avvia|esegui|lancia)\s+(?:lo\s+|il\s+)?(?:scenario\s+)?)?',
        'pt': r'(?:(?:executa|inicia|lança)\s+(?:o\s+)?(?:cenário\s+)?)?'}
_SPLIT = {'ru': r'\s*,\s*|\s+и\s+', 'en': r'\s*,\s*|\s+and\s+', 'de': r'\s*,\s*|\s+und\s+', 'fr': r'\s*,\s*|\s+et\s+',
          'es': r'\s*,\s*|\s+y\s+', 'it': r'\s*,\s*|\s+e\s+', 'pt': r'\s*,\s*|\s+e\s+'}
_V = r'(?P<v>\d+(?:[.,]\d+)?|[^\s,]+)'
_SEC = {'ru': r'(?:\s*(?:секунд[ыау]?|сек\.?|с))?', 'en': r'(?:\s*(?:seconds?|secs?|s))?', 'de': r'(?:\s*(?:sekunden?|sek\.?|s))?',
        'fr': r'(?:\s*(?:secondes?|s))?', 'es': r'(?:\s*(?:segundos?|s))?', 'it': r'(?:\s*(?:secondi|secondo|s))?', 'pt': r'(?:\s*(?:segundos?|s))?'}
_PATTERNS = {
    'monitor': {'ru': (r'(?:на\s+)?(?:монитор[еа]?|экран[еа]?|дисплее?)\s+(?:номер\s+)?' + _V, r'(?:на\s+)?' + _V + r'(?:-?(?:м|ом|ем))?\s+(?:мониторе|экране)'),
                'en': (r'(?:on\s+)?(?:the\s+)?(?:monitor|screen|display)\s+(?:number\s+)?' + _V,),
                'de': (r'(?:auf\s+)?(?:dem\s+)?(?:monitor|bildschirm)\s+(?:nummer\s+)?' + _V,),
                'fr': (r'(?:sur\s+)?(?:l[’\']\s*|le\s+)?(?:écran|moniteur)\s+(?:numéro\s+)?' + _V,),
                'es': (r'(?:en\s+)?(?:el\s+)?(?:monitor|pantalla)\s+(?:número\s+)?' + _V,),
                'it': (r'(?:sul(?:lo)?\s+)?(?:monitor|schermo)\s+(?:numero\s+)?' + _V,),
                'pt': (r'(?:no\s+|na\s+)?(?:monitor|ecrã|tela)\s+(?:número\s+)?' + _V,)},
    'duration': {'ru': (r'(?:с\s+)?(?:паузой|пауза|ожиданием|ожидание|задержкой|задержка)\s+(?:в\s+)?' + _V + _SEC['ru'],),
                 'en': (r'(?:with\s+)?(?:a\s+)?(?:pause|wait|delay)\s+(?:of\s+)?' + _V + _SEC['en'],
                        r'(?:with\s+)?(?:a\s+)?' + _V + r'[- ]seconds?\s+(?:pause|wait|delay)'),
                 'de': (r'(?:mit\s+)?(?:einer\s+)?(?:pause|wartezeit|verzögerung)\s+(?:von\s+)?' + _V + _SEC['de'],),
                 'fr': (r'(?:avec\s+)?(?:une\s+)?(?:pause|attente|délai)\s+(?:de\s+)?' + _V + _SEC['fr'],),
                 'es': (r'(?:con\s+)?(?:una\s+)?(?:pausa|espera|demora)\s+(?:de\s+)?' + _V + _SEC['es'],),
                 'it': (r'(?:con\s+)?(?:una\s+)?(?:pausa|attesa)\s+(?:di\s+)?' + _V + _SEC['it'],),
                 'pt': (r'(?:com\s+)?(?:uma\s+)?(?:pausa|espera)\s+(?:de\s+)?' + _V + _SEC['pt'],)},
    'target': {'ru': (r'(?:с\s+)?(?:файлом|файл|документом|документ|объектом|объект)\s+(?P<v>.+)',),
               'en': (r'(?:with\s+)?(?:the\s+)?(?:file|document)\s+(?P<v>.+)',),
               'de': (r'(?:mit\s+)?(?:der\s+|dem\s+)?(?:datei|dokument)\s+(?P<v>.+)',),
               'fr': (r'(?:avec\s+)?(?:le\s+)?(?:fichier|document)\s+(?P<v>.+)',),
               'es': (r'(?:con\s+)?(?:el\s+)?(?:archivo|documento)\s+(?P<v>.+)',),
               'it': (r'(?:con\s+)?(?:il\s+)?(?:file|documento)\s+(?P<v>.+)',),
               'pt': (r'(?:com\s+)?(?:o\s+)?(?:ficheiro|arquivo|documento)\s+(?P<v>.+)',)},
}


def _key(value: str) -> str:
    return ' '.join(str(value or '').casefold().replace('ё', 'е').strip(' \t\r\n.!?…,;:«»"\'').split())


def _parse_number(text: str, code: str) -> float | None:
    raw = str(text or '').strip().replace(',', '.')
    try:
        return float(raw)
    except ValueError:
        from jarvis.runtime.spoken_numbers import parse_spoken_number
        value = parse_spoken_number(raw, code)
        return float(value) if value is not None else None


def _choose_target(spec: dict, text: str) -> str | None:
    import ntpath
    wanted = _key(text)
    found = []
    for choice in spec.get('choices') or []:
        base = ntpath.basename(str(choice).replace('/', '\\'))
        stem = base.rsplit('.', 1)[0] if '.' in base else base
        if wanted in {_key(choice), _key(base), _key(stem)}:
            found.append(choice)
    return found[0] if len(found) == 1 else None


def parse_value(spec: dict, text: str, code: str) -> Any:
    """One spoken/typed answer for one input (None = not understood)."""
    value = str(text or '').strip()
    kind = spec.get('type')
    if kind == 'target':
        number = _parse_number(value, code)
        choices = spec.get('choices') or []
        if number is not None and number == int(number) and 1 <= number <= len(choices):
            return choices[int(number) - 1]
        for pattern in _PATTERNS['target'].get(code, ()):
            match = re.fullmatch(pattern, value, re.I)
            if match:
                value = match.group('v')
                break
        return _choose_target(spec, value)
    for pattern in _PATTERNS[kind].get(code, ()):
        match = re.fullmatch(pattern, value, re.I)
        if match:
            value = match.group('v')
            break
    else:
        value = re.sub(_SEC.get(code, _SEC['en']) + r'$', '', value, flags=re.I).strip() if kind == 'duration' else value
    number = _parse_number(value, code)
    if number is None:
        return None
    return int(number) if kind == 'monitor' and number == int(number) else number


def _anchored(pattern: str, code: str) -> re.Pattern:
    """A clause pattern matched at the start of the remaining text, ending at a word boundary."""
    split = _SPLIT.get(code, _SPLIT['en'])
    pattern = pattern.replace('(?P<v>.+)', r'(?P<v>.+?)(?=(?:' + split + r')|$)')
    return re.compile(pattern + r'(?=$|[\s,])', re.I)


def _target_at(rest: str, code: str, specs: list[dict], values: dict) -> tuple[tuple[str, Any] | None, int]:
    """«файл notes на мониторе 1»: a target is one of the template's own choices, matched by its name."""
    import ntpath
    for spec in specs:
        if spec.get('type') != 'target' or spec['name'] in values:
            continue
        prefixes = [p.split('(?P<v>')[0] for p in _PATTERNS['target'].get(code, ())]
        prefixes += [r'(?:(?:с|with|mit|avec|con|com)\s+)?' + re.escape(w) + r'\s*[:=]?\s+' for w in {label(spec), str(spec.get('name') or '')} - {''}]
        for prefix in prefixes:
            head = re.match('^' + prefix, rest, re.I)
            if not head:
                continue
            names = []
            for choice in spec.get('choices') or []:
                base = ntpath.basename(str(choice).replace('/', '\\'))
                names += [(choice, choice), (base, choice), (base.rsplit('.', 1)[0] if '.' in base else base, choice)]
            after = rest[head.end():]
            for name, choice in sorted(names, key=lambda row: len(row[0]), reverse=True):
                hit = re.match(re.escape(name) + r'(?=$|[\s,])', after, re.I)
                if hit:
                    others = {c for n, c in names if n.casefold() == name.casefold()}
                    if len(others) != 1:
                        return None, 0                  # two choices share the name: ask, never guess
                    return (spec['name'], choice), head.end() + hit.end()
            word = re.match(r'[^\s,]+', after)
            return ((spec['name'], word.group(0)), head.end() + word.end()) if word else (None, 0)
    return None, 0


_PARAMS_LEAD188 = (r'^(?:с\s+параметрами|with\s+(?:the\s+)?parameters|mit\s+den\s+parametern|avec\s+les\s+paramètres|'
                   r'con\s+los\s+parámetros|con\s+i\s+parametri|com\s+os\s+parâmetros)[\s:]*')


def parse_tail(tail: str, code: str, raw: dict) -> dict | None:
    """«на мониторе 2 с паузой 3 секунды» → {'screen': 2, 'pause': 3.0}; None when any part is not a value."""
    specs = declared(raw)
    rest = ' '.join(str(tail or '').split()).strip(' .!?')
    # NEO 188 atlas (proposed.scenario.run_typed): «… с параметрами пауза=3, экран=2».
    rest = re.sub(_PARAMS_LEAD188, '', rest, count=1, flags=re.I).strip()
    if not rest or not specs:
        return None
    split = re.compile(r'^(?:' + _SPLIT.get(code, _SPLIT['en']) + r'|\s+)', re.I)
    values: dict[str, Any] = {}
    value_of = {'monitor': _V, 'duration': _V + _SEC.get(code, _SEC['en']), 'target': '(?P<v>.+)'}
    while rest:
        lead = split.match(rest)
        if lead and lead.end():
            rest = rest[lead.end():]
            continue
        chosen, used = _target_at(rest, code, specs, values)
        # A label/name of the input selects it («пауза 2», «экран 1»).
        for spec in ([] if chosen else specs):
            for word in sorted({label(spec), str(spec.get('name') or '')} - {''}, key=len, reverse=True):
                hit = _anchored(r'^(?:(?:с|with|mit|avec|con|com)\s+)?' + re.escape(word) + r'(?:\s*[:=]\s*|\s+)' + value_of[spec['type']], code).match(rest)
                if hit:
                    parsed = parse_value(spec, hit.group('v'), code)
                    chosen, used = (spec['name'], parsed if parsed is not None else hit.group('v')), hit.end()
                    break
            if chosen:
                break
        if chosen is None:
            for kind in ('monitor', 'duration'):
                hit = next((m for m in (_anchored('^' + p, code).match(rest) for p in _PATTERNS[kind].get(code, ())) if m), None)
                if not hit:
                    continue
                candidates = [s for s in specs if s.get('type') == kind and s['name'] not in values]
                if len(candidates) != 1:
                    return None                         # ambiguous: the caller asks instead of guessing
                parsed = parse_value(candidates[0], hit.group(0), code)
                chosen, used = (candidates[0]['name'], parsed if parsed is not None else hit.group('v')), hit.end()
                break
        if chosen is None or not used:
            return None
        values[chosen[0]] = chosen[1]
        rest = rest[used:]
    return values or None


def match(text: str, code: str, scenarios: list) -> tuple[str, dict] | None:
    """«<сценарий> на мониторе 2 с паузой 3 секунды» → (scenario name, values)."""
    value = ' '.join(str(text or '').split()).strip(' .!?')
    run = re.match(_RUN.get(code, _RUN['en']), value, re.I)
    rest = value[run.end():] if run else value
    found = []
    for row in scenarios or []:
        if not isinstance(row, dict) or row.get('enabled', True) is not True or not has_inputs(row):
            continue
        names = [str(row.get('name', '') or ''), str(row.get('phrase', '') or '')] + [str(x) for x in (row.get('aliases', []) or [])]
        for name in sorted({n for n in names if n.strip()}, key=len, reverse=True):
            head = re.match(re.escape(name.strip()) + r'(?=[\s,:])[\s,:]*', rest, re.I)
            if head and rest[head.end():].strip():
                parsed = parse_tail(rest[head.end():], code, row)
                if parsed is not None:
                    found.append((str(row.get('name', '') or name), parsed))
                break
    return found[0] if len(found) == 1 else None


_T = {
    'q_monitor': ('Какой монитор использовать для «{scenario}» ({label})? Скажите номер от 1 до 32 (0 — не переносить окно).',
                  'Which monitor should “{scenario}” use ({label})? Say a number from 1 to 32 (0 — do not move the window).',
                  'Welchen Monitor soll „{scenario}“ verwenden ({label})? Sag eine Zahl von 1 bis 32 (0 – Fenster nicht verschieben).',
                  'Quel moniteur utiliser pour « {scenario} » ({label}) ? Dites un nombre de 1 à 32 (0 — ne pas déplacer la fenêtre).',
                  '¿Qué monitor usar para «{scenario}» ({label})? Di un número del 1 al 32 (0: no mover la ventana).',
                  'Quale monitor usare per «{scenario}» ({label})? Di’ un numero da 1 a 32 (0 — non spostare la finestra).',
                  'Que monitor usar para «{scenario}» ({label})? Diga um número de 1 a 32 (0 — não mover a janela).'),
    'q_duration': ('Сколько секунд для «{label}» в «{scenario}»? От 0 до {max}.', 'How many seconds for “{label}” in “{scenario}”? From 0 to {max}.',
                   'Wie viele Sekunden für „{label}“ in „{scenario}“? Von 0 bis {max}.', 'Combien de secondes pour « {label} » dans « {scenario} » ? De 0 à {max}.',
                   '¿Cuántos segundos para «{label}» en «{scenario}»? De 0 a {max}.', 'Quanti secondi per «{label}» in «{scenario}»? Da 0 a {max}.',
                   'Quantos segundos para «{label}» em «{scenario}»? De 0 a {max}.'),
    'q_target': ('Что открыть для «{label}» в «{scenario}»?\n{choices}', 'What should “{scenario}” open for “{label}”?\n{choices}',
                 'Was soll „{scenario}“ für „{label}“ öffnen?\n{choices}', 'Que doit ouvrir « {scenario} » pour « {label} » ?\n{choices}',
                 '¿Qué debe abrir «{scenario}» para «{label}»?\n{choices}', 'Cosa deve aprire «{scenario}» per «{label}»?\n{choices}',
                 'O que «{scenario}» deve abrir para «{label}»?\n{choices}'),
    'bad': ('Значение «{value}» для «{label}» не подходит.', 'The value “{value}” does not fit “{label}”.',
            'Der Wert „{value}“ passt nicht zu „{label}“.', 'La valeur « {value} » ne convient pas pour « {label} ».',
            'El valor «{value}» no sirve para «{label}».', 'Il valore «{value}» non va bene per «{label}».',
            'O valor «{value}» não serve para «{label}».'),
    'unknown': ('В сценарии «{scenario}» нет параметра «{label}».', 'The scenario “{scenario}” has no input “{label}”.',
                'Das Szenario „{scenario}“ hat keinen Parameter „{label}“.', 'Le scénario « {scenario} » n’a pas de paramètre « {label} ».',
                'El escenario «{scenario}» no tiene el parámetro «{label}».', 'Lo scenario «{scenario}» non ha il parametro «{label}».',
                'O cenário «{scenario}» não tem o parâmetro «{label}».'),
    'head': ('Параметры этого запуска (сам сценарий не меняется):', 'Inputs of this run (the scenario itself is not changed):',
             'Parameter dieses Laufs (das Szenario selbst bleibt unverändert):', 'Paramètres de cette exécution (le scénario lui-même ne change pas) :',
             'Parámetros de esta ejecución (el escenario no cambia):', 'Parametri di questa esecuzione (lo scenario non cambia):',
             'Parâmetros desta execução (o cenário em si não muda):'),
    'steps': ('шаги {n}', 'steps {n}', 'Schritte {n}', 'étapes {n}', 'pasos {n}', 'passi {n}', 'passos {n}'),
    'not_set': ('не задано', 'not set', 'nicht gesetzt', 'non défini', 'sin definir', 'non impostato', 'não definido'),
    'invalid': ('недопустимо', 'not allowed', 'nicht zulässig', 'non autorisé', 'no permitido', 'non consentito', 'não permitido'),
    'sec': ('{v} с', '{v} s', '{v} s', '{v} s', '{v} s', '{v} s', '{v} s'),
    'mon': ('монитор {v}', 'monitor {v}', 'Monitor {v}', 'moniteur {v}', 'monitor {v}', 'monitor {v}', 'monitor {v}'),
    'no_move': ('без переноса окна', 'window not moved', 'Fenster nicht verschoben', 'fenêtre non déplacée', 'ventana sin mover',
                'finestra non spostata', 'janela não movida'),
}


def t(key: str, code: str, **values: Any) -> str:
    return _T[key][_CODES.index(code) if code in _CODES else 1].format(**values)


def label(spec: dict) -> str:
    return str(spec.get('label') or spec.get('name') or '')


def _show(spec: dict, value: Any, code: str) -> str:
    if spec.get('type') == 'monitor':
        return t('mon', code, v=int(value)) if int(value) else t('no_move', code)
    if spec.get('type') == 'duration':
        return t('sec', code, v=f'{float(value):g}')
    return str(value)


def upper_bound(raw: dict, name: str) -> float:
    steps = raw.get('steps', []) or []
    bounds = [FIELDS[(_kind(steps[i - 1]), f)][1] for i, f in bindings(raw).get(name, [])
              if 1 <= i <= len(steps) and isinstance(steps[i - 1], dict) and (_kind(steps[i - 1]), f) in FIELDS]
    return min([b for b in bounds if b is not None] or [30.0])


def question(raw: dict, name: str, code: str) -> str:
    spec = next((row for row in declared(raw) if row.get('name') == name), {'name': name, 'type': 'duration'})
    scenario = str(raw.get('name', '') or '')
    if spec.get('type') == 'monitor':
        return t('q_monitor', code, scenario=scenario, label=label(spec))
    if spec.get('type') == 'duration':
        return t('q_duration', code, scenario=scenario, label=label(spec), max=f'{upper_bound(raw, name):g}')
    choices = '\n'.join(f'{i}. {c}' for i, c in enumerate(spec.get('choices') or [], 1))
    return t('q_target', code, scenario=scenario, label=label(spec), choices=choices)


def preview_text(raw: dict, resolution: Resolution, code: str) -> str:
    """Every resolved value, the steps it affects and what is missing/invalid — no action."""
    lines = [t('head', code)]
    bad = {name: value for name, _reason, value in resolution.invalid}
    for spec in declared(raw):
        name = spec.get('name')
        steps = ', '.join(str(i) for i in resolution.affected.get(name, []) or [i for i, _f in bindings(raw).get(name, [])])
        where = f" — {t('steps', code, n=steps)}" if steps else ''
        if name in resolution.values:
            lines.append(f'• {label(spec)}: {_show(spec, resolution.values[name], code)}{where}')
        elif name in bad:
            lines.append(f'• {label(spec)}: {t("invalid", code)} («{bad[name]}»){where}')
        else:
            lines.append(f'• {label(spec)}: {t("not_set", code)}{where}')
    return '\n'.join(lines)

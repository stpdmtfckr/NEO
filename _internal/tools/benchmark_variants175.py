"""End-to-end phrasing benchmark through the real Agent (NEO 175). No command is executed.

Every canonical command of a language is routed once as written; its tool.action is
the oracle. Then the same request said differently must reach the same tool.action:
addressed to NEO, polite, with a filler word, with the object first, in capitals,
with punctuation. Separately, everyday conversation (also with typos) must never
reach any tool: that is what produced «strange answers».

Usage (from _internal): python tools/benchmark_variants175.py [--lang ru] [--json report.json]
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ADDRESS = {'ru': 'джарвис, {p}', 'en': 'jarvis, {p}', 'de': 'jarvis, {p}', 'fr': 'jarvis, {p}', 'es': 'jarvis, {p}',
           'it': 'jarvis, {p}', 'pt': 'jarvis, {p}'}
POLITE_BEFORE = {'ru': 'пожалуйста, {p}', 'en': 'please {p}', 'de': 'bitte {p}', 'fr': "s'il te plaît, {p}",
                 'es': 'por favor, {p}', 'it': 'per favore, {p}', 'pt': 'por favor, {p}'}
POLITE_AFTER = {'ru': '{p}, пожалуйста', 'en': '{p}, please', 'de': '{p}, bitte', 'fr': "{p}, s'il te plaît",
                'es': '{p}, por favor', 'it': '{p}, per favore', 'pt': '{p}, por favor'}
FILLER = {'ru': 'слушай, {p}', 'en': 'hey, {p}', 'de': 'hey, {p}', 'fr': 'dis, {p}', 'es': 'oye, {p}',
          'it': 'senti, {p}', 'pt': 'olha, {p}'}
CAN_YOU = {'en': 'can you {p}', 'ru': 'а теперь {p}'}
KINDS = ('address', 'polite_before', 'polite_after', 'filler', 'can_you', 'object_first', 'capitals', 'punctuation')

# Everyday conversation, with and without typos: none of it may reach a tool.
CONVERSATION = {
    'ru': ['привет', 'привет, как дела?', 'привет как дила', 'превет, как дела', 'как дела', 'как дила', 'как твои дела',
           'што нового', 'что нового', 'доброе утро', 'доброе утро, как спалось', 'добрый вечер', 'добрый вечир',
           'добрый день, как ты?', 'спасибо', 'спосибо', 'спасибо большое', 'пасиба', 'пока', 'до свидания',
           'спокойной ночи', 'мне скучно', 'мне скушно', 'мне грустно', 'я устал', 'я устала', 'расскажи анекдот',
           'раскажи анекдот', 'пошути', 'кто ты', 'кто ты такой', 'как тебя зовут', 'что ты умеешь', 'ты молодец',
           'ты классный', 'давай поболтаем', 'как прошёл твой день', 'как прошол день', 'ну привет', 'ладно', 'понятно',
           'хорошо, спасибо', 'ок спасибо', 'привет, ты тут?', 'как настроение', 'как настраение', 'привет, как настроение',
           'я дома', 'я пришёл', 'я вернулся', 'всё хорошо', 'у меня всё отлично', 'ты лучший', 'спокойной',
           'здарова', 'здорово, как жизнь', 'как жизнь', 'чем занимаешься', 'что делаешь'],
    'en': ['hi', 'hello, how are you?', 'helo, how are you', 'how are you', 'how r u', 'good morning', 'good evening',
           'good mornin', 'thanks', 'thank you so much', 'thx', 'bye', 'good night', 'who are you', 'what can you do',
           'tell me a joke', 'tell me a jok', "i'm bored", 'im bored', "i'm tired", "what's up", 'whats up', 'how was your day',
           "you're awesome", 'nice to meet you', 'ok thanks'],
    'de': ['hallo, wie geht es dir?', 'hallo wie gehts', 'guten morgen', 'guten abend', 'danke', 'danke schön', 'tschüss',
           'wer bist du', 'erzähl mir einen witz', 'mir ist langweilig'],
    'fr': ['salut, ça va ?', 'salut ca va', 'bonjour, comment ça va ?', 'bonsoir', 'merci', 'merci beaucoup', 'au revoir',
           'qui es-tu', 'raconte-moi une blague', "je m'ennuie"],
    'es': ['hola, ¿qué tal?', 'hola que tal', 'hola, ¿cómo estás?', 'buenos días', 'buenas noches', 'gracias',
           'muchas gracias', 'adiós', 'quién eres', 'cuéntame un chiste', 'estoy aburrido'],
    'it': ['ciao, come stai?', 'ciao come stai', 'buongiorno', 'buonasera', 'grazie', 'grazie mille', 'arrivederci',
           'chi sei', 'raccontami una barzelletta', 'mi annoio'],
    'pt': ['olá, tudo bem?', 'ola tudo bem', 'olá, como estás?', 'bom dia', 'boa noite', 'obrigado', 'muito obrigado',
           'tchau', 'quem és tu', 'conta-me uma piada', 'estou entediado'],
}


# Two tools that do the same thing (a NEO 173 app skill and the older command): not a mistake.
SAME = {frozenset({'apps.browser', 'browser_advanced.restore_closed'}), frozenset({'apps.create_folder', 'filesystem.create_folder'})}


def same(expected: str, got: str) -> bool:
    return expected == got or frozenset({expected, got}) in SAME


def variant(phrase: str, kind: str, code: str) -> str | None:
    if kind == 'address':
        return ADDRESS[code].format(p=phrase)
    if kind == 'polite_before':
        return POLITE_BEFORE[code].format(p=phrase)
    if kind == 'polite_after':
        return POLITE_AFTER[code].format(p=phrase)
    if kind == 'filler':
        return FILLER[code].format(p=phrase)
    if kind == 'can_you':
        return CAN_YOU[code].format(p=phrase) if code in CAN_YOU else None
    if kind == 'object_first':
        words = phrase.split()
        # «открой калькулятор» -> «калькулятор открой»: natural in Russian only.
        return ' '.join(words[1:] + words[:1]) if code == 'ru' and 2 <= len(words) <= 4 else None
    if kind == 'capitals':
        return phrase[:1].upper() + phrase[1:] + '.'
    if kind == 'punctuation':
        return phrase + '!'
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--json', help='write every case to this JSON file')
    parser.add_argument('--lang', default='ru', choices=('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'))
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    code = args.lang
    from jarvis.runtime.natural_commands import LANGUAGES
    from jarvis.testing.atlas_session import AtlasSession
    from jarvis.testing.isolation import external_io_blocked

    phrases = []
    for path in sorted(glob.glob(str(ROOT / 'config/canonical_phrases/*.json'))):
        for variants in json.loads(Path(path).read_text(encoding='utf-8')).values():
            filler = {'ru': 'тест'}.get(code, 'test')
            phrases.append(re.sub(r'\{\w+\}', filler, variants[LANGUAGES.index(code)]).lower())
    phrases = sorted(set(phrases))

    def route(session, text):
        session.reset_case(code)
        started = time.perf_counter()
        turn = session.agent.handle(text)
        elapsed = (time.perf_counter() - started) * 1000
        pending = session.agent.conversations.get('desktop').pending_intent
        if session.calls:
            call = session.calls[0]
            return f"{call['tool']}.{call['arguments'].get('action', '')}", elapsed, turn
        if pending is not None:
            return f"ask:{pending.tool}.{pending.arguments.get('action', '')}", elapsed, turn
        if turn.pending is not None:
            return f'confirm:{turn.pending.tool_name}', elapsed, turn
        return 'none:' + ','.join(turn.tool_details or []), elapsed, turn

    rows, talk, times = [], [], []
    with external_io_blocked():
        session = AtlasSession(Path(tempfile.mkdtemp()) / 'variants175', locale=code)
        oracle = {}
        for phrase in phrases:
            got, _, _ = route(session, phrase)
            if not got.startswith('none:'):
                oracle[phrase] = got
        for kind in KINDS:
            for phrase in phrases:
                text = variant(phrase, kind, code)
                if phrase not in oracle or not text or text == phrase:
                    continue
                got, elapsed, _ = route(session, text)
                times.append(elapsed)
                rows.append({'kind': kind, 'phrase': phrase, 'variant': text, 'expected': oracle[phrase], 'got': got})
        for phrase in CONVERSATION.get(code, []):
            got, _, turn = route(session, phrase)
            talk.append({'phrase': phrase, 'got': got, 'reply': str(turn.text)[:160]})
        session.finish()

    print(f'{"phrasing":<15}{"cases":>7}{"understood":>12}{"other tool":>12}')
    for kind in (*KINDS, None):
        part = [r for r in rows if kind is None or r['kind'] == kind]
        if not part:
            continue
        ok = sum(same(r['expected'], r['got']) for r in part)
        wrong = sum(not same(r['expected'], r['got']) and not r['got'].startswith(('none', 'ask', 'confirm')) for r in part)
        print(f'{kind or "TOTAL":<15}{len(part):>7}{ok / len(part):>12.0%}{wrong / len(part):>12.1%}')
    acted = [r for r in talk if not r['got'].startswith('none')]
    print(f'conversation: {len(talk)} phrases, reached a tool: {len(acted)}')
    for row in acted:
        print(f"  ! {row['phrase']!r} -> {row['got']}")
    if times:
        times.sort()
        print(f'latency p50={times[len(times) // 2]:.1f}ms p95={times[int(len(times) * .95)]:.1f}ms')
    if args.json:
        Path(args.json).write_text(json.dumps({'commands': rows, 'conversation': talk}, ensure_ascii=False, indent=1), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""Training and test data of the NEO command model.

Example: phrase + language + context -> intent (tool.action) + slot spans.

Sources
* Phrase Atlas (jarvis/locales/commands/<lang>.json, ~22 000 phrases per language),
* command grammar packs (config/canonical_phrases, config/command_variants),
* contextual follow-ups («назад» after the browser vs. after music),
* negatives that must not run anything: small talk and questions,
* confirmed corrections of the user (only when the user allows it).

Split: examples are grouped by (intent, template); a whole group goes either to
training or to the test set, so the test measures phrasings the model never saw.
The typo cases of the grammar tests are a separate, never-trained test set.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import re
import zlib
from typing import Any, Iterator

LANGUAGES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
NONE = '__none__'
_SLOT = re.compile(r'\{([a-z_][a-z0-9_]*)\}', re.I)
TEST_PERCENT = 12

# Sample slot values. Generic values are shared; texts are per language.
_COMMON = {
    'name': ['Telegram', 'Steam', 'Discord', 'Notepad', 'Chrome', 'OBS', 'Spotify', 'Word', 'Excel', 'Photoshop'],
    'application': ['Telegram', 'Steam', 'Chrome', 'Discord'],
    'url': ['example.com', 'https://github.com', 'youtube.com', 'wikipedia.org'],
    'seconds': ['30', '90', '300'], 'minutes': ['5', '15', '40'], 'volume': ['20', '50', '80'], 'percent': ['25', '60'],
    'count': ['2', '3', '5'], 'value': ['10', '30', '70'], 'index': ['2', '3', '1'], 'monitor': ['1', '2'], 'number': ['3', '7', '12'],
    'x': ['400', '800'], 'y': ['300', '600'], 'zoom': ['125', '150'], 'font_size': ['14', '18'], 'position_seconds': ['30', '90'],
    'key': ['F5', 'Enter', 'ctrl s', 'Esc', 'пробел'], 'keys': ['ctrl+shift+t', 'alt+tab', 'ctrl+s'],
    'city': ['Moscow', 'Berlin', 'Paris', 'Madrid', 'Rome', 'Lisbon', 'London'],
    'playlist': ['Chill', 'Rock', 'Workout'], 'channel': ['general', 'music'], 'server': ['Friends', 'Gaming'],
    'target_language': ['English', 'German', 'French'], 'time': ['10:30', '18:00'], 'date': ['2030-01-15', 'tomorrow'],
    'to': ['anna@example.com'], 'recipient': ['anna@example.com'], 'id': ['NEO_QA'], 'event_id': ['evt1'], 'message_id': ['42'],
    'device_name': ['Headphones', 'Speakers'], 'button': ['left', 'right'], 'host': ['example.com'],
}
_TEXTS = {
    'ru': {'text': ['привет как дела', 'встреча в пять', 'купить молоко'], 'query': ['котики', 'рецепт борща', 'погода на завтра', 'отчёт за май'],
           'title': ['Покупки', 'Работа', 'Идеи'], 'source': ['отчёт.docx', 'фото.png'], 'destination': ['Документы', 'Рабочий стол'],
           'path': ['отчёт.docx', 'заметки.txt'], 'folder': ['Документы', 'Загрузки'], 'new_name': ['итог.docx'], 'content': ['список дел'],
           'body': ['скоро буду'], 'subject': ['встреча'], 'topic': ['работа'], 'target': ['Telegram'], 'instruction': ['что здесь написано'],
           'description': ['таймер для чая'], 'old_text': ['старый'], 'new_text': ['новый'], 'label': ['Работа']},
    'en': {'text': ['hello how are you', 'meeting at five', 'buy milk'], 'query': ['cats', 'pasta recipe', 'weather tomorrow', 'may report'],
           'title': ['Shopping', 'Work', 'Ideas'], 'source': ['report.docx', 'photo.png'], 'destination': ['Documents', 'Desktop'],
           'path': ['report.docx', 'notes.txt'], 'folder': ['Documents', 'Downloads'], 'new_name': ['final.docx'], 'content': ['todo list'],
           'body': ['on my way'], 'subject': ['meeting'], 'topic': ['work'], 'target': ['Telegram'], 'instruction': ['what does it say'],
           'description': ['tea timer'], 'old_text': ['old'], 'new_text': ['new'], 'label': ['Work']},
    'de': {'text': ['hallo wie geht es', 'Treffen um fünf'], 'query': ['Katzen', 'Nudelrezept', 'Wetter morgen'], 'title': ['Einkauf', 'Arbeit'],
           'source': ['Bericht.docx'], 'destination': ['Dokumente'], 'path': ['Bericht.docx'], 'folder': ['Dokumente', 'Downloads'],
           'new_name': ['Ende.docx'], 'content': ['Aufgaben'], 'body': ['bin unterwegs'], 'subject': ['Treffen'], 'topic': ['Arbeit'],
           'target': ['Telegram'], 'instruction': ['was steht hier'], 'description': ['Tee Timer'], 'old_text': ['alt'], 'new_text': ['neu'], 'label': ['Arbeit']},
    'fr': {'text': ['bonjour ça va', 'réunion à cinq heures'], 'query': ['chats', 'recette de pâtes', 'météo demain'], 'title': ['Courses', 'Travail'],
           'source': ['rapport.docx'], 'destination': ['Documents'], 'path': ['rapport.docx'], 'folder': ['Documents', 'Téléchargements'],
           'new_name': ['final.docx'], 'content': ['tâches'], 'body': ['j’arrive'], 'subject': ['réunion'], 'topic': ['travail'],
           'target': ['Telegram'], 'instruction': ['qu’est-ce qui est écrit'], 'description': ['minuteur thé'], 'old_text': ['ancien'], 'new_text': ['nouveau'], 'label': ['Travail']},
    'es': {'text': ['hola qué tal', 'reunión a las cinco'], 'query': ['gatos', 'receta de pasta', 'tiempo mañana'], 'title': ['Compras', 'Trabajo'],
           'source': ['informe.docx'], 'destination': ['Documentos'], 'path': ['informe.docx'], 'folder': ['Documentos', 'Descargas'],
           'new_name': ['final.docx'], 'content': ['tareas'], 'body': ['voy en camino'], 'subject': ['reunión'], 'topic': ['trabajo'],
           'target': ['Telegram'], 'instruction': ['qué pone aquí'], 'description': ['temporizador té'], 'old_text': ['viejo'], 'new_text': ['nuevo'], 'label': ['Trabajo']},
    'it': {'text': ['ciao come va', 'riunione alle cinque'], 'query': ['gatti', 'ricetta pasta', 'meteo domani'], 'title': ['Spesa', 'Lavoro'],
           'source': ['rapporto.docx'], 'destination': ['Documenti'], 'path': ['rapporto.docx'], 'folder': ['Documenti', 'Download'],
           'new_name': ['finale.docx'], 'content': ['compiti'], 'body': ['sto arrivando'], 'subject': ['riunione'], 'topic': ['lavoro'],
           'target': ['Telegram'], 'instruction': ['cosa c’è scritto'], 'description': ['timer tè'], 'old_text': ['vecchio'], 'new_text': ['nuovo'], 'label': ['Lavoro']},
    'pt': {'text': ['olá tudo bem', 'reunião às cinco'], 'query': ['gatos', 'receita de massa', 'tempo amanhã'], 'title': ['Compras', 'Trabalho'],
           'source': ['relatório.docx'], 'destination': ['Documentos'], 'path': ['relatório.docx'], 'folder': ['Documentos', 'Transferências'],
           'new_name': ['final.docx'], 'content': ['tarefas'], 'body': ['estou a caminho'], 'subject': ['reunião'], 'topic': ['trabalho'],
           'target': ['Telegram'], 'instruction': ['o que diz aqui'], 'description': ['temporizador chá'], 'old_text': ['velho'], 'new_text': ['novo'], 'label': ['Trabalho']},
}
_RU_ORDINALS = ['вторую', 'первую', 'третью']
_ORDINALS = {'ru': _RU_ORDINALS, 'en': ['second', 'first', 'third'], 'de': ['zweite', 'erste'], 'fr': ['deuxième', 'premier'],
             'es': ['segundo', 'primer'], 'it': ['secondo', 'primo'], 'pt': ['segundo', 'primeiro']}

# Contextual follow-ups: the same short phrase means different things after different tools.
CONTEXT_PHRASES = {
    'ru': {('назад', 'browser'): 'browser_nav.back', ('назад', 'music'): 'music.previous', ('вперёд', 'browser'): 'browser_nav.forward',
           ('дальше', 'music'): 'music.next', ('следующий', 'music'): 'music.next', ('обнови', 'browser'): 'browser_nav.reload', ('пауза', 'music'): 'music.pause'},
    'en': {('back', 'browser'): 'browser_nav.back', ('back', 'music'): 'music.previous', ('forward', 'browser'): 'browser_nav.forward',
           ('next', 'music'): 'music.next', ('refresh', 'browser'): 'browser_nav.reload', ('pause', 'music'): 'music.pause'},
    'de': {('zurück', 'browser'): 'browser_nav.back', ('zurück', 'music'): 'music.previous', ('weiter', 'music'): 'music.next',
           ('vorwärts', 'browser'): 'browser_nav.forward'},
    'fr': {('retour', 'browser'): 'browser_nav.back', ('retour', 'music'): 'music.previous', ('suivant', 'music'): 'music.next'},
    'es': {('atrás', 'browser'): 'browser_nav.back', ('atrás', 'music'): 'music.previous', ('siguiente', 'music'): 'music.next'},
    'it': {('indietro', 'browser'): 'browser_nav.back', ('indietro', 'music'): 'music.previous', ('avanti', 'browser'): 'browser_nav.forward',
           ('prossima', 'music'): 'music.next'},
    'pt': {('voltar', 'browser'): 'browser_nav.back', ('voltar', 'music'): 'music.previous', ('seguinte', 'music'): 'music.next'},
}

# Questions and remarks that are conversation, not commands.
_NEGATIVE_SHELLS = {
    'ru': ['почему это происходит', 'почему {t}', 'что такое {t}', 'как работает {t}', 'расскажи про {t}', 'объясни {t}', 'мне кажется {t} это интересно',
           'я думаю про {t}', 'как ты думаешь {t} это хорошо', 'что ты знаешь о {t}', 'сегодня был трудный день', 'мне скучно', 'спасибо большое',
           'ты молодец', 'как дела', 'что нового', 'интересно почему так', 'а зачем это нужно', 'что ты об этом думаешь', 'я не понимаю почему так'],
    'en': ['why is this happening', 'why {t}', 'what is {t}', 'how does {t} work', 'tell me about {t}', 'explain {t}', 'i think {t} is interesting',
           'what do you think about {t}', 'today was a hard day', 'i am bored', 'thank you so much', 'good job', 'how are you', 'what is new',
           'i wonder why', 'what is this for', 'i do not understand why'],
    'de': ['warum passiert das', 'warum {t}', 'was ist {t}', 'wie funktioniert {t}', 'erzähl mir von {t}', 'erkläre {t}', 'was denkst du über {t}',
           'heute war ein schwerer tag', 'mir ist langweilig', 'danke schön', 'wie geht es dir', 'ich verstehe nicht warum'],
    'fr': ['pourquoi ça arrive', 'pourquoi {t}', 'qu’est-ce que {t}', 'comment fonctionne {t}', 'parle-moi de {t}', 'explique {t}',
           'que penses-tu de {t}', 'journée difficile aujourd’hui', 'je m’ennuie', 'merci beaucoup', 'comment ça va', 'je ne comprends pas pourquoi'],
    'es': ['por qué pasa esto', 'por qué {t}', 'qué es {t}', 'cómo funciona {t}', 'háblame de {t}', 'explica {t}', 'qué piensas de {t}',
           'hoy fue un día difícil', 'estoy aburrido', 'muchas gracias', 'cómo estás', 'no entiendo por qué'],
    'it': ['perché succede questo', 'perché {t}', 'cos’è {t}', 'come funziona {t}', 'parlami di {t}', 'spiega {t}', 'cosa pensi di {t}',
           'oggi è stata una giornata dura', 'mi annoio', 'grazie mille', 'come stai', 'non capisco perché'],
    'pt': ['porque é que isto acontece', 'porque {t}', 'o que é {t}', 'como funciona {t}', 'fala-me de {t}', 'explica {t}', 'o que achas de {t}',
           'hoje foi um dia difícil', 'estou aborrecido', 'muito obrigado', 'como estás', 'não percebo porquê'],
}
_TOPICS = {
    'ru': ['чёрные дыры', 'интернет', 'эта ошибка', 'музыка', 'погода', 'космос', 'нейросети', 'история рима', 'браузер', 'компьютер'],
    'en': ['black holes', 'the internet', 'this error', 'music', 'the weather', 'space', 'neural networks', 'roman history', 'the browser'],
    'de': ['schwarze Löcher', 'das Internet', 'dieser Fehler', 'Musik', 'das Wetter', 'der Weltraum', 'der Browser'],
    'fr': ['les trous noirs', 'internet', 'cette erreur', 'la musique', 'la météo', 'l’espace', 'le navigateur'],
    'es': ['los agujeros negros', 'internet', 'este error', 'la música', 'el tiempo', 'el espacio', 'el navegador'],
    'it': ['i buchi neri', 'internet', 'questo errore', 'la musica', 'il meteo', 'lo spazio', 'il browser'],
    'pt': ['os buracos negros', 'a internet', 'este erro', 'a música', 'o tempo', 'o espaço', 'o navegador'],
}


# Values used only in the test set, so slot accuracy is measured on values the model never saw.
_TEST_VALUES = {
    'name': ['Blender', 'Zoom', 'VLC', 'Thunderbird', 'GIMP'], 'application': ['Zoom', 'VLC'], 'city': ['Oslo', 'Prague', 'Kyoto'],
    'url': ['openstreetmap.org', 'example.net'], 'playlist': ['Jazz Evening', 'Focus'], 'seconds': ['45', '120'], 'minutes': ['7', '25'],
    'volume': ['35', '65'], 'percent': ['45'], 'value': ['15', '55'], 'number': ['5', '9'], 'key': ['F2', 'Tab', 'Home'],
}
_TEST_TEXTS = {
    'ru': {'text': ['завтра созвон в девять', 'позвонить маме'], 'query': ['ремонт велосипеда', 'история кино'], 'title': ['Путешествия'],
           'folder': ['Проекты'], 'path': ['план.txt'], 'source': ['смета.xlsx']},
    'en': {'text': ['call mom tomorrow', 'dinner at eight'], 'query': ['bike repair', 'film history'], 'title': ['Travel'],
           'folder': ['Projects'], 'path': ['plan.txt'], 'source': ['budget.xlsx']},
}


def _values(slot: str, code: str, split: str = 'train') -> list[str]:
    if split == 'test':
        special = _TEST_TEXTS.get(code, {}).get(slot) or _TEST_VALUES.get(slot)
        if special:
            return special
    if slot in {'index'}:
        return _ORDINALS.get(code, ['2'])
    texts = _TEXTS.get(code, _TEXTS['en'])
    return texts.get(slot) or _COMMON.get(slot) or texts['query']


# Free-text slots: in training, half of the values are random words, so the model learns where a value
# stands in the request (its context), not a short list of example values.
_FREE_SLOTS = {'name', 'application', 'title', 'query', 'text', 'content', 'body', 'subject', 'topic', 'playlist', 'target',
               'city', 'channel', 'server', 'folder', 'label', 'description', 'instruction', 'device_name'}
_VOCAB: dict[str, list[str]] = {}


def set_vocabulary(code: str, words: list[str]) -> None:
    _VOCAB[code] = sorted(set(words))


def _random_value(code: str, rng: random.Random) -> str:
    words = _VOCAB.get(code) or ['alpha', 'beta', 'gamma']
    value = ' '.join(rng.choice(words) for _ in range(rng.choice((1, 1, 2, 2, 3))))
    return value.capitalize() if rng.random() < 0.4 else value


def fill(template: str, code: str, rng: random.Random, split: str = 'train') -> tuple[str, list[dict[str, Any]]]:
    """Template with {slots} -> text and the character spans of the filled slots."""
    text, spans, cursor = '', [], 0
    for match in _SLOT.finditer(template):
        text += template[cursor:match.start()]
        if split == 'train' and match.group(1) in _FREE_SLOTS and rng.random() < 0.5:
            value = _random_value(code, rng)
        else:
            value = rng.choice(_values(match.group(1), code, split))
        spans.append({'name': match.group(1), 'start': len(text), 'end': len(text) + len(value), 'value': value})
        text += value
        cursor = match.end()
    return text + template[cursor:], spans


def _group(intent: str, template: str) -> str:
    return intent + '|' + ' '.join(template.casefold().split())


def _is_test(group: str) -> bool:
    return zlib.crc32(group.encode('utf-8')) % 100 < TEST_PERCENT


def _typo(text: str, rng: random.Random) -> str:
    words = text.split(' ')
    candidates = [i for i, w in enumerate(words) if len(w) >= 4 and '{' not in w]
    if not candidates:
        return text
    i = rng.choice(candidates)
    word = words[i]
    j = rng.randrange(1, len(word) - 1)
    kind = rng.randrange(3)
    if kind == 0:
        word = word[:j] + word[j + 1] + word[j] + word[j + 2:]
    elif kind == 1:
        word = word[:j] + word[j + 1:]
    else:
        word = word[:j] + word[j] + word[j:]
    words[i] = word
    return ' '.join(words)


def _template_sources(root: Path, code: str) -> Iterator[tuple[str, str, str]]:
    """(intent, template, source) from the atlas and the grammar packs."""
    index = LANGUAGES.index(code)
    atlas = root / 'jarvis' / 'locales' / 'commands' / f'{code}.json'
    if atlas.exists():
        pack = json.loads(atlas.read_text(encoding='utf-8'))
        for intent, phrases in pack.get('action_phrases', {}).items():
            for phrase in phrases:
                yield intent, phrase, 'atlas'
    for path in sorted((root / 'config' / 'canonical_phrases').glob('*.json')):
        for intent, row in json.loads(path.read_text(encoding='utf-8')).items():
            if isinstance(row, list) and len(row) == 7:
                yield intent, row[index], 'canonical'
    for path in sorted((root / 'config' / 'command_variants').glob('*.json')):
        for intent, languages in json.loads(path.read_text(encoding='utf-8')).items():
            for phrase in languages.get(code, []):
                yield intent, phrase, 'variant'


def build(root: Path, *, languages: tuple[str, ...] = LANGUAGES, corrections: list[dict[str, Any]] | None = None,
          seed: int = 1) -> dict[str, list[dict[str, Any]]]:
    """{'train': [...], 'test': [...], 'typo_test': [...]} of example dicts."""
    rng = random.Random(seed)
    out: dict[str, list[dict[str, Any]]] = {'train': [], 'test': [], 'typo_test': []}
    for code in languages:
        seen: dict[str, str] = {}
        conflicts: set[str] = set()
        rows: list[dict[str, Any]] = []
        sources = list(_template_sources(root, code))
        test_words = {w.casefold() for values in list(_TEST_VALUES.values()) + list(_TEST_TEXTS.get(code, {}).values()) for v in values for w in v.split()}
        set_vocabulary(code, [w for _, template, _ in sources for w in re.findall(r'\w{4,}', _SLOT.sub(' ', template).casefold())
                              if not w.isdigit() and w not in test_words])
        for intent, template, source in sources:
            template = ' '.join(str(template).split())
            if not template or '.' not in intent:
                continue
            group = _group(intent, template)
            split = 'test' if _is_test(group) else 'train'
            fills = 2 if (split == 'train' and _SLOT.search(template)) else 1
            for _ in range(fills):
                text, spans = fill(template, code, rng, split)
                key = ' '.join(text.casefold().split())
                if key in seen and seen[key] != intent:
                    conflicts.add(key)
                seen.setdefault(key, intent)
                rows.append({'language': code, 'text': text, 'context': '', 'intent': intent, 'slots': spans,
                             'group': group, 'split': split, 'source': source})
                if split == 'train' and rng.random() < 0.25:
                    noisy = _typo(text, rng)
                    if noisy != text and not spans:
                        rows.append({'language': code, 'text': noisy, 'context': '', 'intent': intent, 'slots': [],
                                     'group': group, 'split': 'train', 'source': 'aug_typo'})
        # The same words meaning two different actions cannot be learned from the text alone.
        rows = [r for r in rows if ' '.join(r['text'].casefold().split()) not in conflicts]
        for (phrase, context), intent in CONTEXT_PHRASES.get(code, {}).items():
            group = f'context|{phrase}|{context}'
            for _ in range(6):
                rows.append({'language': code, 'text': phrase, 'context': context, 'intent': intent, 'slots': [],
                             'group': group, 'split': 'train', 'source': 'context'})
            rows.append({'language': code, 'text': phrase, 'context': context, 'intent': intent, 'slots': [],
                         'group': group, 'split': 'test', 'source': 'context'})
        for shell in _NEGATIVE_SHELLS.get(code, []):
            for topic in (_TOPICS.get(code, []) if '{t}' in shell else ['']):
                group = f'none|{shell}|{topic}'
                rows.append({'language': code, 'text': shell.replace('{t}', topic), 'context': '', 'intent': NONE, 'slots': [],
                             'group': group, 'split': 'test' if _is_test(group) else 'train', 'source': 'negative'})
        try:
            from jarvis.runtime.social_atlas import catalog
            for phrase in sorted(catalog(code)):
                group = f'none|social|{phrase}'
                rows.append({'language': code, 'text': phrase, 'context': '', 'intent': NONE, 'slots': [],
                             'group': group, 'split': 'test' if _is_test(group) else 'train', 'source': 'social'})
        except Exception:
            pass
        for row in rows:
            out[row['split']].append(row)
        try:
            from jarvis.testing.typo_cases161 import cases as typo_cases
            for case in typo_cases(root, code):
                out['typo_test'].append({'language': code, 'text': case['phrase'], 'context': '',
                                         'intent': f"{case['expected_tool']}.{case['expected_action']}", 'slots': [],
                                         'group': 'typo161', 'split': 'typo_test', 'source': case.get('source', 'typo')})
        except Exception:
            pass
    for row in corrections or []:
        right = row.get('right') or {}
        intent = f"{right.get('tool', '')}.{(right.get('arguments') or {}).get('action', '')}"
        if row.get('language') in languages and right.get('tool'):
            out['train'].append({'language': row['language'], 'text': row['text'], 'context': '', 'intent': intent, 'slots': [],
                                 'group': 'correction', 'split': 'train', 'source': 'correction'})
    return out


def fingerprint(data: dict[str, list[dict[str, Any]]]) -> str:
    digest = hashlib.sha256()
    for split in sorted(data):
        for row in data[split]:
            digest.update(f"{split}|{row['language']}|{row['context']}|{row['intent']}|{row['text']}\n".encode('utf-8'))
    return digest.hexdigest()[:16]


def save(data: dict[str, list[dict[str, Any]]], folder: Path) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for split, rows in data.items():
        with (folder / f'{split}.jsonl').open('w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        counts[split] = len(rows)
    info = {'fingerprint': fingerprint(data), 'counts': counts,
            'languages': sorted({r['language'] for rows in data.values() for r in rows}),
            'intents': len({r['intent'] for r in data['train']})}
    (folder / 'dataset.json').write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding='utf-8')
    return info

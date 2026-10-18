"""NEO 169: what NEO remembers about the user, stored locally and fully under the user's control.

* Preferences and notes live in the existing SQLite ``memories`` table
  (keys ``pref:<name>`` and ``note:<time>``; older plain keys stay readable).
* Personal names for commands reuse :class:`jarvis.runtime.conversation.PersonalAliases`.
* Confirmed command corrections are kept in ``<data>/neo_model/corrections.jsonl``.
  They work at once as exact rules and, separately, can become training examples
  for the NEO command model. Memory never requires retraining.

``memory.enabled = false`` stops using and saving memory (stored data is kept
until the user clears it).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any

_CODES = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_LOCK = threading.Lock()
_LAST_KEY: dict[str, str] = {}          # «забудь это» -> the key NEO last mentioned
_INTERNAL_PREFIXES = ('personal_aliases.', 'context.', 'neo_', 'system.', 'internal.')

KEY_MAX, VALUE_MAX, NOTE_MAX = 120, 500, 1000      # NEO 183: the limits are told, never applied silently
_TEXT = {
    'note_corrected': ('Исправил: «{text}».', 'Corrected: “{text}”.', 'Korrigiert: „{text}“.', 'Corrigé : « {text} ».',
                       'Corregido: «{text}».', 'Corretto: «{text}».', 'Corrigido: «{text}».'),
    'too_long': ('Не сохранил: слишком длинно ({length} из {limit} символов). Сократите текст или сохраните его заметкой на полке.',
                 'Not saved: too long ({length} of {limit} characters). Shorten it or keep it as a note on the shelf.',
                 'Nicht gespeichert: zu lang ({length} von {limit} Zeichen). Kürze den Text oder lege ihn als Notiz im Regal ab.',
                 'Non enregistré : trop long ({length} sur {limit} caractères). Raccourcissez-le ou gardez-le comme note sur l’étagère.',
                 'No guardado: demasiado largo ({length} de {limit} caracteres). Acórtalo o guárdalo como nota en la estantería.',
                 'Non salvato: troppo lungo ({length} su {limit} caratteri). Accorcialo o salvalo come nota sullo scaffale.',
                 'Não guardado: demasiado longo ({length} de {limit} caracteres). Encurte-o ou guarde-o como nota na estante.'),
    'saved': ('Запомнил: {key} — {value}.', 'Remembered: {key} — {value}.', 'Gemerkt: {key} — {value}.', 'C’est noté : {key} — {value}.',
              'Lo recordaré: {key} — {value}.', 'Memorizzato: {key} — {value}.', 'Memorizado: {key} — {value}.'),
    'note_saved': ('Сохранил заметку.', 'Note saved.', 'Notiz gespeichert.', 'Note enregistrée.', 'Nota guardada.', 'Nota salvata.', 'Nota guardada.'),
    'forgot': ('Забыл: {key}.', 'Forgotten: {key}.', 'Vergessen: {key}.', 'Oublié : {key}.', 'Olvidado: {key}.', 'Dimenticato: {key}.', 'Esquecido: {key}.'),
    'not_found': ('Такой записи нет: {key}.', 'No such memory: {key}.', 'Kein solcher Eintrag: {key}.', 'Aucune entrée : {key}.',
                  'No hay tal dato: {key}.', 'Nessuna voce: {key}.', 'Não existe: {key}.'),
    'nothing_last': ('Не понял, что именно забыть. Скажите «что ты обо мне запомнил», а затем «забудь» и название.',
                     'I am not sure what to forget. Say “what do you remember about me”, then “forget” and the name.',
                     'Unklar, was ich vergessen soll. Sagen Sie „was weißt du über mich“ und dann „vergiss“ mit dem Namen.',
                     'Je ne sais pas quoi oublier. Dites « que sais-tu de moi », puis « oublie » et le nom.',
                     'No sé qué olvidar. Di «qué recuerdas de mí» y luego «olvida» y el nombre.',
                     'Non so cosa dimenticare. Di’ «cosa ricordi di me» e poi «dimentica» e il nome.',
                     'Não sei o que esquecer. Diga «o que lembras de mim» e depois «esquece» e o nome.'),
    'cleared': ('Сохранённая память очищена: {count} записей.', 'Saved memory cleared: {count} entries.', 'Gespeicherte Erinnerungen gelöscht: {count}.',
                'Mémoire effacée : {count} entrées.', 'Memoria borrada: {count} entradas.', 'Memoria cancellata: {count} voci.', 'Memória apagada: {count} entradas.'),
    'disabled': ('Память отключена: я не буду сохранять и использовать сведения о вас. Сохранённое не удалено.',
                 'Memory is off: I will not save or use facts about you. Saved entries are kept.',
                 'Erinnerung aus: ich speichere und nutze nichts über Sie. Gespeichertes bleibt erhalten.',
                 'Mémoire désactivée : je n’enregistre ni n’utilise rien sur vous. Les entrées sont conservées.',
                 'Memoria desactivada: no guardaré ni usaré datos sobre ti. Lo guardado se conserva.',
                 'Memoria disattivata: non salverò né userò dati su di te. I dati salvati restano.',
                 'Memória desativada: não guardo nem uso dados sobre si. O guardado mantém-se.'),
    'enabled': ('Память включена.', 'Memory is on.', 'Erinnerung ist an.', 'Mémoire activée.', 'Memoria activada.', 'Memoria attivata.', 'Memória ativada.'),
    'is_off': ('Память отключена. Скажите «включи память», чтобы я снова запоминал.', 'Memory is off. Say “turn memory on” first.',
               'Erinnerung ist aus. Sagen Sie „Erinnerung einschalten“.', 'La mémoire est désactivée. Dites « active la mémoire ».',
               'La memoria está desactivada. Di «activa la memoria».', 'La memoria è disattivata. Di’ «attiva la memoria».',
               'A memória está desativada. Diga «ativa a memória».'),
    'empty': ('Я пока ничего о вас не запомнил.', 'I have not remembered anything about you yet.', 'Ich habe mir noch nichts über Sie gemerkt.',
              'Je n’ai encore rien retenu sur vous.', 'Todavía no recuerdo nada sobre ti.', 'Non ricordo ancora nulla di te.', 'Ainda não memorizei nada sobre si.'),
    'h_prefs': ('Предпочтения', 'Preferences', 'Vorlieben', 'Préférences', 'Preferencias', 'Preferenze', 'Preferências'),
    'h_names': ('Ваши названия команд', 'Your command names', 'Ihre Befehlsnamen', 'Vos noms de commandes', 'Tus nombres de comandos',
                'I tuoi nomi dei comandi', 'Os seus nomes de comandos'),
    'h_notes': ('Заметки', 'Notes', 'Notizen', 'Notes', 'Notas', 'Note', 'Notas'),
    'h_fixes': ('Исправления команд', 'Command corrections', 'Befehlskorrekturen', 'Corrections de commandes', 'Correcciones de comandos',
                'Correzioni dei comandi', 'Correções de comandos'),
    'forget_hint': ('Чтобы удалить: «забудь» и название. Всё сразу: «очисти сохранённую память».',
                    'To remove one: “forget” and its name. Everything: “clear saved memory”.',
                    'Einzeln löschen: „vergiss“ und Name. Alles: „gespeicherte Erinnerungen löschen“.',
                    'Pour en supprimer une : « oublie » et son nom. Tout : « efface la mémoire ».',
                    'Para borrar uno: «olvida» y su nombre. Todo: «borra la memoria guardada».',
                    'Per rimuoverne una: «dimentica» e il nome. Tutto: «cancella la memoria salvata».',
                    'Para apagar uma: «esquece» e o nome. Tudo: «apaga a memória guardada».'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    row = _TEXT[key]
    return row[_CODES.index(code) if code in _CODES else 1].format(**values)


def enabled(config: Any) -> bool:
    return bool(config.get('memory.enabled', True))


def _clean_key(key: str, *, cut: bool = True) -> str:
    value = ' '.join(str(key or '').strip(' .,:;!?—-–"«»').split())
    value = re.sub(r'^(?:что|that|dass|que|che)\s+', '', value, flags=re.I)
    return value[:KEY_MAX] if cut else value


def _user_rows(database: Any, limit: int = 500) -> list[dict[str, str]]:
    # NEO 184: internal records are left out before the limit (in SQL when the database can).
    if hasattr(database, 'list_user_memories'):
        try:
            return database.list_user_memories(_INTERNAL_PREFIXES, limit=limit)
        except Exception:
            pass
    rows = database.list_memories(limit=limit)
    return [r for r in rows if not str(r.get('key', '')).startswith(_INTERNAL_PREFIXES)]


def _preference_value(key: str, value: str, code: str) -> tuple[str, str, str]:
    name = _clean_key(key, cut=False)
    text = ' '.join(str(value or '').strip(' .!?').split())
    if not name or not text:
        raise ValueError('key and value are required')
    for item, limit in ((name, KEY_MAX), (text, VALUE_MAX)):
        if len(item) > limit:
            return name, text, t('too_long', code, length=len(item), limit=limit)
    return name, text, ''


def _note_value(text: str, code: str, *, preserve_layout: bool = False) -> tuple[str, str]:
    # The editor owns literal note text. Preserve its paragraphs, tabs and margins;
    # conversational note creation keeps its existing whitespace normalization.
    value = str(text or '') if preserve_layout else ' '.join(str(text or '').split())
    if not value.strip():
        raise ValueError('text is required')
    return value, (t('too_long', code, length=len(value), limit=NOTE_MAX) if len(value) > NOTE_MAX else '')


def remember(database: Any, config: Any, key: str, value: str, code: str) -> tuple[bool, str]:
    if not enabled(config):
        return False, t('is_off', code)
    name, text, error = _preference_value(key, value, code)
    if error:
        return False, error
    database.set_memory('pref:' + name.casefold(), json.dumps({'name': name, 'value': text}, ensure_ascii=False))
    _LAST_KEY['desktop'] = name
    _apply_known_preference(config, name, text)
    return True, t('saved', code, key=name, value=text)


def note(database: Any, config: Any, text: str, code: str) -> tuple[bool, str]:
    if not enabled(config):
        return False, t('is_off', code)
    value, error = _note_value(text, code)
    if error:
        return False, error
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
    database.set_memory('note:' + stamp, value)
    return True, t('note_saved', code)


def _decode(row: dict[str, str]) -> tuple[str, str, str]:
    """(kind, name, value) for a stored row."""
    key, value = str(row.get('key', '')), str(row.get('value', ''))
    if key.startswith('pref:'):
        try:
            data = json.loads(value)
            if not isinstance(data, dict):
                return 'pref', key[5:], value
            return 'pref', str(data.get('name') or key[5:]), str(data.get('value', ''))
        except (TypeError, ValueError):
            return 'pref', key[5:], value
    if key.startswith('note:'):
        return 'note', key, value
    return 'pref', key, value           # facts saved by older NEO versions


def user_fact_rows(database: Any) -> list[dict[str, str]]:
    """Explicit user preferences/notes for the editor, with exact storage identity.

    Untyped legacy rows cannot be distinguished safely from other application data.
    Leave them to the existing conversational memory commands; never expose or remove
    them through this editor. Do not use listing(), which includes learned commands.
    """
    rows = _user_rows(database, limit=100000)
    return [dict(row) for row in rows if _is_user_fact(row)]


def _is_user_fact(row: Any) -> bool:
    return (isinstance(row, dict) and isinstance(row.get('key'), str)
            and row['key'].startswith(('pref:', 'note:')) and len(row['key']) > 5
            and isinstance(row.get('value'), str) and isinstance(row.get('updated_at'), str))


def edit_user_fact(database: Any, config: Any, row: dict[str, str], value: str, code: str) -> tuple[bool, str]:
    """Edit the value of one reviewed user fact, keeping its exact key and name.

    Like remember/note, disabled memory refuses saves and length limits never truncate.
    This edits facts, not configuration: setting changes remain in Settings.
    """
    from jarvis.runtime.quality_memory_text184 import text
    if not enabled(config):
        return False, t('is_off', code)
    if not _is_user_fact(row):
        return False, text('stale', code)
    kind, name, _ = _decode(row)
    try:
        if kind == 'pref':
            _, prepared, error = _preference_value(name, value, code)
            prepared = json.dumps({'name': name, 'value': prepared}, ensure_ascii=False)
        else:
            prepared, error = _note_value(value, code, preserve_layout=True)
    except ValueError:
        return False, text('empty_value', code)
    if error:
        return False, error
    if not database.change_memory_snapshot([row], value=prepared):
        return False, text('stale', code)
    _LAST_KEY.pop('desktop', None)
    return True, text('saved_fact', code)


def delete_user_facts(database: Any, rows: list[dict[str, str]], code: str) -> tuple[bool, str]:
    """Delete only reviewed pref:/note: rows; aliases, corrections and reset scope stay separate.

    Deletion remains available when memory is off, like forget()/clear().
    """
    from jarvis.runtime.quality_memory_text184 import text
    if not rows or not all(_is_user_fact(row) for row in rows):
        return False, text('stale', code)
    if not database.change_memory_snapshot(rows):
        return False, text('stale', code)
    _LAST_KEY.pop('desktop', None)
    return True, text('deleted_fact', code)


def forget(database: Any, key: str, code: str) -> tuple[bool, str]:
    name = _clean_key(key)
    if name.casefold() in {'это', 'это предпочтение', 'это правило', 'this', 'that', 'this preference', 'das', 'ça', 'cela', 'eso', 'esto', 'questo', 'isso', 'isto'} or not name:
        name = _LAST_KEY.get('desktop', '')
        if not name:
            return False, t('nothing_last', code)
    for candidate in ('pref:' + name.casefold(), name):
        if database.delete_memory(candidate):
            _LAST_KEY.pop('desktop', None)
            return True, t('forgot', code, key=name)
    # Also match a stored preference by its displayed name.
    rows = _user_rows(database, limit=100000)
    for row in rows:
        kind, shown, _ = _decode(row)
        if kind == 'pref' and shown.casefold() == name.casefold() and database.delete_memory(row['key']):
            return True, t('forgot', code, key=shown)
    # NEO 183: one note can be deleted by its text (or its unique beginning)
    wanted = name.casefold()
    notes = [row for row in rows if _decode(row)[0] == 'note' and _decode(row)[2].casefold().startswith(wanted)]
    exact = [row for row in notes if _decode(row)[2].casefold() == wanted]
    pick = exact if len(exact) == 1 else notes
    if len(pick) == 1 and database.delete_memory(pick[0]['key']):
        return True, t('forgot', code, key=_decode(pick[0])[2][:80])
    return False, t('not_found', code, key=name)


def clear(database: Any, config: Any, code: str) -> str:
    count = 0
    # NEO 183: page through everything (one pass stopped after 500 records)
    for _ in range(10000):
        rows = _user_rows(database, limit=5000)
        if not rows:
            break
        deleted = sum(bool(database.delete_memory(row['key'])) for row in rows)
        count += deleted
        if not deleted:
            break
    for lang in _CODES:
        if database.get_memory('personal_aliases.' + lang, ''):
            database.delete_memory('personal_aliases.' + lang)
    path = corrections_path(config)
    if path is not None and path.exists():
        count += sum(1 for _ in path.open(encoding='utf-8'))
        path.unlink()
    _LAST_KEY.clear()
    return t('cleared', code, count=count)


def listing(database: Any, config: Any, code: str) -> str:
    prefs, notes = [], []
    for row in _user_rows(database):
        kind, name, value = _decode(row)
        (prefs if kind == 'pref' else notes).append((name, value))
    aliases = []
    try:
        data = json.loads(database.get_memory('personal_aliases.' + code, '{}') or '{}')
        aliases = [(r.get('phrase', k), r.get('command', ''), r.get('enabled', False)) for k, r in data.items()]
    except (TypeError, ValueError, AttributeError):
        pass
    fixes = load_corrections(config)
    if not (prefs or notes or aliases or fixes):
        return t('empty', code)
    lines = []
    if prefs:
        lines.append(t('h_prefs', code) + ':')
        lines += [f'• {name} — {value}' for name, value in prefs[:40]]
        _LAST_KEY['desktop'] = prefs[0][0]
    if aliases:
        lines.append(t('h_names', code) + ':')
        lines += [f"{'•' if on else '○'} «{phrase}» → {command}" for phrase, command, on in aliases[:40]]
    if notes:
        lines.append(t('h_notes', code) + ':')
        lines += [f'• {value[:120]}' for _, value in notes[:20]]
    if fixes:
        lines.append(t('h_fixes', code) + ':')
        lines += [f"• «{f['text']}» → {f['right']['tool']}.{f['right']['arguments'].get('action', '')}" for f in fixes[-10:]]
    if not enabled(config):
        lines.append(t('is_off', code))
    lines.append(t('forget_hint', code))
    return '\n'.join(lines)


def facts_for_prompt(database: Any, config: Any, limit: int = 30) -> list[str]:
    if not enabled(config):
        return []
    facts = []
    for row in _user_rows(database)[:limit]:
        kind, name, value = _decode(row)
        facts.append(f'{name}: {value}' if kind == 'pref' else f'note: {value}')
    return facts


# Known preferences also change the matching NEO setting (the user sees it in Settings).
_BROWSERS = {'firefox': 'firefox', 'файрфокс': 'firefox', 'chrome': 'chrome', 'хром': 'chrome', 'google chrome': 'chrome',
             'edge': 'edge', 'эдж': 'edge', 'opera': 'opera', 'опера': 'opera', 'opera gx': 'opera_gx', 'brave': 'brave',
             'vivaldi': 'vivaldi', 'yandex': 'yandex', 'яндекс': 'yandex', 'яндекс браузер': 'yandex'}


def _apply_known_preference(config: Any, name: str, value: str) -> None:
    if not re.search(r'браузер|browser|navigateur|navegador', name, re.I):
        return
    browser = _BROWSERS.get(value.casefold().strip())
    if not browser:
        return
    try:
        data = json.loads(json.dumps(config.data))
        data.setdefault('desktop', {})['preferred_browser'] = browser
        config.save(data)
    except Exception:
        pass


# -------------------------------------------------------------- corrections
def corrections_path(config: Any) -> Path | None:
    paths = getattr(config, 'paths', None)
    root = getattr(paths, 'root', None)
    return Path(root) / 'neo_model' / 'corrections.jsonl' if root else None


def _norm(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(text or '').casefold().replace('ё', 'е')).split())


def record_correction(config: Any, code: str, text: str, wrong: tuple[str, dict] | None, right: tuple[str, dict]) -> bool:
    """A confirmed correction: the phrase ``text`` should have meant ``right``."""
    if not enabled(config) or not text.strip() or not right or not right[0]:
        return False
    row = {'time': datetime.now(timezone.utc).isoformat(), 'language': code, 'text': ' '.join(text.split())[:300],
           'wrong': {'tool': wrong[0], 'arguments': wrong[1]} if wrong else None,
           'right': {'tool': right[0], 'arguments': dict(right[1])}, 'confirmed': True}
    path = corrections_path(config)
    if path is None:
        return False
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [r for r in load_corrections(config) if not (r['language'] == code and _norm(r['text']) == _norm(text))]
        rows.append(row)
        rows = rows[-2000:]
        temporary = path.with_suffix('.tmp')
        temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
        temporary.replace(path)
    return True


def load_corrections(config: Any) -> list[dict[str, Any]]:
    path = corrections_path(config)
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('right'), dict):
            rows.append(row)
    return rows


def correction_rule(config: Any, text: str, code: str) -> tuple[str, dict] | None:
    """Exact-phrase rule from a confirmed correction (works immediately, no retraining)."""
    if not enabled(config):
        return None
    key = _norm(text)
    for row in reversed(load_corrections(config)):
        if row.get('language') == code and _norm(row.get('text', '')) == key:
            return str(row['right']['tool']), dict(row['right'].get('arguments') or {})
    return None


_STOP184 = {'мой', 'моя', 'моё', 'мое', 'мои', 'my', 'mein', 'meine', 'mon', 'ma', 'mes', 'mi', 'mis', 'il', 'la', 'mio', 'mia', 'o', 'a',
            'meu', 'minha', 'это', 'is', 'ist', 'est', 'es', 'è', 'é', '—', '-', 'что', 'that', 'dass', 'que', 'che'}


def correct_note(database: Any, config: Any, text: str, code: str) -> tuple[bool, str]:
    """NEO 184: «Исправление: мой любимый цвет янтарный» replaces the stored fact about the same thing.

    Stored notes sharing the subject words (all but the last word) are removed, then the new
    fact is saved; nothing is removed when the subject is not found."""
    value = ' '.join(str(text or '').split()).strip(' .!')
    words = [w.casefold().strip('.,!?:;«»"') for w in value.split()]
    subject = [w for w in words[:-1] if w and w not in _STOP184]
    if not subject:
        return note(database, config, value, code)
    replaced = 0
    for row in _user_rows(database, limit=100000):
        kind, _name, stored = _decode(row)
        if kind != 'note':
            continue
        stored_words = {w.casefold().strip('.,!?:;«»"') for w in stored.split()}
        if all(word in stored_words for word in subject) and database.delete_memory(row['key']):
            replaced += 1
    ok, message = note(database, config, value, code)
    if ok and replaced:
        return True, t('note_corrected', code, text=value)
    return ok, message

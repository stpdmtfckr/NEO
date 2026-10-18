"""NEO 172: learning from use — only with the user's consent, measured, reported once a week.

* With «Собирать мои команды для проверки понимания» (Settings, off by default) NEO keeps
  the commands it *executed successfully* and the corrections the user confirmed:
  phrase, language, tool.action. Nothing else — no answers, no documents, no chat.
  At most ``LIMIT`` rows, stored locally; «удали собранные фразы» removes them.
* ``measure`` runs the NEO command model over that set: which share of phrases it maps
  to the right command. Every result is kept, so a report can say «было 91%».
* Once a week (on the first turn after 7 days) NEO posts a short report in the chat;
  «как ты меня понимаешь» shows it at any time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
from typing import Any, Callable

LIMIT = 1000
_LOCK = threading.Lock()
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


def _pick(row: tuple[str, ...], code: str) -> str:
    return row[_LANGS.index(_code(code))]


def enabled(config: Any) -> bool:
    return bool(config.get('learning.collect_phrases', False))


def _root(config: Any) -> Path | None:
    root = getattr(getattr(config, 'paths', None), 'root', None)
    return Path(root) / 'neo_model' / 'eval172' if root else None


def _norm(text: str) -> str:
    return ' '.join(re.sub(r'[^\w\s]', ' ', str(text or '').casefold().replace('ё', 'е')).split())


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    temporary.replace(path)


def record(config: Any, text: str, code: str, tool: str, action: str, *, source: str = 'executed') -> bool:
    """Keep one phrase → command pair (only when the user allowed it)."""
    if not enabled(config) or not tool or not str(text or '').strip():
        return False
    root = _root(config)
    if root is None:
        return False
    phrase = ' '.join(str(text).split())[:200]
    if len(phrase.split()) > 20:
        return False                     # long dictation or chat, not a command
    intent = f'{tool}.{action}' if action else tool
    row = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'language': _code(code),
           'text': phrase, 'intent': intent, 'source': source}
    with _LOCK:
        path = root / 'phrases.jsonl'
        rows = [r for r in _read(path) if not (r.get('language') == row['language'] and _norm(r.get('text', '')) == _norm(phrase))]
        rows.append(row)
        _write(path, rows[-LIMIT:])
    return True


def phrases(config: Any) -> list[dict[str, Any]]:
    root = _root(config)
    return _read(root / 'phrases.jsonl') if root else []


def clear(config: Any) -> int:
    root = _root(config)
    if root is None:
        return 0
    with _LOCK:
        count = len(_read(root / 'phrases.jsonl'))
        for name in ('phrases.jsonl', 'results.jsonl'):
            try:
                (root / name).unlink()
            except OSError:
                pass
    return count


def measure(config: Any, predict: Callable[[str, str], str] | None = None) -> dict[str, Any] | None:
    """Share of collected phrases the command model maps to the recorded command. Saved with a date."""
    rows = phrases(config)
    if len(rows) < 10:
        return None
    if predict is None:
        from jarvis.neo_model import runtime
        model = runtime.model(config)
        if model is None:
            return None

        def predict(text: str, code: str) -> str:
            ranked = model.ranked(text, code, '', top=1)
            return ranked[0][0] if ranked else ''
    right = sum(1 for r in rows if predict(r['text'], r['language']) == r['intent'])
    result = {'time': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'total': len(rows), 'right': right,
              'accuracy': round(100.0 * right / len(rows), 1)}
    root = _root(config)
    if root is not None:
        with _LOCK:
            history = _read(root / 'results.jsonl')
            history.append(result)
            _write(root / 'results.jsonl', history[-60:])
    return result


def history(config: Any) -> list[dict[str, Any]]:
    root = _root(config)
    return _read(root / 'results.jsonl') if root else []


def corrections_this_week(config: Any, now: datetime | None = None) -> int:
    from jarvis.runtime import memory169
    since = (now or datetime.now(timezone.utc)) - timedelta(days=7)
    count = 0
    for row in memory169.load_corrections(config):
        try:
            if datetime.fromisoformat(str(row.get('time'))) >= since:
                count += 1
        except ValueError:
            continue
    return count


_TEXT = {
    'report': ('Как я вас понимаю: {now}% команд из {total} собранных фраз узнаю правильно{before}. Исправлений за неделю: {fixes}.',
               'How well I understand you: {now}% of {total} collected phrases map to the right command{before}. Corrections this week: {fixes}.',
               'So gut verstehe ich dich: {now}% von {total} gesammelten Sätzen erkenne ich richtig{before}. Korrekturen diese Woche: {fixes}.',
               'Ma compréhension : {now} % des {total} phrases collectées sont bien reconnues{before}. Corrections cette semaine : {fixes}.',
               'Cómo te entiendo: reconozco bien el {now}% de {total} frases recogidas{before}. Correcciones esta semana: {fixes}.',
               'Quanto ti capisco: riconosco correttamente il {now}% di {total} frasi raccolte{before}. Correzioni questa settimana: {fixes}.',
               'Como te percebo: reconheço bem {now}% de {total} frases recolhidas{before}. Correções esta semana: {fixes}.'),
    'before': (' (было {was}%)', ' (was {was}%)', ' (vorher {was}%)', ' (avant {was} %)', ' (antes {was}%)', ' (prima {was}%)', ' (antes {was}%)'),
    'off': ('Сбор фраз для проверки выключен. Включить: Настройки → Окно → «Локальный ИИ» → «Собирать мои команды для проверки понимания». '
            'Сохраняются только выполненные команды и ваши исправления, на этом компьютере.',
            'Phrase collection is off. Turn it on in Settings → Window → “Local AI” → “Collect my commands to measure understanding”. '
            'Only executed commands and your corrections are kept, on this computer.',
            'Das Sammeln ist aus. Einschalten: Einstellungen → Fenster → „Lokale KI“ → „Meine Befehle zum Messen sammeln“. '
            'Gespeichert werden nur ausgeführte Befehle und deine Korrekturen, auf diesem PC.',
            'La collecte est désactivée. Activez-la dans Paramètres → Fenêtre → « IA locale » → « Collecter mes commandes pour mesurer la compréhension ». '
            'Seules les commandes exécutées et vos corrections sont gardées, sur cet ordinateur.',
            'La recogida está desactivada. Actívala en Ajustes → Ventana → «IA local» → «Recoger mis órdenes para medir la comprensión». '
            'Solo se guardan las órdenes ejecutadas y tus correcciones, en este equipo.',
            'La raccolta è disattivata. Attivala in Impostazioni → Finestra → «IA locale» → «Raccogli i miei comandi per misurare la comprensione». '
            'Si salvano solo i comandi eseguiti e le tue correzioni, su questo computer.',
            'A recolha está desligada. Liga em Definições → Janela → «IA local» → «Recolher os meus comandos para medir a compreensão». '
            'Só ficam os comandos executados e as tuas correções, neste computador.'),
    'few': ('Пока собрано {n} фраз; для оценки нужно хотя бы 10.', 'Only {n} phrases collected so far; at least 10 are needed.',
            'Bisher {n} Sätze gesammelt; mindestens 10 sind nötig.', 'Seulement {n} phrases collectées ; il en faut au moins 10.',
            'De momento hay {n} frases; se necesitan al menos 10.', 'Finora {n} frasi raccolte; ne servono almeno 10.',
            'Por agora há {n} frases; são precisas pelo menos 10.'),
    'no_model': ('Модель команд NEO сейчас не загружена, оценить нельзя.', 'The NEO command model isn’t loaded, so I can’t measure.',
                 'Das NEO-Befehlsmodell ist nicht geladen, messen geht nicht.', 'Le modèle de commandes NEO n’est pas chargé, impossible de mesurer.',
                 'El modelo de órdenes de NEO no está cargado; no puedo medir.', 'Il modello dei comandi NEO non è caricato, non posso misurare.',
                 'O modelo de comandos do NEO não está carregado; não consigo medir.'),
    'cleared': ('Удалил собранные фразы ({n}).', 'Deleted the collected phrases ({n}).', 'Gesammelte Sätze gelöscht ({n}).',
                'Phrases collectées supprimées ({n}).', 'Frases recogidas eliminadas ({n}).', 'Frasi raccolte eliminate ({n}).',
                'Frases recolhidas apagadas ({n}).'),
}


def report_text(config: Any, code: str, *, predict: Callable[[str, str], str] | None = None) -> str:
    if not enabled(config):
        return _pick(_TEXT['off'], code)
    rows = phrases(config)
    if len(rows) < 10:
        return _pick(_TEXT['few'], code).format(n=len(rows))
    previous = history(config)
    result = measure(config, predict)
    if result is None:
        return _pick(_TEXT['no_model'], code)
    before = _pick(_TEXT['before'], code).format(was=_num(previous[-1]['accuracy'], code)) if previous else ''
    return _pick(_TEXT['report'], code).format(now=_num(result['accuracy'], code), total=result['total'], before=before,
                                               fixes=corrections_this_week(config))


def _num(value: float, code: str) -> str:
    text = f'{value:.1f}'.rstrip('0').rstrip('.')
    return text.replace('.', ',') if _code(code) != 'en' else text


def weekly_due(config: Any, now: datetime | None = None) -> bool:
    """True once a week, when collection is on and there is something to measure."""
    if not enabled(config) or len(phrases(config)) < 10:
        return False
    rows = history(config)
    if not rows:
        return True
    try:
        last = datetime.fromisoformat(str(rows[-1]['time']))
    except (KeyError, ValueError):
        return True
    return (now or datetime.now(timezone.utc)) - last >= timedelta(days=7)


def cleared_text(n: int, code: str) -> str:
    return _pick(_TEXT['cleared'], code).format(n=n)


__all__ = ['LIMIT', 'clear', 'cleared_text', 'corrections_this_week', 'enabled', 'history', 'measure', 'phrases', 'record',
           'report_text', 'weekly_due']

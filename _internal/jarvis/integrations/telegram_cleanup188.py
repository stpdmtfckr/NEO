"""NEO 188 NEO-TG-001 / TZ-TELEGRAM-CONTROL-HISTORY-CLEANUP-01.

One honest pass over the KNOWN message IDs of the paired private control chat:
incoming user commands/media and NEO's own replies/media/panels. Every ID gets
exactly one outcome:

* ``deleted``     – Telegram confirmed the deletion;
* ``absent``      – Telegram reliably says the message no longer exists;
* ``not_allowed`` – Telegram refuses this deletion (rights / service message);
* ``expired``     – older than the Bot API deletion window (48 h, dice 24 h):
                    it can only be removed by hand in Telegram;
* ``pending``     – a temporary failure (network, 5xx, 429 retry_after): the
                    ID stays in the ledger for the next pass.

Unknown IDs are never invented. A pass never reports «all history removed»
when something is pending, refused, expired or the ledger has a known gap.
No message text or token is stored or logged here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

ORDINARY_WINDOW = 47.8 * 3600      # Bot API: messages younger than 48 hours
DICE_WINDOW = 23.8 * 3600          # private-chat dice: 24 hours
BATCH = 100                        # deleteMessages accepts up to 100 IDs


@dataclass
class CleanupResult:
    deleted: set[int] = field(default_factory=set)
    absent: set[int] = field(default_factory=set)
    not_allowed: set[int] = field(default_factory=set)
    expired: set[int] = field(default_factory=set)
    pending: set[int] = field(default_factory=set)
    retry_after: float = 0.0
    skipped_busy: bool = False

    def finished_ids(self) -> set[int]:
        """IDs that leave the ledger: nothing more can or needs to be done with them."""
        return self.deleted | self.absent | self.not_allowed | self.expired

    def counts(self) -> dict[str, int]:
        return {key: len(getattr(self, key)) for key in ('deleted', 'absent', 'not_allowed', 'expired', 'pending')}


def window(item: dict[str, Any]) -> float:
    return DICE_WINDOW if str(item.get('kind', '') or '') == 'dice' else ORDINARY_WINDOW


def classify_error(text: str) -> tuple[str, float]:
    """('absent'|'not_allowed'|'retry'|'temporary', retry_after seconds)."""
    value = str(text or '').casefold()
    if 'message to delete not found' in value or 'message_id_invalid' in value or 'message not found' in value:
        return 'absent', 0.0
    if ("can't be deleted" in value or 'cant be deleted' in value or 'message_delete_forbidden' in value
            or 'not enough rights' in value or 'have no rights' in value):
        return 'not_allowed', 0.0
    found = re.search(r'retry after (\d+)', value)
    if found or 'too many requests' in value or '429' in value:
        return 'retry', float(found.group(1)) if found else 5.0
    return 'temporary', 0.0


def run(entries: Iterable[dict[str, Any]], now: float,
        delete_many: Callable[[list[int]], Any], delete_one: Callable[[int], Any], *,
        max_single: int = 40, stop: Callable[[], bool] = lambda: False, quick: bool = False) -> CleanupResult:
    """Delete the given ledger entries of ONE chat. Never touches anything else."""
    result = CleanupResult()
    live: list[int] = []
    for item in entries:
        mid = int(item.get('message_id', 0) or 0)
        if mid <= 0:
            continue
        age = now - float(item.get('ts', now) or now)
        if age >= window(item):
            result.expired.add(mid)
        else:
            live.append(mid)
    ids = sorted(set(live))
    singles = 0
    for offset in range(0, len(ids), BATCH):
        batch = ids[offset:offset + BATCH]
        if stop() and not quick:
            result.pending.update(batch)
            continue
        if result.retry_after:
            result.pending.update(batch)
            continue
        try:
            delete_many(batch)
            # deleteMessages skips IDs it cannot find; success means none is left.
            result.deleted.update(batch)
            continue
        except Exception as exc:  # noqa: BLE001 - every failure is classified below
            kind, retry = classify_error(str(exc))
        if kind == 'retry':
            result.retry_after = max(1.0, retry)
            result.pending.update(batch)
            continue
        if quick:
            # Shutdown is bounded: keep the IDs for the next startup instead of
            # turning exit into minutes of per-message retries.
            result.pending.update(batch)
            continue
        for mid in batch:
            if singles >= max_single or result.retry_after:
                result.pending.add(mid)
                continue
            singles += 1
            try:
                delete_one(mid)
                result.deleted.add(mid)
            except Exception as exc:  # noqa: BLE001
                kind, retry = classify_error(str(exc))
                if kind == 'absent':
                    result.absent.add(mid)
                elif kind == 'not_allowed':
                    result.not_allowed.add(mid)
                elif kind == 'retry':
                    result.retry_after = max(1.0, retry)
                    result.pending.add(mid)
                else:
                    result.pending.add(mid)
    return result


def status(result: CleanupResult | dict[str, Any], *, coverage_gap: bool) -> str:
    counts = result.counts() if isinstance(result, CleanupResult) else dict(result)
    if counts.get('pending'):
        return 'partial'
    if coverage_gap:
        return 'unknown'
    if counts.get('not_allowed') or counts.get('expired'):
        return 'partial'
    return 'complete'


_TEXT = {
    'complete': ('Очистка Telegram: все известные сообщения удалены ({deleted}).',
                 'Telegram cleanup: all known messages were deleted ({deleted}).',
                 'Telegram-Bereinigung: alle bekannten Nachrichten gelöscht ({deleted}).',
                 'Nettoyage Telegram : tous les messages connus ont été supprimés ({deleted}).',
                 'Limpieza de Telegram: se eliminaron todos los mensajes conocidos ({deleted}).',
                 'Pulizia Telegram: eliminati tutti i messaggi noti ({deleted}).',
                 'Limpeza do Telegram: todas as mensagens conhecidas foram apagadas ({deleted}).'),
    'partial': ('Очистка Telegram выполнена частично: удалено {deleted}, ждут повтора {pending}, Telegram запретил {not_allowed}, старше окна удаления {expired}.',
                'Telegram cleanup was partial: {deleted} deleted, {pending} waiting for a retry, {not_allowed} refused by Telegram, {expired} older than the deletion window.',
                'Telegram-Bereinigung teilweise: {deleted} gelöscht, {pending} warten auf Wiederholung, {not_allowed} von Telegram abgelehnt, {expired} älter als das Löschfenster.',
                'Nettoyage Telegram partiel : {deleted} supprimés, {pending} en attente, {not_allowed} refusés par Telegram, {expired} hors délai de suppression.',
                'Limpieza de Telegram parcial: {deleted} eliminados, {pending} pendientes de reintento, {not_allowed} rechazados por Telegram, {expired} fuera del plazo de borrado.',
                'Pulizia Telegram parziale: {deleted} eliminati, {pending} in attesa di nuovo tentativo, {not_allowed} rifiutati da Telegram, {expired} oltre il limite di eliminazione.',
                'Limpeza do Telegram parcial: {deleted} apagadas, {pending} a aguardar nova tentativa, {not_allowed} recusadas pelo Telegram, {expired} fora do prazo de eliminação.'),
    'unknown': ('Очистка Telegram: известные сообщения удалены ({deleted}), но часть сообщений могла остаться без известного ID (например, после сбоя сети или обновления NEO).',
                'Telegram cleanup: known messages were deleted ({deleted}), but some messages may have no known ID (for example after a network failure or a NEO update).',
                'Telegram-Bereinigung: bekannte Nachrichten gelöscht ({deleted}), einige Nachrichten haben aber evtl. keine bekannte ID (z. B. nach einem Netzwerkfehler oder Update).',
                'Nettoyage Telegram : messages connus supprimés ({deleted}), mais certains messages peuvent ne pas avoir d’ID connu (par ex. après une panne réseau ou une mise à jour).',
                'Limpieza de Telegram: se eliminaron los mensajes conocidos ({deleted}), pero puede que algunos no tengan un ID conocido (por ejemplo, tras un fallo de red o una actualización).',
                'Pulizia Telegram: eliminati i messaggi noti ({deleted}), ma alcuni messaggi potrebbero non avere un ID noto (ad es. dopo un errore di rete o un aggiornamento).',
                'Limpeza do Telegram: mensagens conhecidas apagadas ({deleted}), mas algumas podem não ter um ID conhecido (por exemplo, após uma falha de rede ou atualização).'),
    'manual': (' Сообщения старше 48 часов и без известных ID бот удалить не может — их можно удалить вручную в Telegram.',
               ' The bot cannot delete messages older than 48 hours or without a known ID — delete them by hand in Telegram.',
               ' Nachrichten, die älter als 48 Stunden sind oder keine bekannte ID haben, kann der Bot nicht löschen – bitte manuell in Telegram löschen.',
               ' Le bot ne peut pas supprimer les messages de plus de 48 heures ou sans ID connu : supprimez-les à la main dans Telegram.',
               ' El bot no puede borrar mensajes de más de 48 horas ni sin ID conocido: bórralos a mano en Telegram.',
               ' Il bot non può eliminare messaggi più vecchi di 48 ore o senza ID noto: eliminali a mano in Telegram.',
               ' O bot não pode apagar mensagens com mais de 48 horas ou sem ID conhecido: apaga-as manualmente no Telegram.'),
    'never': ('Очистка Telegram ещё не выполнялась.', 'Telegram cleanup has not run yet.', 'Die Telegram-Bereinigung lief noch nicht.',
              'Le nettoyage Telegram n’a pas encore été effectué.', 'La limpieza de Telegram aún no se ha ejecutado.',
              'La pulizia Telegram non è ancora stata eseguita.', 'A limpeza do Telegram ainda não foi executada.'),
}
_ORDER = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')


def summary(report: dict[str, Any] | None, code: str) -> str:
    """A short localized result for Settings and the log (no IDs, no text)."""
    index = _ORDER.index(code) if code in _ORDER else 1
    if not isinstance(report, dict) or not report.get('status'):
        return _TEXT['never'][index]
    counts = {key: int(report.get(key, 0) or 0) for key in ('deleted', 'absent', 'not_allowed', 'expired', 'pending')}
    state = str(report.get('status'))
    text = _TEXT.get(state, _TEXT['partial'])[index].format(**counts)
    if state != 'complete' and (counts['expired'] or counts['not_allowed'] or state == 'unknown'):
        text += _TEXT['manual'][index]
    return text

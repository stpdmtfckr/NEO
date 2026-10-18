# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

"""Headless-safe localization for deterministic JARVIS runtime messages.

This module intentionally imports no Qt/UI package. Desktop, Telegram and future
transports can therefore apply the same selected-language output gate without
loading another UI stack or another Phrase Atlas language.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

SUPPORTED = frozenset({"ru", "en", "de", "fr", "es", "it", "pt"})

# Unknown internal text must never be flattened to the old misleading
# “Action completed.” catch-all. That made unrelated results (including startup
# briefings) look identical. The fallback below is explicit about localization
# rather than inventing a success state.
_FALLBACK = {
    "en": ("The tool reported an error.", "The tool returned a result that is not localized yet."),
    "de": ("Das Tool hat einen Fehler gemeldet.", "Das Tool hat ein Ergebnis geliefert, das noch nicht lokalisiert ist."),
    "fr": ("L’outil a signalé une erreur.", "L’outil a renvoyé un résultat qui n’est pas encore localisé."),
    "es": ("La herramienta informó de un error.", "La herramienta devolvió un resultado que aún no está localizado."),
    "it": ("Lo strumento ha segnalato un errore.", "Lo strumento ha restituito un risultato non ancora localizzato."),
    "pt": ("A ferramenta relatou um erro.", "A ferramenta retornou um resultado que ainda não está localizado."),
}

_UI_CATALOG_DIR = Path(__file__).resolve().parents[1] / "locales" / "ui"

@lru_cache(maxsize=6)
def _ui_catalog(lang: str) -> dict[str, str]:
    try:
        data = json.loads((_UI_CATALOG_DIR / f"{lang}.json").read_text(encoding="utf-8"))
        rows = data.get("strings", {}) if isinstance(data, dict) else {}
        return {str(k): str(v) for k, v in rows.items()} if isinstance(rows, dict) else {}
    except Exception:
        return {}

_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (
        (r"^Не удалось открыть «([^»]+)»[.!]?$", "Could not open «{0}»."),
        (r"^Не удалось запустить «([^»]+)»[.!]?$", "Could not start «{0}»."),
        (r"^Не удалось закрыть «([^»]+)»[.!]?$", "Could not close «{0}»."),
        (r"^Не удалось найти «([^»]+)»[.!]?$", "Could not find «{0}»."),
        (r"^Включил аудиодорожку YouTube в Discord:\s*(.+)\.$", "Playing YouTube audio in Discord: {0}."),
        (r"^Добавил YouTube-видео в общую очередь Discord:\s*(.+)\.$", "Added YouTube video to the shared Discord queue: {0}."),
        (r"^Ошибка:\s*(.+)$", "Error: {0}"),
        (r"^Открыл(?: приложение)?:?\s*(.+)$", "Opened: {0}"),
        (r"^Запустил(?: приложение)?:?\s*(.+)$", "Started: {0}"),
        (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Closed: {0}"),
        (r"^Переключил трек:?\s*(.+)$", "Switched track: {0}"),
        (r"^Найдено:\s*(\d+)$", "Found: {0}"),
        (r"^Громкость:\s*(.+)$", "Volume: {0}"),
    ),
    "de": (
        (r"^Не удалось открыть «([^»]+)»[.!]?$", "«{0}» konnte nicht geöffnet werden."),
        (r"^Не удалось запустить «([^»]+)»[.!]?$", "«{0}» konnte nicht gestartet werden."),
        (r"^Не удалось закрыть «([^»]+)»[.!]?$", "«{0}» konnte nicht geschlossen werden."),
        (r"^Не удалось найти «([^»]+)»[.!]?$", "«{0}» wurde nicht gefunden."),
        (r"^Включил аудиодорожку YouTube в Discord:\s*(.+)\.$", "YouTube-Audio wird in Discord abgespielt: {0}."),
        (r"^Добавил YouTube-видео в общую очередь Discord:\s*(.+)\.$", "YouTube-Video zur gemeinsamen Discord-Warteschlange hinzugefügt: {0}."),
        (r"^Ошибка:\s*(.+)$", "Fehler: {0}"),
        (r"^Открыл(?: приложение)?:?\s*(.+)$", "Geöffnet: {0}"),
        (r"^Запустил(?: приложение)?:?\s*(.+)$", "Gestartet: {0}"),
        (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Geschlossen: {0}"),
        (r"^Переключил трек:?\s*(.+)$", "Titel gewechselt: {0}"),
        (r"^Найдено:\s*(\d+)$", "Gefunden: {0}"),
        (r"^Громкость:\s*(.+)$", "Lautstärke: {0}"),
    ),
    "fr": (
        (r"^Не удалось открыть «([^»]+)»[.!]?$", "Impossible d’ouvrir «{0}»."),
        (r"^Не удалось запустить «([^»]+)»[.!]?$", "Impossible de lancer «{0}»."),
        (r"^Не удалось закрыть «([^»]+)»[.!]?$", "Impossible de fermer «{0}»."),
        (r"^Не удалось найти «([^»]+)»[.!]?$", "Impossible de trouver «{0}»."),
        (r"^Включил аудиодорожку YouTube в Discord:\s*(.+)\.$", "Lecture de l’audio YouTube dans Discord : {0}."),
        (r"^Добавил YouTube-видео в общую очередь Discord:\s*(.+)\.$", "Vidéo YouTube ajoutée à la file Discord partagée : {0}."),
        (r"^Ошибка:\s*(.+)$", "Erreur : {0}"),
        (r"^Открыл(?: приложение)?:?\s*(.+)$", "Ouvert : {0}"),
        (r"^Запустил(?: приложение)?:?\s*(.+)$", "Lancé : {0}"),
        (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Fermé : {0}"),
        (r"^Переключил трек:?\s*(.+)$", "Piste changée : {0}"),
        (r"^Найдено:\s*(\d+)$", "Trouvé : {0}"),
        (r"^Громкость:\s*(.+)$", "Volume : {0}"),
    ),
    "es": (
        (r"^Не удалось открыть «([^»]+)»[.!]?$", "No se pudo abrir «{0}»."),
        (r"^Не удалось запустить «([^»]+)»[.!]?$", "No se pudo iniciar «{0}»."),
        (r"^Не удалось закрыть «([^»]+)»[.!]?$", "No se pudo cerrar «{0}»."),
        (r"^Не удалось найти «([^»]+)»[.!]?$", "No se pudo encontrar «{0}»."),
        (r"^Включил аудиодорожку YouTube в Discord:\s*(.+)\.$", "Reproduciendo audio de YouTube en Discord: {0}."),
        (r"^Добавил YouTube-видео в общую очередь Discord:\s*(.+)\.$", "Video de YouTube añadido a la cola compartida de Discord: {0}."),
        (r"^Ошибка:\s*(.+)$", "Error: {0}"),
        (r"^Открыл(?: приложение)?:?\s*(.+)$", "Abierto: {0}"),
        (r"^Запустил(?: приложение)?:?\s*(.+)$", "Iniciado: {0}"),
        (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Cerrado: {0}"),
        (r"^Переключил трек:?\s*(.+)$", "Pista cambiada: {0}"),
        (r"^Найдено:\s*(\d+)$", "Encontrado: {0}"),
        (r"^Громкость:\s*(.+)$", "Volumen: {0}"),
    ),
    "it": (
        (r"^Не удалось открыть «([^»]+)»[.!]?$", "Impossibile aprire «{0}»."),
        (r"^Не удалось запустить «([^»]+)»[.!]?$", "Impossibile avviare «{0}»."),
        (r"^Не удалось закрыть «([^»]+)»[.!]?$", "Impossibile chiudere «{0}»."),
        (r"^Не удалось найти «([^»]+)»[.!]?$", "Impossibile trovare «{0}»."),
        (r"^Включил аудиодорожку YouTube в Discord:\s*(.+)\.$", "Riproduzione audio YouTube in Discord: {0}."),
        (r"^Добавил YouTube-видео в общую очередь Discord:\s*(.+)\.$", "Video YouTube aggiunto alla coda Discord condivisa: {0}."),
        (r"^Ошибка:\s*(.+)$", "Errore: {0}"),
        (r"^Открыл(?: приложение)?:?\s*(.+)$", "Aperto: {0}"),
        (r"^Запустил(?: приложение)?:?\s*(.+)$", "Avviato: {0}"),
        (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Chiuso: {0}"),
        (r"^Переключил трек:?\s*(.+)$", "Traccia cambiata: {0}"),
        (r"^Найдено:\s*(\d+)$", "Trovato: {0}"),
        (r"^Громкость:\s*(.+)$", "Volume: {0}"),
    ),
    "pt": (
        (r"^Не удалось открыть «([^»]+)»[.!]?$", "Não foi possível abrir «{0}»."),
        (r"^Не удалось запустить «([^»]+)»[.!]?$", "Não foi possível iniciar «{0}»."),
        (r"^Не удалось закрыть «([^»]+)»[.!]?$", "Não foi possível fechar «{0}»."),
        (r"^Не удалось найти «([^»]+)»[.!]?$", "Não foi possível encontrar «{0}»."),
        (r"^Включил аудиодорожку YouTube в Discord:\s*(.+)\.$", "Reproduzindo áudio do YouTube no Discord: {0}."),
        (r"^Добавил YouTube-видео в общую очередь Discord:\s*(.+)\.$", "Vídeo do YouTube adicionado à fila compartilhada do Discord: {0}."),
        (r"^Ошибка:\s*(.+)$", "Erro: {0}"),
        (r"^Открыл(?: приложение)?:?\s*(.+)$", "Aberto: {0}"),
        (r"^Запустил(?: приложение)?:?\s*(.+)$", "Iniciado: {0}"),
        (r"^Закрыл(?: приложение| окно)?:?\s*(.+)$", "Fechado: {0}"),
        (r"^Переключил трек:?\s*(.+)$", "Faixa alterada: {0}"),
        (r"^Найдено:\s*(\d+)$", "Encontrado: {0}"),
        (r"^Громкость:\s*(.+)$", "Volume: {0}"),
    ),
}


# HOTFIX119: structured mailbox/calendar responses keep data readable while
# localizing the structural labels independently on every supported UI locale.
_STRUCTURED_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    "en": (
        (r"^Google Calendar подключён\.$", "Google Calendar connected."),
        (r"^Событий нет\.$", "No events."),
        (r"^Расписание$", "Schedule"),
        (r"^Расписание на сегодня$", "Today’s schedule"),
        (r"^Расписание на завтра$", "Tomorrow’s schedule"),
        (r"^Ближайшие события$", "Upcoming events"),
        (r"^Всё ближайшее расписание$", "Upcoming schedule"),
        (r"^Следующее событие$", "Next event"),
        (r"^Расписание на (\d+) дн\.$", "Schedule for {0} days"),
        (r"^\s*Когда:\s*(.+)$", "   When: {0}"),
        (r"^\s*Где:\s*(.+)$", "   Where: {0}"),
        (r"^\s*Описание:\s*(.+)$", "   Description: {0}"),
        (r"^Всего писем:\s*(\d+)$", "Total messages: {0}"),
        (r"^Непрочитанные:\s*(\d+)$", "Unread: {0}"),
        (r"^За последние 24 часа:\s*(\d+)$", "Last 24 hours: {0}"),
        (r"^Сегодня:\s*(\d+)$", "Today: {0}"),
        (r"^Папки:$", "Folders:"),
        (r"^\s+Непрочитанные:\s*(\d+)$", '  Unread: {0}'),
        (r"^\s+Сегодня:\s*(\d+)$", '  Today: {0}'),
        (r"^\s*Всего:\s*(\d+)$", "  Total: {0}"),
        (r"^\s*За 24 часа:\s*(\d+)$", "  Last 24 hours: {0}"),
        (r"^• (.+): всего — (\d+)$", "• {0}: total — {1}"),
        (r"^• (.+): непрочитанные — (\d+)$", "• {0}: unread — {1}"),
        (r"^• (.+): за 24 часа — (\d+)$", "• {0}: last 24 hours — {1}"),
        (r"^• (.+): сегодня — (\d+)$", "• {0}: today — {1}"),
    ),
    "de": (
        (r"^Google Calendar подключён\.$", "Google Calendar verbunden."),
        (r"^Событий нет\.$", "Keine Termine."),
        (r"^Расписание$", "Zeitplan"),
        (r"^Расписание на сегодня$", "Heutiger Zeitplan"),
        (r"^Расписание на завтра$", "Zeitplan für morgen"),
        (r"^Ближайшие события$", "Bevorstehende Termine"),
        (r"^Всё ближайшее расписание$", "Bevorstehender Zeitplan"),
        (r"^Следующее событие$", "Nächster Termin"),
        (r"^Расписание на (\d+) дн\.$", "Zeitplan für {0} Tage"),
        (r"^\s*Когда:\s*(.+)$", "   Wann: {0}"),
        (r"^\s*Где:\s*(.+)$", "   Wo: {0}"),
        (r"^\s*Описание:\s*(.+)$", "   Beschreibung: {0}"),
        (r"^Всего писем:\s*(\d+)$", "Nachrichten gesamt: {0}"),
        (r"^Непрочитанные:\s*(\d+)$", "Ungelesen: {0}"),
        (r"^За последние 24 часа:\s*(\d+)$", "Letzte 24 Stunden: {0}"),
        (r"^Сегодня:\s*(\d+)$", "Heute: {0}"),
        (r"^Папки:$", "Ordner:"),
        (r"^\s+Непрочитанные:\s*(\d+)$", '  Ungelesen: {0}'),
        (r"^\s+Сегодня:\s*(\d+)$", '  Heute: {0}'),
        (r"^\s*Всего:\s*(\d+)$", "  Gesamt: {0}"),
        (r"^\s*За 24 часа:\s*(\d+)$", "  Letzte 24 Stunden: {0}"),
        (r"^• (.+): всего — (\d+)$", "• {0}: gesamt — {1}"),
        (r"^• (.+): непрочитанные — (\d+)$", "• {0}: ungelesen — {1}"),
        (r"^• (.+): за 24 часа — (\d+)$", "• {0}: letzte 24 Std. — {1}"),
        (r"^• (.+): сегодня — (\d+)$", "• {0}: heute — {1}"),
    ),
    "fr": (
        (r"^Google Calendar подключён\.$", "Google Calendar connecté."),
        (r"^Событий нет\.$", "Aucun événement."),
        (r"^Расписание$", "Planning"),
        (r"^Расписание на сегодня$", "Planning d’aujourd’hui"),
        (r"^Расписание на завтра$", "Planning de demain"),
        (r"^Ближайшие события$", "Événements à venir"),
        (r"^Всё ближайшее расписание$", "Planning à venir"),
        (r"^Следующее событие$", "Prochain événement"),
        (r"^Расписание на (\d+) дн\.$", "Planning pour {0} jours"),
        (r"^\s*Когда:\s*(.+)$", "   Quand : {0}"),
        (r"^\s*Где:\s*(.+)$", "   Où : {0}"),
        (r"^\s*Описание:\s*(.+)$", "   Description : {0}"),
        (r"^Всего писем:\s*(\d+)$", "Messages au total : {0}"),
        (r"^Непрочитанные:\s*(\d+)$", "Non lus : {0}"),
        (r"^За последние 24 часа:\s*(\d+)$", "Dernières 24 h : {0}"),
        (r"^Сегодня:\s*(\d+)$", "Aujourd’hui : {0}"),
        (r"^Папки:$", "Dossiers :"),
        (r"^\s+Непрочитанные:\s*(\d+)$", '  Non lus : {0}'),
        (r"^\s+Сегодня:\s*(\d+)$", '  Aujourd’hui : {0}'),
        (r"^\s*Всего:\s*(\d+)$", "  Total : {0}"),
        (r"^\s*За 24 часа:\s*(\d+)$", "  Dernières 24 h : {0}"),
        (r"^• (.+): всего — (\d+)$", "• {0} : total — {1}"),
        (r"^• (.+): непрочитанные — (\d+)$", "• {0} : non lus — {1}"),
        (r"^• (.+): за 24 часа — (\d+)$", "• {0} : dernières 24 h — {1}"),
        (r"^• (.+): сегодня — (\d+)$", "• {0} : aujourd’hui — {1}"),
    ),
    "es": (
        (r"^Google Calendar подключён\.$", "Google Calendar conectado."),
        (r"^Событий нет\.$", "No hay eventos."),
        (r"^Расписание$", "Agenda"),
        (r"^Расписание на сегодня$", "Agenda de hoy"),
        (r"^Расписание на завтра$", "Agenda de mañana"),
        (r"^Ближайшие события$", "Próximos eventos"),
        (r"^Всё ближайшее расписание$", "Próxima agenda"),
        (r"^Следующее событие$", "Siguiente evento"),
        (r"^Расписание на (\d+) дн\.$", "Agenda para {0} días"),
        (r"^\s*Когда:\s*(.+)$", "   Cuándo: {0}"),
        (r"^\s*Где:\s*(.+)$", "   Dónde: {0}"),
        (r"^\s*Описание:\s*(.+)$", "   Descripción: {0}"),
        (r"^Всего писем:\s*(\d+)$", "Mensajes totales: {0}"),
        (r"^Непрочитанные:\s*(\d+)$", "No leídos: {0}"),
        (r"^За последние 24 часа:\s*(\d+)$", "Últimas 24 horas: {0}"),
        (r"^Сегодня:\s*(\d+)$", "Hoy: {0}"),
        (r"^Папки:$", "Carpetas:"),
        (r"^\s+Непрочитанные:\s*(\d+)$", '  No leídos: {0}'),
        (r"^\s+Сегодня:\s*(\d+)$", '  Hoy: {0}'),
        (r"^\s*Всего:\s*(\d+)$", "  Total: {0}"),
        (r"^\s*За 24 часа:\s*(\d+)$", "  Últimas 24 horas: {0}"),
        (r"^• (.+): всего — (\d+)$", "• {0}: total — {1}"),
        (r"^• (.+): непрочитанные — (\d+)$", "• {0}: no leídos — {1}"),
        (r"^• (.+): за 24 часа — (\d+)$", "• {0}: últimas 24 h — {1}"),
        (r"^• (.+): сегодня — (\d+)$", "• {0}: hoy — {1}"),
    ),
    "it": (
        (r"^Google Calendar подключён\.$", "Google Calendar connesso."),
        (r"^Событий нет\.$", "Nessun evento."),
        (r"^Расписание$", "Programma"),
        (r"^Расписание на сегодня$", "Programma di oggi"),
        (r"^Расписание на завтра$", "Programma di domani"),
        (r"^Ближайшие события$", "Prossimi eventi"),
        (r"^Всё ближайшее расписание$", "Programma imminente"),
        (r"^Следующее событие$", "Prossimo evento"),
        (r"^Расписание на (\d+) дн\.$", "Programma per {0} giorni"),
        (r"^\s*Когда:\s*(.+)$", "   Quando: {0}"),
        (r"^\s*Где:\s*(.+)$", "   Dove: {0}"),
        (r"^\s*Описание:\s*(.+)$", "   Descrizione: {0}"),
        (r"^Всего писем:\s*(\d+)$", "Messaggi totali: {0}"),
        (r"^Непрочитанные:\s*(\d+)$", "Non letti: {0}"),
        (r"^За последние 24 часа:\s*(\d+)$", "Ultime 24 ore: {0}"),
        (r"^Сегодня:\s*(\d+)$", "Oggi: {0}"),
        (r"^Папки:$", "Cartelle:"),
        (r"^\s+Непрочитанные:\s*(\d+)$", '  Non letti: {0}'),
        (r"^\s+Сегодня:\s*(\d+)$", '  Oggi: {0}'),
        (r"^\s*Всего:\s*(\d+)$", "  Totale: {0}"),
        (r"^\s*За 24 часа:\s*(\d+)$", "  Ultime 24 ore: {0}"),
        (r"^• (.+): всего — (\d+)$", "• {0}: totale — {1}"),
        (r"^• (.+): непрочитанные — (\d+)$", "• {0}: non letti — {1}"),
        (r"^• (.+): за 24 часа — (\d+)$", "• {0}: ultime 24 h — {1}"),
        (r"^• (.+): сегодня — (\d+)$", "• {0}: oggi — {1}"),
    ),
    "pt": (
        (r"^Google Calendar подключён\.$", "Google Calendar conectado."),
        (r"^Событий нет\.$", "Nenhum evento."),
        (r"^Расписание$", "Agenda"),
        (r"^Расписание на сегодня$", "Agenda de hoje"),
        (r"^Расписание на завтра$", "Agenda de amanhã"),
        (r"^Ближайшие события$", "Próximos eventos"),
        (r"^Всё ближайшее расписание$", "Próxima agenda"),
        (r"^Следующее событие$", "Próximo evento"),
        (r"^Расписание на (\d+) дн\.$", "Agenda para {0} dias"),
        (r"^\s*Когда:\s*(.+)$", "   Quando: {0}"),
        (r"^\s*Где:\s*(.+)$", "   Onde: {0}"),
        (r"^\s*Описание:\s*(.+)$", "   Descrição: {0}"),
        (r"^Всего писем:\s*(\d+)$", "Total de mensagens: {0}"),
        (r"^Непрочитанные:\s*(\d+)$", "Não lidas: {0}"),
        (r"^За последние 24 часа:\s*(\d+)$", "Últimas 24 horas: {0}"),
        (r"^Сегодня:\s*(\d+)$", "Hoje: {0}"),
        (r"^Папки:$", "Pastas:"),
        (r"^\s+Непрочитанные:\s*(\d+)$", '  Não lidas: {0}'),
        (r"^\s+Сегодня:\s*(\d+)$", '  Hoje: {0}'),
        (r"^\s*Всего:\s*(\d+)$", "  Total: {0}"),
        (r"^\s*За 24 часа:\s*(\d+)$", "  Últimas 24 horas: {0}"),
        (r"^• (.+): всего — (\d+)$", "• {0}: total — {1}"),
        (r"^• (.+): непрочитанные — (\d+)$", "• {0}: não lidas — {1}"),
        (r"^• (.+): за 24 часа — (\d+)$", "• {0}: últimas 24 h — {1}"),
        (r"^• (.+): сегодня — (\d+)$", "• {0}: hoje — {1}"),
    ),
}

# NEO 182: a timer's name is user data; the expiry template around it is already localized.
_TIMER_DONE182 = {
    "en": r"^(.+): time is up\.$", "de": r"^(.+): Zeit ist abgelaufen\.$", "fr": r"^(.+) : temps écoulé\.$",
    "es": r"^(.+): se acabó el tiempo\.$", "it": r"^(.+): tempo scaduto\.$", "pt": r"^(.+): o tempo acabou\.$",
}
_TIMER_DONE_RU182 = {
    "en": "{0}: time is up.", "de": "{0}: Zeit ist abgelaufen.", "fr": "{0} : temps écoulé.",
    "es": "{0}: se acabó el tiempo.", "it": "{0}: tempo scaduto.", "pt": "{0}: o tempo acabou.",
}
_GENERIC_TIMER182 = {"таймер": ("Timer", "Timer", "Minuteur", "Temporizador", "Timer", "Temporizador"),
                     "напоминание": ("Reminder", "Erinnerung", "Rappel", "Recordatorio", "Promemoria", "Lembrete"),
                     "будильник": ("Alarm", "Wecker", "Alarme", "Alarma", "Sveglia", "Alarme")}
for _lang182, _pattern182 in _TIMER_DONE182.items():
    _STRUCTURED_PATTERNS.setdefault(_lang182, ())
    _STRUCTURED_PATTERNS[_lang182] = ((_pattern182, "{0}" + _TIMER_DONE_RU182[_lang182][3:]),
                                      (r"^(.+): время вышло\.$", _TIMER_DONE_RU182[_lang182])) + tuple(_STRUCTURED_PATTERNS[_lang182])


def _timer_name182(name: str, lang: str) -> str:
    row = _GENERIC_TIMER182.get(name.strip().casefold())
    return row[("en", "de", "fr", "es", "it", "pt").index(lang)] if row and lang in _TIMER_DONE182 else name


def _localize_one(source: str, lang: str) -> str:
    if not source or not re.search(r"[А-Яа-яЁё]", source):
        return source
    exact = _ui_catalog(lang).get(source)
    if exact:
        return exact
    for pat, repl in _STRUCTURED_PATTERNS.get(lang, ()):
        match = re.match(pat, source, flags=re.I | re.S)
        if match:
            if lang in _TIMER_DONE182 and pat in (_TIMER_DONE182[lang], r"^(.+): время вышло\.$"):
                return _TIMER_DONE_RU182[lang].format(_timer_name182(match.group(1), lang))
            # Calendar titles/locations and mailbox folder names are external data,
            # not interface strings; preserve them while translating the labels.
            return repl.format(*match.groups())
    for pat, repl in _PATTERNS.get(lang, ()):
        match = re.match(pat, source, flags=re.I | re.S)
        if match:
            rendered = repl.format(*match.groups())
            # A Cyrillic filename, artist or user-authored title is opaque data,
            # not untranslated UI. Never discard it because of its alphabet.
            return rendered

    # Preserve useful opaque entities without pretending the operation succeeded.
    failed = bool(re.search(r"(?:не удалось|ошиб|не найден|не наш|не смог|отклон|заблок|невозможно|failed|error)", source, re.I))
    fallback = _FALLBACK[lang][0 if failed else 1]
    payloads: list[str] = []
    payloads.extend(re.findall(r"«([^»]{1,180})»", source))
    payloads.extend(re.findall(r"(?:[A-Za-z]:\\[^\n;]+|https?://\S+)", source))
    clean: list[str] = []
    seen: set[str] = set()
    for item in payloads:
        value = str(item).strip().rstrip(".,")
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            clean.append(value)
    return fallback + ((" " + " | ".join(clean[:4])) if clean else "")


def localize_runtime_text(text: str, code: str) -> str:
    source = str(text or "")
    lang = str(code or "ru").strip().casefold()
    if lang not in SUPPORTED:
        lang = "ru"
    if lang == "ru" or not source or not re.search(r"[А-Яа-яЁё]", source):
        return source

    # Multi-line deterministic results are localized line-by-line. This preserves
    # already-correct English paths/metrics and prevents one Russian fragment from
    # collapsing an entire startup briefing into a generic success phrase.
    if "\n" in source:
        rendered_lines: list[str] = []
        for line in source.split("\n"):
            # Numbered event/message titles and bare bullet labels are service/user
            # data, not UI chrome. Preserve them verbatim while translating the
            # structural labels around them.
            stripped = line.strip()
            if re.match(r"^\d+\.\s+.+$", stripped) or (stripped.startswith("• ") and ":" not in stripped):
                rendered_lines.append(line)
            else:
                rendered_lines.append(_localize_one(line, lang) if re.search(r"[А-Яа-яЁё]", line) else line)
        return "\n".join(rendered_lines)
    return _localize_one(source, lang)


# Typed tool projection. Each row is RU, EN, DE, FR, ES, IT, PT. The handler's
# success and structured data remain authoritative; text is never used to infer
# success. Content returned by translation, screen analysis, file reading and
# web search is user/provider content and is deliberately not machine-translated.
_LANG_ORDER = ("ru", "en", "de", "fr", "es", "it", "pt")

_RESULT_WORDS = {
    "cancelled": ("Действие отменено", "Action cancelled", "Aktion abgebrochen", "Action annulée", "Acción cancelada", "Operazione annullata", "Ação cancelada"),
    "failed": ("Действие не выполнено", "Action failed", "Aktion fehlgeschlagen", "Échec de l’action", "La acción falló", "Operazione non riuscita", "A ação falhou"),
    "pending": ("Результат ещё не подтверждён", "Outcome is not yet confirmed", "Ergebnis noch nicht bestätigt", "Résultat pas encore confirmé", "El resultado aún no está confirmado", "Esito non ancora confermato", "Resultado ainda não confirmado"),
    "partial": ("Выполнено частично", "Partially completed", "Teilweise ausgeführt", "Exécution partielle", "Completado parcialmente", "Completato parzialmente", "Parcialmente concluído"),
    "details": ("Подробности", "Details", "Details", "Détails", "Detalles", "Dettagli", "Detalhes"),
    "empty": ("Ничего не найдено", "Nothing found", "Nichts gefunden", "Aucun résultat", "No se encontró nada", "Nessun risultato", "Nenhum resultado"),
    "yes": ("да", "yes", "ja", "oui", "sí", "sì", "sim"),
    "no": ("нет", "no", "nein", "non", "no", "no", "não"),
    "auth": ("Нужно подключить аккаунт или обновить авторизацию", "Connect the account or renew authorization", "Konto verbinden oder Autorisierung erneuern", "Connectez le compte ou renouvelez l’autorisation", "Conecta la cuenta o renueva la autorización", "Collega l’account o rinnova l’autorizzazione", "Ligue a conta ou renove a autorização"),
    "network": ("Сервис временно недоступен по сети", "The service is temporarily unreachable", "Der Dienst ist vorübergehend nicht erreichbar", "Le service est temporairement inaccessible", "El servicio no está disponible temporalmente", "Il servizio è temporaneamente irraggiungibile", "O serviço está temporariamente inacessível"),
    "timeout": ("Время ожидания истекло; результат может быть неизвестен. Повтор не отправлен", "Timed out; the outcome may be unknown. No retry was sent", "Zeitüberschreitung; Ergebnis möglicherweise unbekannt. Keine Wiederholung gesendet", "Délai dépassé ; le résultat peut être inconnu. Aucun nouvel envoi", "Se agotó el tiempo; el resultado puede ser desconocido. No se reenvió", "Tempo scaduto; l’esito potrebbe essere sconosciuto. Nessun nuovo invio", "Tempo esgotado; o resultado pode ser desconhecido. Não foi repetido"),
    "denied": ("Доступ или действие запрещены", "Access or action is denied", "Zugriff oder Aktion verweigert", "Accès ou action refusés", "Acceso o acción denegados", "Accesso o azione negati", "Acesso ou ação negados"),
    "disabled": ("Функция отключена в настройках", "The feature is disabled in settings", "Die Funktion ist in den Einstellungen deaktiviert", "La fonction est désactivée dans les paramètres", "La función está desactivada en los ajustes", "La funzione è disattivata nelle impostazioni", "A função está desativada nas configurações"),
    "invalid": ("Проверьте параметры команды", "Check the command parameters", "Befehlsparameter prüfen", "Vérifiez les paramètres de la commande", "Comprueba los parámetros del comando", "Controlla i parametri del comando", "Verifique os parâmetros do comando"),
    "not_found": ("Указанный объект не найден", "The requested item was not found", "Das gesuchte Objekt wurde nicht gefunden", "L’élément demandé est introuvable", "No se encontró el elemento solicitado", "L’elemento richiesto non è stato trovato", "O item solicitado não foi encontrado"),
}

_ACTION_ROWS = {
    "open": ("Открыто", "Opened", "Geöffnet", "Ouvert", "Abierto", "Aperto", "Aberto"),
    "close": ("Закрыто", "Closed", "Geschlossen", "Fermé", "Cerrado", "Chiuso", "Fechado"),
    "start": ("Запущено", "Started", "Gestartet", "Démarré", "Iniciado", "Avviato", "Iniciado"),
    "stop": ("Остановлено", "Stopped", "Gestoppt", "Arrêté", "Detenido", "Fermato", "Parado"),
    "pause": ("Приостановлено", "Paused", "Pausiert", "En pause", "En pausa", "In pausa", "Em pausa"),
    "play": ("Воспроизведение", "Playback", "Wiedergabe", "Lecture", "Reproducción", "Riproduzione", "Reprodução"),
    "next": ("Следующий трек", "Next track", "Nächster Titel", "Piste suivante", "Pista siguiente", "Traccia successiva", "Faixa seguinte"),
    "previous": ("Предыдущий трек", "Previous track", "Vorheriger Titel", "Piste précédente", "Pista anterior", "Traccia precedente", "Faixa anterior"),
    "search": ("Результаты поиска", "Search results", "Suchergebnisse", "Résultats de recherche", "Resultados de búsqueda", "Risultati di ricerca", "Resultados da pesquisa"),
    "status": ("Состояние", "Status", "Status", "État", "Estado", "Stato", "Estado"),
    "list": ("Список", "List", "Liste", "Liste", "Lista", "Elenco", "Lista"),
    "create": ("Создано", "Created", "Erstellt", "Créé", "Creado", "Creato", "Criado"),
    "update": ("Изменено", "Updated", "Aktualisiert", "Modifié", "Actualizado", "Aggiornato", "Atualizado"),
    "delete": ("Удалено", "Deleted", "Gelöscht", "Supprimé", "Eliminado", "Eliminato", "Eliminado"),
    "clear": ("Очищено", "Cleared", "Geleert", "Effacé", "Vaciado", "Svuotato", "Limpo"),
    "copy": ("Скопировано", "Copied", "Kopiert", "Copié", "Copiado", "Copiato", "Copiado"),
    "move": ("Перемещено", "Moved", "Verschoben", "Déplacé", "Movido", "Spostato", "Movido"),
    "rename": ("Переименовано", "Renamed", "Umbenannt", "Renommé", "Renombrado", "Rinominato", "Renomeado"),
    "read": ("Содержимое", "Contents", "Inhalt", "Contenu", "Contenido", "Contenuto", "Conteúdo"),
    "write": ("Текст сохранён", "Text saved", "Text gespeichert", "Texte enregistré", "Texto guardado", "Testo salvato", "Texto guardado"),
    "undo": ("Изменение отменено", "Change undone", "Änderung rückgängig gemacht", "Modification annulée", "Cambio deshecho", "Modifica annullata", "Alteração desfeita"),
    "volume": ("Громкость", "Volume", "Lautstärke", "Volume", "Volumen", "Volume", "Volume"),
    "mute": ("Отключение звука", "Mute", "Stummschaltung", "Sourdine", "Silencio", "Disattivazione audio", "Silenciar"),
    "seek": ("Позиция воспроизведения", "Playback position", "Wiedergabeposition", "Position de lecture", "Posición de reproducción", "Posizione di riproduzione", "Posição de reprodução"),
    "queue": ("Очередь воспроизведения", "Playback queue", "Wiedergabewarteschlange", "File de lecture", "Cola de reproducción", "Coda di riproduzione", "Fila de reprodução"),
    "shuffle": ("Случайный порядок", "Shuffle", "Zufallswiedergabe", "Lecture aléatoire", "Reproducción aleatoria", "Riproduzione casuale", "Reprodução aleatória"),
    "repeat": ("Повтор", "Repeat", "Wiederholung", "Répétition", "Repetición", "Ripetizione", "Repetição"),
    "like": ("Оценка трека сохранена", "Track preference saved", "Titelbewertung gespeichert", "Préférence de piste enregistrée", "Preferencia de pista guardada", "Preferenza del brano salvata", "Preferência da faixa guardada"),
    "show": ("Показано", "Shown", "Angezeigt", "Affiché", "Mostrado", "Mostrato", "Mostrado"),
    "hide": ("Скрыто", "Hidden", "Ausgeblendet", "Masqué", "Oculto", "Nascosto", "Oculto"),
    "focus": ("Выбрано", "Selected", "Ausgewählt", "Sélectionné", "Seleccionado", "Selezionato", "Selecionado"),
    "minimize": ("Свёрнуто", "Minimized", "Minimiert", "Réduit", "Minimizado", "Ridotto", "Minimizado"),
    "maximize": ("Развёрнуто", "Maximized", "Maximiert", "Agrandie", "Maximizado", "Ingrandito", "Maximizado"),
    "restore": ("Восстановлено", "Restored", "Wiederhergestellt", "Restauré", "Restaurado", "Ripristinato", "Restaurado"),
    "settings": ("Настройки", "Settings", "Einstellungen", "Paramètres", "Ajustes", "Impostazioni", "Configurações"),
    "send": ("Отправлено", "Sent", "Gesendet", "Envoyé", "Enviado", "Inviato", "Enviado"),
    "reply": ("Ответ отправлен", "Reply sent", "Antwort gesendet", "Réponse envoyée", "Respuesta enviada", "Risposta inviata", "Resposta enviada"),
    "connect": ("Подключение", "Connection", "Verbindung", "Connexion", "Conexión", "Connessione", "Ligação"),
    "disconnect": ("Отключение", "Disconnection", "Trennung", "Déconnexion", "Desconexión", "Disconnessione", "Desligação"),
    "capture": ("Снимок сохранён", "Screenshot saved", "Screenshot gespeichert", "Capture enregistrée", "Captura guardada", "Schermata salvata", "Captura guardada"),
    "analyze": ("Анализ изображения", "Image analysis", "Bildanalyse", "Analyse d’image", "Análisis de imagen", "Analisi dell’immagine", "Análise da imagem"),
    "translate": ("Перевод", "Translation", "Übersetzung", "Traduction", "Traducción", "Traduzione", "Tradução"),
    "cancel": ("Отменено", "Cancelled", "Abgebrochen", "Annulé", "Cancelado", "Annullato", "Cancelado"),
    "toggle": ("Режим изменён", "Mode changed", "Modus geändert", "Mode modifié", "Modo cambiado", "Modalità cambiata", "Modo alterado"),
    "lock": ("Блокировка", "Lock", "Sperren", "Verrouillage", "Bloqueo", "Blocco", "Bloqueio"),
    "shutdown": ("Завершение работы", "Shutdown", "Herunterfahren", "Arrêt", "Apagado", "Arresto", "Desligar"),
    "restart": ("Перезапуск", "Restart", "Neustart", "Redémarrage", "Reinicio", "Riavvio", "Reiniciar"),
    "sleep": ("Сон", "Sleep", "Energiesparmodus", "Veille", "Suspensión", "Sospensione", "Suspensão"),
    "hibernate": ("Гибернация", "Hibernate", "Ruhezustand", "Veille prolongée", "Hibernación", "Ibernazione", "Hibernação"),
    "brightness": ("Яркость", "Brightness", "Helligkeit", "Luminosité", "Brillo", "Luminosità", "Brilho"),
    "click": ("Нажатие выполнено", "Click performed", "Klick ausgeführt", "Clic effectué", "Clic realizado", "Clic eseguito", "Clique executado"),
    "key": ("Клавиши нажаты", "Keys pressed", "Tasten gedrückt", "Touches pressées", "Teclas pulsadas", "Tasti premuti", "Teclas premidas"),
    "check": ("Отмечено", "Checked", "Abgehakt", "Coché", "Marcado", "Selezionato", "Marcado"),
    "uncheck": ("Отметка снята", "Unchecked", "Markierung entfernt", "Décoché", "Desmarcado", "Deselezionato", "Desmarcado"),
    "pin": ("Закрепление", "Pinning", "Anheften", "Épinglage", "Fijación", "Fissaggio", "Fixação"),
    "archive": ("Архив создан", "Archive created", "Archiv erstellt", "Archive créée", "Archivo creado", "Archivio creato", "Arquivo criado"),
    "extract": ("Архив распакован", "Archive extracted", "Archiv entpackt", "Archive extraite", "Archivo extraído", "Archivio estratto", "Arquivo extraído"),
    "hash": ("Контрольная сумма", "Checksum", "Prüfsumme", "Somme de contrôle", "Suma de comprobación", "Somma di controllo", "Soma de verificação"),
    "exists": ("Наличие объекта", "Item existence", "Vorhandenes Objekt", "Présence de l’élément", "Existencia del elemento", "Esistenza dell’elemento", "Existência do item"),
    "test": ("Результаты проверки", "Test results", "Prüfergebnisse", "Résultats des tests", "Resultados de pruebas", "Risultati dei test", "Resultados dos testes"),
    "export": ("Экспортировано", "Exported", "Exportiert", "Exporté", "Exportado", "Esportato", "Exportado"),
    "import": ("Импортировано", "Imported", "Importiert", "Importé", "Importado", "Importato", "Importado"),
    "zoom": ("Масштаб", "Zoom", "Zoom", "Zoom", "Zoom", "Zoom", "Zoom"),
}

_GROUPS = {
    "open": "open open_url new_tab open_inbox open_settings open_task_manager reveal",
    "close": "close close_tab close_others close_with_content close_instance keep_only_content",
    "start": "start run run_python_file",
    "stop": "stop kill terminate_process music_stop",
    "pause": "pause music_pause",
    "play": "play play_pause resume music_resume play_track play_song play_music start_track playlist play_playlist playlist_by_name start_playlist my_wave my_tracks music_bot_play music_youtube_url music_my_tracks music_my_wave music_playlist play_search",
    "next": "next music_skip",
    "previous": "previous music_previous",
    "search": "search search_first search_track find_track find_song find files content search_text resolve from_sender",
    "status": "status background_status api_status info active state session_info context date_info summary startup_summary current temperature forecast rain precipitation humidity wind sunrise sunset cpu gpu memory disks network count total text_channel_status get_input_volume get_power_plan get_mouse_sensitivity",
    "list": "list list_active list_sessions list_devices list_controls list_drives list_instances list_browsers list_channels list_power_plans top_processes tree disk_usage providers folders unread unread_list unread_today unread_today_list unread_recent24h unread_recent24h_list recent24h recent24h_list today today_list tomorrow upcoming all latest recent plugins objects latency",
    "create": "create create_folder create_text_file create_file checklist_create add draft timer reminder alarm generate_python_app remember",
    "update": "update set set_default route_app_device configure set_rate set_button style set_wifi set_bluetooth set_power_plan set_mouse_sensitivity change_mouse_sensitivity group_by_topic",
    "delete": "delete delete_permanent delete_all forget checklist_remove",
    "clear": "clear clear_history full_cleanup queue_clear music_queue_clear clear_text checklist_clear",
    "copy": "copy copy_history copy_page_url",
    "move": "move move_resize move_to_monitor move_instance move_to_new_window",
    "rename": "rename",
    "read": "read read_text",
    "write": "write_text replace_text append_text insert_text type_text set_control_text set_text remove_text dictate fix_selection replace_last_sentence paste_history checklist_add",
    "undo": "undo undo_last_change",
    "volume": "set_volume change_volume change_target_volume set_input_volume volume_up volume_down music_set_volume music_volume_up music_volume_down duck_system restore_ducking",
    "mute": "mute set_input_mute mute_target mute_except",
    "seek": "seek seek_forward seek_backward seek_to",
    "queue": "queue queue_status queue_add queue_next music_queue_status music_queue_add music_queue_next parallel",
    "shuffle": "shuffle_on shuffle_off music_shuffle_on music_shuffle_off",
    "repeat": "repeat_on repeat_track repeat_playlist repeat_off music_repeat_track music_repeat_playlist music_repeat_off",
    "like": "like unlike dislike music_like music_dislike",
    "show": "show show_session fullscreen theater",
    "hide": "hide hide_all turn_off_displays",
    "focus": "focus focus_instance focus_control activate_tab activate_tab_title activate_by_topic select_previous_paragraph",
    "minimize": "minimize",
    "maximize": "maximize",
    "restore": "restore restore_closed",
    "settings": "settings",
    "send": "send send_message clipboard_to_discord page_url_to_discord last_screenshot_to_discord selected_explorer_file_to_discord attach_file_discord",
    "reply": "reply",
    "connect": "connect authorize bot_join_voice connect_channel attach_text_channel",
    "disconnect": "bot_leave_voice detach_text_channel",
    "capture": "capture capture_window",
    "analyze": "analyze analyze_window",
    "translate": "translate",
    "cancel": "cancel cancel_shutdown",
    "toggle": "toggle",
    "lock": "lock",
    "shutdown": "shutdown",
    "restart": "restart flush_dns",
    "sleep": "sleep",
    "hibernate": "hibernate",
    "brightness": "set_brightness",
    "click": "click click_control click_visual",
    "key": "hotkey",
    "check": "checklist_check checklist_complete_all mark_read",
    "uncheck": "checklist_uncheck checklist_uncheck_all",
    "pin": "pin pin_current",
    "archive": "zip",
    "extract": "unzip",
    "hash": "hash",
    "exists": "exists",
    "test": "self_test diagnostic ping",
    "export": "export",
    "import": "import_file",
    "zoom": "zoom",
}
ACTION_CATEGORIES = {alias: category for category, aliases in _GROUPS.items() for alias in aliases.split()}

_FIELDS = {
    "path": ("Путь", "Path", "Pfad", "Chemin", "Ruta", "Percorso", "Caminho"),
    "source": ("Источник", "Source", "Quelle", "Source", "Origen", "Origine", "Origem"),
    "destination": ("Назначение", "Destination", "Ziel", "Destination", "Destino", "Destinazione", "Destino"),
    "title": ("Название", "Title", "Titel", "Titre", "Título", "Titolo", "Título"),
    "artist": ("Исполнитель", "Artist", "Interpret", "Artiste", "Artista", "Artista", "Artista"),
    "count": ("Количество", "Count", "Anzahl", "Nombre", "Cantidad", "Numero", "Quantidade"),
    "state": ("Состояние", "State", "Zustand", "État", "Estado", "Stato", "Estado"),
    "volume": _ACTION_ROWS["volume"],
    "duration_ms": ("Длительность (мс)", "Duration (ms)", "Dauer (ms)", "Durée (ms)", "Duración (ms)", "Durata (ms)", "Duração (ms)"),
    "position_ms": ("Позиция (мс)", "Position (ms)", "Position (ms)", "Position (ms)", "Posición (ms)", "Posizione (ms)", "Posição (ms)"),
    "size": ("Размер", "Size", "Größe", "Taille", "Tamaño", "Dimensione", "Tamanho"),
    "updated_at": ("Обновлено", "Updated", "Aktualisiert", "Mis à jour", "Actualizado", "Aggiornato", "Atualizado"),
    "queue": _ACTION_ROWS["queue"],
    "history": ("Предыдущие треки", "Previous tracks", "Vorherige Titel", "Pistes précédentes", "Pistas anteriores", "Tracce precedenti", "Faixas anteriores"),
    "enabled": ("Включено", "Enabled", "Aktiviert", "Activé", "Activado", "Attivato", "Ativado"),
    "temperature": ("Температура", "Temperature", "Temperatur", "Température", "Temperatura", "Temperatura", "Temperatura"),
    "humidity": ("Влажность", "Humidity", "Luftfeuchtigkeit", "Humidité", "Humedad", "Umidità", "Humidade"),
    "name": ("Имя", "Name", "Name", "Nom", "Nombre", "Nome", "Nome"),
    "target": ("Объект", "Target", "Ziel", "Cible", "Destino", "Destinazione", "Destino"),
}


def _word(rows: dict[str, tuple[str, ...]], key: str, code: str) -> str:
    row = rows.get(key)
    return row[_LANG_ORDER.index(code)] if row else key.replace("_", " ")


def _opaque(value: Any, code: str) -> str:
    if isinstance(value, bool):
        return _word(_RESULT_WORDS, "yes" if value else "no", code)
    return str(value)


def _result_details(data: Any, arguments: dict[str, Any], code: str) -> list[str]:
    # Allow-listed presentation fields, never arbitrary secret-bearing dict dumps.
    if isinstance(data, list):
        rows = []
        for item in data[:20]:
            if isinstance(item, dict):
                values = [item.get(k) for k in ("name", "title", "artist", "path", "destination", "when", "start", "count", "pid") if item.get(k) is not None]
                line = " · ".join(_opaque(x, code) for x in values if not isinstance(x, (dict, list)))
                if line: rows.append("• " + line)
            elif isinstance(item, (str, int, float)):
                rows.append("• " + str(item))
        return [_word(_FIELDS, "count", code) + ": " + str(len(data)), *rows]
    if not isinstance(data, dict):
        if isinstance(data, (str, int, float, bool)) and data != "":
            return [_opaque(data, code)]
        data = {}
    rows = []
    fields = ("path", "source", "destination", "title", "artist", "count", "state", "volume", "volume_percent", "duration_ms", "position_ms", "size", "size_bytes", "pid", "sha256", "exists", "enabled", "temperature", "humidity", "updated_at", "removed", "matched", "unread", "total", "latency_ms", "query", "name", "target")
    used_values: set[str] = set()
    for key in fields:
        value = data.get(key)
        if value is None or value == "" or isinstance(value, (dict, list, tuple)):
            continue
        rendered = _opaque(value, code)
        if rendered in used_values:
            continue
        used_values.add(rendered)
        label_key = {"volume_percent": "volume", "size_bytes": "size", "removed": "count", "matched": "count", "total": "count"}.get(key, key)
        rows.append(_word(_FIELDS, label_key, code) + ": " + rendered)
    if not rows:
        for key in ("destination", "path", "source", "name", "query", "target", "title", "value", "volume"):
            value = arguments.get(key)
            if value is not None and value != "" and not isinstance(value, (list, dict)):
                rows.append(_word(_FIELDS, key, code) + ": " + _opaque(value, code))
    for key in ("queue", "history", "items", "results", "files", "events", "processes", "devices", "rows", "downloads"):
        values = data.get(key)
        if isinstance(values, list) and values:
            selected = list(reversed(values[-5:])) if key == "history" else values[:5] if key == "queue" else values[:20]
            rows.append(_word(_FIELDS, key, code) + ":")
            rows.extend(_result_details(selected, {}, code)[1:])
    return rows


_MEDIA_SENTENCES = {
    "next": ("Включил следующий трек", "Playing the next track", "Der nächste Titel läuft", "Je lance le titre suivant", "Reproduciendo la siguiente canción", "Riproduco il brano successivo", "A tocar a próxima música"),
    "previous": ("Вернул предыдущий трек", "Playing the previous track", "Der vorherige Titel läuft", "Je remets le titre précédent", "Reproduciendo la canción anterior", "Riproduco il brano precedente", "A tocar a música anterior"),
    "play": ("Готово, продолжил воспроизведение", "Done, I resumed playback", "Erledigt, ich habe die Wiedergabe fortgesetzt", "C’est fait, j’ai repris la lecture", "Listo, he reanudado la reproducción", "Fatto, ho ripreso la riproduzione", "Feito, retomei a reprodução"),
    "pause": ("Готово, поставил музыку на паузу", "Done, I paused the music", "Erledigt, ich habe die Musik pausiert", "C’est fait, j’ai mis la musique en pause", "Listo, he puesto la música en pausa", "Fatto, ho messo in pausa la musica", "Feito, coloquei a música em pausa"),
    "stop": ("Готово, остановил музыку", "Done, I stopped the music", "Erledigt, ich habe die Musik gestoppt", "C’est fait, j’ai arrêté la musique", "Listo, he detenido la música", "Fatto, ho fermato la musica", "Feito, parei a música"),
    "start": ("Включил", "Now playing", "Jetzt läuft", "Lecture en cours", "Reproduciendo", "In riproduzione", "A tocar"),
    "queued": ("Добавил в очередь", "Added to the queue", "Zur Warteschlange hinzugefügt", "Ajouté à la file d’attente", "Añadido a la cola", "Aggiunto alla coda", "Adicionado à fila"),
    "queue_clear": ("Очистил очередь", "Queue cleared", "Warteschlange geleert", "File d’attente vidée", "Cola vaciada", "Coda svuotata", "Fila limpa"),
    "queue_status": ("Очередь воспроизведения", "Playback queue", "Wiedergabewarteschlange", "File de lecture", "Cola de reproducción", "Coda di riproduzione", "Fila de reprodução"),
    "stopped": ("Сейчас ничего не играет", "Nothing is playing", "Zurzeit läuft nichts", "Aucune lecture en cours", "No se está reproduciendo nada", "Nessuna riproduzione in corso", "Nada está a tocar"),
    "empty_queue": ("Очередь пуста", "The queue is empty", "Die Warteschlange ist leer", "La file d’attente est vide", "La cola está vacía", "La coda è vuota", "A fila está vazia"),
    "finished": ("Очередь закончилась; воспроизведение остановлено", "The queue has ended; playback stopped", "Die Warteschlange ist zu Ende; Wiedergabe gestoppt", "La file est terminée ; lecture arrêtée", "La cola ha terminado; reproducción detenida", "La coda è terminata; riproduzione interrotta", "A fila terminou; reprodução parada"),
    "shuffle_on": ("Включил случайный порядок", "Shuffle enabled", "Zufallswiedergabe aktiviert", "Lecture aléatoire activée", "Reproducción aleatoria activada", "Riproduzione casuale attivata", "Reprodução aleatória ativada"),
    "shuffle_off": ("Выключил случайный порядок", "Shuffle disabled", "Zufallswiedergabe deaktiviert", "Lecture aléatoire désactivée", "Reproducción aleatoria desactivada", "Riproduzione casuale disattivata", "Reprodução aleatória desativada"),
    "repeat_track": ("Включил повтор трека", "Track repeat enabled", "Titelwiederholung aktiviert", "Répétition du titre activée", "Repetición de canción activada", "Ripetizione del brano attivata", "Repetição da faixa ativada"),
    "repeat_playlist": ("Включил повтор плейлиста", "Playlist repeat enabled", "Playlistwiederholung aktiviert", "Répétition de la playlist activée", "Repetición de lista activada", "Ripetizione della playlist attivata", "Repetição da playlist ativada"),
    "repeat_off": ("Выключил повтор", "Repeat disabled", "Wiederholung deaktiviert", "Répétition désactivée", "Repetición desactivada", "Ripetizione disattivata", "Repetição desativada"),
}


def _render_media_result(tool: str, action: str, success: bool, data: Any, code: str) -> str | None:
    """Render the content source, independently of its local transport adapter."""
    if tool not in {"music", "yandex_music", "spotify", "media", "discord"} or not isinstance(data, dict):
        return None
    if data.get("cancelled") and data.get("superseded"):
        return (
            "Предыдущая музыкальная команда отменена более новой командой.",
            "The previous music command was cancelled by a newer command.",
            "Der vorherige Musikbefehl wurde durch einen neueren Befehl abgebrochen.",
            "La commande musicale précédente a été annulée par une commande plus récente.",
            "Una orden más reciente canceló la orden de música anterior.",
            "Il comando musicale precedente è stato annullato da un comando più recente.",
            "O comando de música anterior foi cancelado por um comando mais recente.",
        )[_LANG_ORDER.index(code)]
    if not success:
        if data.get("error_kind") == "not_found":
            sentence = ("Не нашёл подходящий трек. Уточните название или исполнителя.",
                        "I couldn’t find a matching track. Tell me the title or artist more precisely.",
                        "Ich habe keinen passenden Titel gefunden. Nenne mir den Titel oder Interpreten genauer.",
                        "Je n’ai pas trouvé de titre correspondant. Précise le titre ou l’artiste.",
                        "No encontré una canción coincidente. Precisa el título o el artista.",
                        "Non ho trovato un brano corrispondente. Precisa il titolo o l’artista.",
                        "Não encontrei uma faixa correspondente. Especifica melhor o título ou o artista.")[_LANG_ORDER.index(code)]
            return sentence + ("\n" + str(data["query"]) if data.get("query") else "")
        if data.get("error_kind") == "auth":
            return _word(_RESULT_WORDS, "auth", code) + "."
        if data.get("integration_disabled"):
            return _word(_RESULT_WORDS, "disabled", code) + "."
        if data.get("error_kind") == "service":
            return ("Музыкальный сервис вернул ошибку. Попробуйте команду ещё раз немного позже.",
                    "The music service returned an error. Try the command again a little later.",
                    "Der Musikdienst hat einen Fehler gemeldet. Versuche den Befehl etwas später erneut.",
                    "Le service musical a renvoyé une erreur. Réessaie la commande un peu plus tard.",
                    "El servicio de música devolvió un error. Intenta el comando de nuevo un poco más tarde.",
                    "Il servizio musicale ha restituito un errore. Riprova il comando tra poco.",
                    "O serviço de música devolveu um erro. Tenta o comando novamente daqui a pouco.")[_LANG_ORDER.index(code)]
        if data.get("error_kind") == "network":
            from jarvis.runtime.media_notice156 import network_notice
            return network_notice(code)
        return None
    if data.get("search_only") and data.get("verified") is True:
        from jarvis.runtime.dialogue_messages import message as dialogue_message
        provider = str(data.get("provider") or data.get("content_provider") or tool)
        label = {"yandex_music": "Яндекс Музыка" if code == "ru" else "Yandex Music", "spotify": "Spotify", "music": "Music"}.get(provider, provider)
        track = " — ".join(str(data.get(k) or "") for k in ("artist", "title") if data.get(k))
        return dialogue_message("found_track", code, track=track, provider=label)
    provider = str(data.get("content_provider") or "")
    if not provider:
        return None
    label = {"youtube": "YouTube", "yandex_music": "Яндекс Музыка" if code == "ru" else "Yandex Music", "spotify": "Spotify"}.get(provider)
    if not label:
        return None
    if data.get("verified") is False or data.get("outcome_pending") or data.get("outcome_unknown") or data.get("already_advancing") or data.get("starting"):
        return _word(_RESULT_WORDS, "pending", code) + f" ({label})."
    action = {"music_skip": "next", "music_previous": "previous", "music_pause": "pause", "music_resume": "play", "music_stop": "stop", "repeat_on": "repeat_track"}.get(action, action)
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    player = data.get("player") if isinstance(data.get("player"), dict) else {}
    title = str(data.get("title") or current.get("title") or player.get("title") or "")
    artist = str(data.get("artist") or current.get("artist") or player.get("artist") or "")
    track = " — ".join(x for x in (artist, title) if x)
    def upcoming() -> str:
        from jarvis.runtime.media_notice156 import queue_notice
        if not isinstance(data.get("queue"), list):
            return ""
        from jarvis.runtime.media_notice156 import queue_labels
        labels = queue_labels(data["queue"], code, 5)
        return queue_notice(None if not labels and (data.get("queue_refilling") or data.get("queue_unavailable")) else labels, code)
    if action in {"my_wave", "music_my_wave", "play_my_wave", "play_wave", "wave", "mywave"} or data.get("station_kind") == "my_wave" and action == "play_music":
        # One command answer includes service, station, real current track and
        # up to five *known* queued songs. Rotor may not have returned five yet.
        started = (
            'Включил Яндекс Музыку — «Моя волна»', 'Started Yandex Music — My Wave',
            'Yandex Music — Meine Welle gestartet', 'Yandex Music — Ma Vague démarrée',
            'Inicié Yandex Music — Mi Onda', 'Ho avviato Yandex Music — La mia onda',
            'Iniciei Yandex Music — Minha Onda',
        )
        active = (
            'Яндекс Музыка — «Моя волна» уже играет', 'Yandex Music — My Wave is already playing',
            'Yandex Music — Meine Welle läuft bereits', 'Yandex Music — Ma Vague joue déjà',
            'Yandex Music — Mi Onda ya está sonando', 'Yandex Music — La mia onda è già in riproduzione',
            'Yandex Music — Minha Onda já está a tocar',
        )
        now_words = ('Сейчас играет', 'Now playing', 'Gerade läuft', 'Lecture en cours', 'Ahora suena', 'In riproduzione', 'A tocar agora')
        next_words = ('Дальше в очереди', 'Up next', 'Als Nächstes', 'À suivre', 'A continuación', 'A seguire', 'A seguir')
        missing = (
            'Следующие треки пока не получены.', 'The next tracks are not available yet.',
            'Die nächsten Titel sind noch nicht verfügbar.', 'Les prochains titres ne sont pas encore disponibles.',
            'Las próximas canciones aún no están disponibles.', 'I prossimi brani non sono ancora disponibili.',
            'As próximas faixas ainda não estão disponíveis.',
        )
        pending = (
            'Остальные треки ещё подбираются.', 'More tracks are still being selected.',
            'Weitere Titel werden noch ausgewählt.', 'D’autres titres sont en cours de sélection.',
            'Se están seleccionando más canciones.', 'Altri brani sono in fase di selezione.',
            'Mais faixas estão a ser selecionadas.',
        )
        idx = _LANG_ORDER.index(code)
        body = (active if data.get('unchanged') else started)[idx] + '.'
        if track:
            body += ' ' + now_words[idx] + ': ' + track + '.'
        labels = []
        for row in list(data.get('queue') or [])[:5]:
            if isinstance(row, dict):
                name = ' — '.join(str(row.get(k) or '').strip() for k in ('artist', 'title') if str(row.get(k) or '').strip())
                if name:
                    labels.append(f'{len(labels) + 1}. {name}')
        body += '\n' + (next_words[idx] + ':\n' + '\n'.join(labels) if labels else missing[idx])
        if labels and len(labels) < 5 and data.get('queue_refilling'):
            body += '\n' + pending[idx]
        return body
    key = action
    if data.get("playlist_finished"):
        key = "finished"
    elif data.get("queued") and not data.get("queue_started_immediately") and not data.get("play_now"):
        key = "queued"
    elif action in {"play_search", "youtube_url", "play_track", "play_song", "start_track"}:
        key = "start"
    elif action in {"status", "background_status"}:
        state = str(data.get("state") or player.get("state") or "").casefold()
        if state == "paused" or data.get("paused") is True:
            sentence = ("Музыка на паузе", "The music is paused", "Die Musik ist pausiert", "La musique est en pause", "La música está en pausa", "La musica è in pausa", "A música está em pausa")[_LANG_ORDER.index(code)]
            return sentence + f" ({label})." + upcoming()
        if state in {"playing", "buffering", "starting"} or data.get("playing") is True:
            if state in {"buffering", "starting"}:
                return _word(_RESULT_WORDS, "pending", code) + f" ({label})."
            sentence = ("Сейчас играет", "Now playing", "Gerade läuft", "Lecture en cours", "Reproduciendo ahora", "In riproduzione", "A tocar agora")[_LANG_ORDER.index(code)]
            return sentence + f" ({label})" + (": " + track if track else "") + "." + upcoming()
        key = "stopped"
    if key not in _MEDIA_SENTENCES:
        return None
    if data.get("unchanged") and action in {"pause", "play", "stop"}:
        already = {
            "pause": ("Музыка уже на паузе", "The music is already paused", "Die Musik ist bereits pausiert", "La musique est déjà en pause", "La música ya está en pausa", "La musica è già in pausa", "A música já está em pausa"),
            "play": ("Музыка уже играет", "The music is already playing", "Die Musik läuft bereits", "La musique joue déjà", "La música ya está sonando", "La musica è già in riproduzione", "A música já está a tocar"),
            "stop": ("Музыка уже остановлена", "The music is already stopped", "Die Musik ist bereits gestoppt", "La musique est déjà arrêtée", "La música ya está detenida", "La musica è già ferma", "A música já está parada"),
        }
        return _word(already, action, code) + f" ({label})."
    message = _word(_MEDIA_SENTENCES, key, code) + f" ({label})"
    if track and key not in {"stop", "pause", "stopped", "queue_clear", "finished"}:
        message += ": " + track
    message += "."
    if isinstance(data.get("queue"), list):
        message += upcoming()
    return message


def _render_scenario_result(source: str, data: Any, code: str) -> str:
    """Project the runner's outcome; a completed run is no longer a start action."""
    from jarvis.ui.scenario_strings import execution_error_text, st
    if not isinstance(data, dict) or not data.get("status"):
        return source
    name = str(data.get("name", "") or "")
    lines = [f"{name}: {st(str(data['status']), code)}" if name else source]
    summary = data.get("summary")
    if isinstance(summary, dict) and summary:
        lines.append(st("result_summary", code, **{key: summary.get(key, 0)
                     for key in ("completed", "failed", "skipped", "not_run")}))
    error = execution_error_text(data.get("error"), code)
    if error:
        lines.append(error)
    for outcome in data.get("outcomes", []) or []:
        if not isinstance(outcome, dict):
            continue
        key = ("result_not_run" if outcome.get("not_run") else "step_skipped" if outcome.get("skipped")
               else "cancelled" if outcome.get("cancelled") else "result_done" if outcome.get("success") else "failed")
        line = f"{st('step', code)} {outcome.get('step_index', 0)} · {st(key, code)}"
        if outcome.get("opened") and not outcome.get("success"):
            line += " · " + st("result_opened", code)
        if outcome.get("path"):
            line += " · " + str(outcome["path"])
        detail = execution_error_text(outcome.get("error"), code)
        if detail:
            line += " · " + detail
        lines.append(line)
    return "\n".join(lines)


_APP_OPENED186 = {
    "en": ("Opened {label}.", "Opened {label} and brought its window to the front."),
    "de": ("{label} geöffnet.", "{label} geöffnet und sein Fenster in den Vordergrund geholt."),
    "fr": ("{label} ouvert.", "{label} ouvert et sa fenêtre mise au premier plan."),
    "es": ("He abierto {label}.", "He abierto {label} y he traído su ventana al frente."),
    "it": ("Ho aperto {label}.", "Ho aperto {label} e portato la sua finestra in primo piano."),
    "pt": ("Abri {label}.", "Abri {label} e trouxe a janela para a frente."),
}
_APP_SWITCHED187 = {
    "en": "{label} was already running — switched to its window.",
    "de": "{label} lief bereits — zu seinem Fenster gewechselt.",
    "fr": "{label} était déjà ouvert — je suis passé à sa fenêtre.",
    "es": "{label} ya estaba abierto: he cambiado a su ventana.",
    "it": "{label} era già in esecuzione: sono passato alla sua finestra.",
    "pt": "{label} já estava aberto — mudei para a janela dele.",
}
_APP_NOT_FOUND186 = {
    "en": "I couldn't find an installed application for «{query}». I didn't open any similar folders or files.",
    "de": "Für «{query}» habe ich keine installierte Anwendung gefunden. Ähnliche Ordner oder Dateien habe ich nicht geöffnet.",
    "fr": "Je n’ai trouvé aucune application installée pour «{query}». Je n’ai ouvert aucun dossier ni fichier similaire.",
    "es": "No encontré ninguna aplicación instalada para «{query}». No abrí carpetas ni archivos parecidos.",
    "it": "Non ho trovato nessuna applicazione installata per «{query}». Non ho aperto cartelle o file simili.",
    "pt": "Não encontrei nenhuma aplicação instalada para «{query}». Não abri pastas nem ficheiros parecidos.",
}


_OVERLAY186 = {
    #        checklist_add                       checklist_check                     checklist_uncheck                         checklist_remove                       append_text                         set_text
    "en": ("Added «{item}» to «{panel}».", "Checked «{item}» in «{panel}».", "Unchecked «{item}» in «{panel}».", "Removed «{item}» from «{panel}».", "Added the text to «{panel}».", "Replaced the text in «{panel}»."),
    "de": ("«{item}» zu «{panel}» hinzugefügt.", "«{item}» in «{panel}» abgehakt.", "Haken bei «{item}» in «{panel}» entfernt.", "«{item}» aus «{panel}» entfernt.", "Text zu «{panel}» hinzugefügt.", "Text in «{panel}» ersetzt."),
    "fr": ("«{item}» ajouté à «{panel}».", "«{item}» coché dans «{panel}».", "«{item}» décoché dans «{panel}».", "«{item}» retiré de «{panel}».", "Texte ajouté à «{panel}».", "Texte remplacé dans «{panel}»."),
    "es": ("He añadido «{item}» a «{panel}».", "He marcado «{item}» en «{panel}».", "He desmarcado «{item}» en «{panel}».", "He quitado «{item}» de «{panel}».", "He añadido el texto a «{panel}».", "He sustituido el texto de «{panel}»."),
    "it": ("Ho aggiunto «{item}» a «{panel}».", "Ho spuntato «{item}» in «{panel}».", "Ho tolto la spunta a «{item}» in «{panel}».", "Ho rimosso «{item}» da «{panel}».", "Ho aggiunto il testo a «{panel}».", "Ho sostituito il testo di «{panel}»."),
    "pt": ("Adicionei «{item}» a «{panel}».", "Marquei «{item}» em «{panel}».", "Desmarquei «{item}» em «{panel}».", "Removi «{item}» de «{panel}».", "Adicionei o texto a «{panel}».", "Substituí o texto de «{panel}»."),
}
_OVERLAY_ACTIONS186 = ("checklist_add", "checklist_check", "checklist_uncheck", "checklist_remove", "append_text", "set_text")


def _render_overlay186(tool: str, action: str, success: bool, data: Any, arguments: dict[str, Any], lang: str) -> str | None:
    """PHRASE185-OVERLAY-01: the panel, the exact item and its new state — not a
    dump of the internal snapshot with a raw epoch and an empty item list."""
    if tool != "overlay" or not success or action not in _OVERLAY_ACTIONS186 or lang not in _OVERLAY186:
        return None
    structured = data if isinstance(data, dict) else {}
    panel = str(structured.get("title") or structured.get("name") or arguments.get("name") or "").strip()
    item = str(arguments.get("item") or structured.get("item") or "").strip()
    rows = structured.get("items") if isinstance(structured.get("items"), list) else []
    index = arguments.get("index")
    if not item and isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(rows) and isinstance(rows[index], dict):
        item = str(rows[index].get("text", "") or "").strip()
    if not panel or (action.startswith("checklist_") and not item):
        return None
    return _OVERLAY186[lang][_OVERLAY_ACTIONS186.index(action)].format(item=item, panel=panel)


def _render_application186(tool: str, action: str, success: bool, data: Any, lang: str) -> str | None:
    """PHRASE185-L10N-01: launch results come from structured fields, never from a
    Russian sentence cut by a broad regex. The application name stays literal."""
    if tool != "application" or action != "open" or lang not in _APP_OPENED186 or not isinstance(data, dict):
        return None
    if success and data.get("verified") is True and str(data.get("label", "") or "").strip():
        focused = bool(data.get("focused") or data.get("verification") == "semantic_window")
        if data.get("already_running") is True and focused:
            # REC-SEMANTIC-SOURCE-01: a shown existing window is not a new launch.
            return _APP_SWITCHED187[lang].format(label=str(data["label"]).strip())
        return _APP_OPENED186[lang][1 if focused else 0].format(label=str(data["label"]).strip())
    if not success and data.get("app_not_found") and str(data.get("query", "") or "").strip():
        return _APP_NOT_FOUND186[lang].format(query=str(data["query"]).strip())
    return None


def render_action_result(tool_name: str, arguments: dict[str, Any], success: bool,
                         text: str, data: Any, code: str) -> str:
    """Localize result framing without changing values, errors or action status."""
    lang = str(code or "ru").casefold().split("-")[0]
    if lang not in SUPPORTED:
        return str(text or "")
    source = str(text or "")
    tool = str(tool_name or "")
    action = str(arguments.get("action", "status") or "status")
    if tool == "scenario" and action in {"run", "retry"}:
        return _render_scenario_result(source, data, lang)
    if isinstance(data, dict) and data.get("localized187"):
        return source  # NEO 187: producer wrote the whole sentence (result or failure) in this language
    media = _render_media_result(tool, action, success, data, lang)
    if media is not None:
        return media
    if ((tool == 'search' and action in {'files', 'content'}) or
            (tool == 'filesystem' and action == 'find')) and (
                isinstance(data, list) or isinstance(data, dict) and isinstance(data.get('results'), list)):
        # The local scanner already formats its complete result in the selected
        # language. A Cyrillic filename is user data, not Russian UI text.
        return source
    if lang == "ru" or tool in {"capture", "match", "ui", "browser_nav", "copy", "memory", "knowledge", "plugins", "neo_model", "assistant_model", "task"}:  # these tools write their own localized text
        return source
    if success and isinstance(data, dict) and data.get("localized186"):
        # The producer already rendered every service word in this language;
        # its Cyrillic event titles or places are user data, not Russian UI.
        return source
    app186 = _render_application186(tool, action, success, data, lang) or _render_overlay186(tool, action, success, data, arguments, lang)
    if app186 is not None:
        return app186
    if tool == "diagnostics" and action == "self_test":
        return source
    # These bodies can intentionally be in a language different from the UI.
    if success and (tool in {"translate", "briefing", "date_info", "web", "web_search", "game_web", "desktop_features", "game_locations", "downloads", "activity", "mini_browser", "shelf"}
                    or (tool == "screen" and action in {"analyze", "analyze_window", "translate"})
                    or (tool == "filesystem" and action == "read_text")
                    or (tool == "email" and action == "read")
                    or (tool == "clipboard" and action in {"current", "list"})):
        return source
    category = ACTION_CATEGORIES.get(action, "status")
    heading = _word(_ACTION_ROWS, category, lang)
    structured = data if isinstance(data, dict) else {}
    if structured.get("queued") and not structured.get("queue_started_immediately") and not structured.get("play_now"):
        heading = _word(_ACTION_ROWS, ACTION_CATEGORIES.get("queue_add", "status"), lang)
    if tool == "google_calendar" and category == "next":
        heading = _word(_ACTION_ROWS, "list", lang)
    pending = structured.get("outcome_pending") or structured.get("outcome_unknown") or structured.get("verified") is False
    if not success:
        lowered = source.casefold()
        error_key = "failed"
        if structured.get("cancelled") is True or structured.get("outcome") == "cancelled":
            # REC-SEMANTIC-SOURCE-01: a cancellation is its own state, not a fault.
            error_key = "cancelled"
        elif structured.get("error_kind") in {"network", "auth", "not_found", "disabled", "invalid"}: error_key = structured["error_kind"]
        elif structured.get("outcome_pending") or structured.get("outcome_unknown") or re.search(r"timeout|timed out|время ожидания|результат.*неизвест", lowered): error_key = "timeout"
        elif structured.get("invalid_tool_call") or re.search(r"параметр|invalid.*argument|обязательн", lowered): error_key = "invalid"
        elif structured.get("blocked_by_routing_firewall") or re.search(r"запрещ|отклон|заблок|permission|denied", lowered): error_key = "denied"
        elif re.search(r"не найден|не наш|не удалось найти|not found|no results", lowered): error_key = "not_found"
        elif re.search(r"авториз|oauth|токен|401|403|credential|unauthorized", lowered): error_key = "auth"
        elif re.search(r"отключен|отключён|disabled", lowered): error_key = "disabled"
        elif re.search(r"сеть|сетев|network|connection|подключен|подключён", lowered): error_key = "network"
        heading = _word(_RESULT_WORDS, error_key, lang)
    elif pending:
        heading = _word(_RESULT_WORDS, "pending", lang)
    elif structured.get("partial") or structured.get("errors"):
        heading = _word(_RESULT_WORDS, "partial", lang)
    details = _result_details(data, arguments, lang)
    # Existing explicit localized text is richer for weather/calendar/mail. Use
    # it if it was translated without a generic missing-localization fallback.
    localized = localize_runtime_text(source, lang)
    missing_markers = [word for pair in _FALLBACK.values() for word in pair]
    if success and not pending and source and localized != source and not any(marker in localized for marker in missing_markers):
        return localized
    if success and not pending and source and not re.search(r"[А-Яа-яЁё]", source):
        return source
    if not success and source:
        # Preserve exact error diagnostics, filenames, codes, provider wording.
        # A static catalogue cannot translate arbitrary third-party exceptions.
        details.append(_word(_RESULT_WORDS, "details", lang) + ": " + source)
    if not details and source:
        # Keep the specific provider output instead of the previous data-losing
        # "not localized yet" message. It may contain externally supplied text.
        details = [_word(_RESULT_WORDS, "details", lang) + ": " + source]
    return heading + (("\n" + "\n".join(details)) if details else "")

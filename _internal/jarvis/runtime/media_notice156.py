"""Native passive media notices; track metadata remains literal user data."""
from __future__ import annotations

_TEXT = {
    'ru': ('Сейчас играет ({source}): {track}.', 'Дальше в очереди:', 'Очередь: пуста.', '{who} добавил в очередь {track}{position}.', ' · позиция {n}'),
    'en': ('Now playing ({source}): {track}.', 'Up next:', 'The queue is empty.', '{who} added {track} to the queue{position}.', ' · position {n}'),
    'de': ('Jetzt läuft ({source}): {track}.', 'Als Nächstes:', 'Die Warteschlange ist leer.', '{who} hat {track} zur Warteschlange hinzugefügt{position}.', ' · Position {n}'),
    'fr': ('En cours ({source}) : {track}.', 'À suivre :', 'La file est vide.', '{who} a ajouté {track} à la file{position}.', ' · position {n}'),
    'es': ('Sonando ({source}): {track}.', 'A continuación:', 'La cola está vacía.', '{who} añadió {track} a la cola{position}.', ' · posición {n}'),
    'it': ('In riproduzione ({source}): {track}.', 'A seguire:', 'La coda è vuota.', '{who} ha aggiunto {track} alla coda{position}.', ' · posizione {n}'),
    'pt': ('Tocando agora ({source}): {track}.', 'A seguir:', 'A fila está vazia.', '{who} adicionou {track} à fila{position}.', ' · posição {n}'),
}
_UNAVAILABLE = {
    'ru': 'Очередь пока недоступна.', 'en': 'The queue is currently unavailable.',
    'de': 'Die Warteschlange ist momentan nicht verfügbar.', 'fr': 'La file est momentanément indisponible.',
    'es': 'La cola no está disponible ahora.', 'it': 'La coda non è al momento disponibile.',
    'pt': 'A fila está indisponível no momento.',
}
_NETWORK = {
    'ru': 'Обнаружена высокая задержка или нарушение работы сети.',
    'en': 'High network latency or a network disruption detected.',
    'de': 'Hohe Netzwerklatenz oder eine Netzwerkstörung erkannt.',
    'fr': 'Une forte latence ou une interruption du réseau a été détectée.',
    'es': 'Se detectó una latencia alta o una interrupción de la red.',
    'it': 'Rilevata latenza elevata o un’interruzione di rete.',
    'pt': 'Foi detetada latência elevada ou uma interrupção da rede.',
}


_LOADING_TITLE = {
    'ru': 'название загружается…', 'en': 'title loading…', 'de': 'Titel wird geladen…',
    'fr': 'titre en cours de chargement…', 'es': 'cargando el título…', 'it': 'titolo in caricamento…',
    'pt': 'a carregar o título…',
}


def queue_labels(rows, language, limit=5):
    """Next-five labels from queue rows. A row whose name is not known yet is a
    real pending track: it is shown with a loading placeholder, never dropped,
    so a non-empty queue cannot be reported as empty (MUSIC185-QUEUE-01)."""
    code = str(language).replace('_', '-').split('-')[0].casefold()
    placeholder = _LOADING_TITLE.get(code, _LOADING_TITLE['en'])
    labels = []
    for row in list(rows or []):
        if len(labels) >= max(1, int(limit)):
            break
        if not isinstance(row, dict):
            continue
        artist = str(row.get('artist', '') or '').strip()
        title = str(row.get('title', '') or row.get('query', '') or '').strip()
        label = ' — '.join(x for x in (artist, title) if x)
        if not label:
            if not str(row.get('id', '') or row.get('track_id', '') or '').strip():
                continue
            label = placeholder
        labels.append(f"{len(labels) + 1}. {label}")
    return labels


def network_notice(language):
    code = str(language).replace('_', '-').split('-')[0].casefold()
    return _NETWORK.get(code, _NETWORK['en'])


def _texts(language):
    return _TEXT.get(str(language).replace('_', '-').split('-')[0].casefold(), _TEXT['en'])


def queue_notice(labels, language):
    if labels is None:
        code = str(language).replace('_', '-').split('-')[0].casefold()
        return '\n' + _UNAVAILABLE.get(code, _UNAVAILABLE['en'])
    words = _texts(language)
    return '\n' + (words[1] + '\n' + '\n'.join(labels) if labels else words[2])


def now_playing_notice(source, track, queue, language):
    return _texts(language)[0].format(source=source, track=track) + queue


def queue_added_notice(who, track, position, language):
    words = _texts(language)
    suffix = words[4].format(n=position) if position > 0 else ''
    return words[3].format(who=who, track=track, position=suffix)

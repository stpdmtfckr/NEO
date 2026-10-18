"""Truthful queue read formatting; names are provider data, never generated."""
from jarvis.runtime.natural_commands import LANGUAGES
_LABELS=(
 ('Spotify. Сейчас играет: {track}.','Spotify: сейчас ничего не играет.','Дальше в очереди: {tracks}.','Очередь Spotify пуста.'),
 ('Spotify. Now playing: {track}.','Spotify is not playing a track.','Up next: {tracks}.','The Spotify queue is empty.'),
 ('Spotify. Aktuell: {track}.','Spotify spielt gerade keinen Titel.','Als Nächstes: {tracks}.','Die Spotify-Warteschlange ist leer.'),
 ('Spotify. En cours : {track}.','Spotify ne lit aucun titre.','Ensuite : {tracks}.','La file Spotify est vide.'),
 ('Spotify. Suena: {track}.','Spotify no está reproduciendo ninguna pista.','A continuación: {tracks}.','La cola de Spotify está vacía.'),
 ('Spotify. In riproduzione: {track}.','Spotify non sta riproducendo alcun brano.','A seguire: {tracks}.','La coda di Spotify è vuota.'),
 ('Spotify. A tocar: {track}.','O Spotify não está a reproduzir nenhuma faixa.','A seguir: {tracks}.','A fila do Spotify está vazia.'),
)
def format_spotify_queue157(data, code):
    words=_LABELS[LANGUAGES.index(code if code in LANGUAGES else 'ru')]
    def label(row):return ' — '.join(str(row.get(k,'')).strip() for k in ('artist','title') if str(row.get(k,'')).strip())
    current=label(data.get('current') or {})
    tracks=[label(x) for x in (data.get('queue') or [])[:5] if isinstance(x,dict)]
    tracks=[x for x in tracks if x]
    return (words[0].format(track=current) if current else words[1])+' '+(words[2].format(tracks='; '.join(tracks)) if tracks else words[3])

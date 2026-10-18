"""NEO 184: translation with a local model (TranslateGemma) or SearchAPI.io.

* When the local translation model is installed, translation runs on this computer
  through Ollama: no internet, no SearchAPI requests.
* When it is missing, NEO asks once whether to download it. «Да» starts the download in
  the background and NEO reports its progress in the chat; the requested phrase is
  translated as soon as the model is ready. «Нет» means SearchAPI.io from now on: NEO
  warns once that this uses the free daily SearchAPI requests and never asks or warns again.
* Settings → Перевод installs or deletes the model at any time and holds the options.

The text to translate is data: it is never executed and never logged unless detailed
logging was switched on by the user.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MODEL = 'translategemma:4b'
# Approximate download sizes (GB) of the published Ollama tags.
MODEL_SIZES_GB = {'translategemma:4b': 3.3, 'translategemma:12b': 8.1, 'translategemma:27b': 17.0}
MODEL_CHOICES = ('translategemma:4b', 'translategemma:12b')
ENGINES = ('auto', 'local', 'searchapi')
_LANGS = ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt')
_CHUNK = 1400                      # characters per request to the model (paragraph-aligned)
_ASK_SECONDS = 300                 # how long «да/нет» answers the install question

DEFAULTS: dict[str, Any] = {
    'engine': 'auto',              # auto: the local model when installed, otherwise SearchAPI
    'model': DEFAULT_MODEL,
    'offer_install': True,         # ask once when the model is missing
    'searchapi_notice_shown': False,
    'fallback_to_searchapi': True,  # the local model did not answer -> SearchAPI (shown in the answer)
    'default_target': '',          # '' = automatic (interface language / English)
    'keep_alive_minutes': 2,       # the model leaves memory this long after the last translation
    'show_engine': True,           # the answer says what translated it
    'max_chars': 6000,
}


def _code(code: str) -> str:
    code = str(code or 'ru').casefold().split('-')[0]
    return code if code in _LANGS else 'en'


_TEXT: dict[str, tuple[str, ...]] = {
    'ask': (
        'Модель для перевода ({model}, около {size} ГБ) не установлена. Установить её? С ней перевод работает без интернета и не тратит запросы SearchAPI. Ответьте «да» или «нет».',
        'The translation model ({model}, about {size} GB) is not installed. Install it? With it, translation works offline and uses no SearchAPI requests. Answer “yes” or “no”.',
        'Das Übersetzungsmodell ({model}, etwa {size} GB) ist nicht installiert. Soll ich es installieren? Damit funktioniert die Übersetzung offline und verbraucht keine SearchAPI-Anfragen. Antworte „ja“ oder „nein“.',
        'Le modèle de traduction ({model}, environ {size} Go) n’est pas installé. L’installer ? Avec lui, la traduction fonctionne hors ligne et n’utilise aucune requête SearchAPI. Répondez « oui » ou « non ».',
        'El modelo de traducción ({model}, unos {size} GB) no está instalado. ¿Lo instalo? Con él la traducción funciona sin internet y no gasta consultas de SearchAPI. Responde «sí» o «no».',
        'Il modello di traduzione ({model}, circa {size} GB) non è installato. Lo installo? Con esso la traduzione funziona offline e non usa richieste SearchAPI. Rispondi «sì» o «no».',
        'O modelo de tradução ({model}, cerca de {size} GB) não está instalado. Instalo-o? Com ele a tradução funciona sem internet e não gasta pedidos da SearchAPI. Responde «sim» ou «não».'),
    'start': (
        'Начинаю загрузку модели перевода {model} (около {size} ГБ). Буду сообщать о прогрессе; NEO можно пользоваться как обычно.',
        'Downloading the translation model {model} (about {size} GB). I will report the progress; you can keep using NEO.',
        'Ich lade das Übersetzungsmodell {model} herunter (etwa {size} GB). Ich melde den Fortschritt; du kannst NEO weiter benutzen.',
        'Je télécharge le modèle de traduction {model} (environ {size} Go). Je vous tiendrai au courant ; NEO reste utilisable.',
        'Descargando el modelo de traducción {model} (unos {size} GB). Te iré informando del progreso; puedes seguir usando NEO.',
        'Scarico il modello di traduzione {model} (circa {size} GB). Ti aggiornerò sui progressi; puoi continuare a usare NEO.',
        'A transferir o modelo de tradução {model} (cerca de {size} GB). Vou informando o progresso; podes continuar a usar o NEO.'),
    'progress': (
        'Загрузка модели перевода: {percent}% ({done} из {total} ГБ).',
        'Translation model download: {percent}% ({done} of {total} GB).',
        'Download des Übersetzungsmodells: {percent}% ({done} von {total} GB).',
        'Téléchargement du modèle de traduction : {percent} % ({done} sur {total} Go).',
        'Descarga del modelo de traducción: {percent}% ({done} de {total} GB).',
        'Download del modello di traduzione: {percent}% ({done} di {total} GB).',
        'Transferência do modelo de tradução: {percent}% ({done} de {total} GB).'),
    'done': (
        'Модель перевода установлена. Теперь перевожу на этом компьютере, без интернета.',
        'The translation model is installed. Translation now runs on this computer, offline.',
        'Das Übersetzungsmodell ist installiert. Übersetzt wird jetzt auf diesem Computer, offline.',
        'Le modèle de traduction est installé. La traduction se fait désormais sur cet ordinateur, hors ligne.',
        'El modelo de traducción está instalado. Ahora traduzco en este ordenador, sin internet.',
        'Il modello di traduzione è installato. Ora traduco su questo computer, offline.',
        'O modelo de tradução está instalado. Agora traduzo neste computador, sem internet.'),
    'queued_result': (
        'Перевод, который вы просили:\n{answer}', 'The translation you asked for:\n{answer}',
        'Die gewünschte Übersetzung:\n{answer}', 'La traduction demandée :\n{answer}',
        'La traducción que pediste:\n{answer}', 'La traduzione che hai chiesto:\n{answer}',
        'A tradução que pediste:\n{answer}'),
    'failed': (
        'Не удалось загрузить модель перевода: {error}. Можно попробовать снова в Настройках → Перевод.',
        'Could not download the translation model: {error}. You can try again in Settings → Translation.',
        'Das Übersetzungsmodell konnte nicht geladen werden: {error}. Erneut versuchen unter Einstellungen → Übersetzung.',
        'Impossible de télécharger le modèle de traduction : {error}. Réessayez dans Paramètres → Traduction.',
        'No se pudo descargar el modelo de traducción: {error}. Puedes reintentarlo en Ajustes → Traducción.',
        'Impossibile scaricare il modello di traduzione: {error}. Riprova in Impostazioni → Traduzione.',
        'Não foi possível transferir o modelo de tradução: {error}. Podes tentar de novo em Definições → Tradução.'),
    'cancelled': (
        'Загрузка модели перевода отменена.', 'Translation model download cancelled.',
        'Download des Übersetzungsmodells abgebrochen.', 'Téléchargement du modèle de traduction annulé.',
        'Descarga del modelo de traducción cancelada.', 'Download del modello di traduzione annullato.',
        'Transferência do modelo de tradução cancelada.'),
    'no_ollama': (
        'для локального перевода нужен Ollama, а он не запущен или не установлен',
        'local translation needs Ollama, which is not running or not installed',
        'die lokale Übersetzung braucht Ollama, das nicht läuft oder nicht installiert ist',
        'la traduction locale a besoin d’Ollama, qui n’est pas lancé ou pas installé',
        'la traducción local necesita Ollama, que no está en ejecución o no está instalado',
        'la traduzione locale richiede Ollama, che non è avviato o non è installato',
        'a tradução local precisa do Ollama, que não está a correr ou não está instalado'),
    'declined': (
        'Хорошо, буду переводить через SearchAPI. Учтите: каждый перевод расходует бесплатные ежедневные запросы SearchAPI. Больше спрашивать об этом не буду — модель можно установить в любой момент в Настройках → Перевод.',
        'OK, I will translate through SearchAPI. Note: every translation uses your free daily SearchAPI requests. I won’t ask again — you can install the model any time in Settings → Translation.',
        'Gut, ich übersetze über SearchAPI. Beachte: Jede Übersetzung verbraucht deine kostenlosen täglichen SearchAPI-Anfragen. Ich frage nicht mehr — das Modell lässt sich jederzeit unter Einstellungen → Übersetzung installieren.',
        'D’accord, je traduirai via SearchAPI. Attention : chaque traduction consomme vos requêtes SearchAPI gratuites quotidiennes. Je ne le redemanderai pas — le modèle s’installe à tout moment dans Paramètres → Traduction.',
        'De acuerdo, traduciré con SearchAPI. Ten en cuenta que cada traducción gasta tus consultas gratuitas diarias de SearchAPI. No volveré a preguntar: puedes instalar el modelo cuando quieras en Ajustes → Traducción.',
        'Va bene, tradurrò tramite SearchAPI. Nota: ogni traduzione consuma le richieste gratuite giornaliere di SearchAPI. Non lo chiederò più: puoi installare il modello in qualsiasi momento in Impostazioni → Traduzione.',
        'Está bem, vou traduzir pela SearchAPI. Atenção: cada tradução gasta os pedidos gratuitos diários da SearchAPI. Não volto a perguntar — podes instalar o modelo quando quiseres em Definições → Tradução.'),
    'downloading_searchapi': (
        'Модель перевода ещё загружается ({percent}%), поэтому этот перевод сделан через SearchAPI.',
        'The translation model is still downloading ({percent}%), so this translation used SearchAPI.',
        'Das Übersetzungsmodell wird noch geladen ({percent}%), daher lief diese Übersetzung über SearchAPI.',
        'Le modèle de traduction se télécharge encore ({percent} %), cette traduction passe donc par SearchAPI.',
        'El modelo de traducción aún se está descargando ({percent}%), así que esta traducción usó SearchAPI.',
        'Il modello di traduzione si sta ancora scaricando ({percent}%), quindi questa traduzione ha usato SearchAPI.',
        'O modelo de tradução ainda está a transferir ({percent}%), por isso esta tradução usou a SearchAPI.'),
    'downloading_wait': (
        'Модель перевода ещё загружается ({percent}%). Переведу, как только она будет готова.',
        'The translation model is still downloading ({percent}%). I will translate as soon as it is ready.',
        'Das Übersetzungsmodell wird noch geladen ({percent}%). Ich übersetze, sobald es bereit ist.',
        'Le modèle de traduction se télécharge encore ({percent} %). Je traduirai dès qu’il sera prêt.',
        'El modelo de traducción aún se está descargando ({percent}%). Traduciré en cuanto esté listo.',
        'Il modello di traduzione si sta ancora scaricando ({percent}%). Tradurrò appena sarà pronto.',
        'O modelo de tradução ainda está a transferir ({percent}%). Traduzo assim que estiver pronto.'),
    'local_missing': (
        'Модель перевода не установлена, а в настройках выбран только локальный перевод. Установите её в Настройках → Перевод или скажите «установи модель перевода».',
        'The translation model is not installed and only local translation is selected. Install it in Settings → Translation or say “install the translation model”.',
        'Das Übersetzungsmodell ist nicht installiert, und es ist nur lokale Übersetzung gewählt. Installiere es unter Einstellungen → Übersetzung oder sag „installiere das Übersetzungsmodell“.',
        'Le modèle de traduction n’est pas installé et seule la traduction locale est choisie. Installez-le dans Paramètres → Traduction ou dites « installe le modèle de traduction ».',
        'El modelo de traducción no está instalado y solo está elegida la traducción local. Instálalo en Ajustes → Traducción o di «instala el modelo de traducción».',
        'Il modello di traduzione non è installato ed è scelta solo la traduzione locale. Installalo in Impostazioni → Traduzione o di’ «installa il modello di traduzione».',
        'O modelo de tradução não está instalado e só a tradução local está escolhida. Instala-o em Definições → Tradução ou diz «instala o modelo de tradução».'),
    'cancelled187': ('Перевод отменён.', 'Translation cancelled.', 'Übersetzung abgebrochen.', 'Traduction annulée.',
                     'Traducción cancelada.', 'Traduzione annullata.', 'Tradução cancelada.'),
    'partial187': ('Часть текста не переведена ({n} из {total} фрагментов) — эти места оставлены в оригинале и отмечены «[…]».',
                   'Part of the text was not translated ({n} of {total} pieces); those places stay in the original, marked «[…]».',
                   'Ein Teil des Textes wurde nicht übersetzt ({n} von {total} Abschnitten); diese Stellen bleiben im Original, markiert mit «[…]».',
                   'Une partie du texte n’a pas été traduite ({n} sur {total} fragments) ; ces passages restent dans l’original, marqués «[…]».',
                   'Parte del texto no se tradujo ({n} de {total} fragmentos); esos lugares quedan en el original, marcados con «[…]».',
                   'Una parte del testo non è stata tradotta ({n} di {total} frammenti); quei punti restano in originale, segnati con «[…]».',
                   'Parte do texto não foi traduzida ({n} de {total} fragmentos); esses trechos ficam no original, marcados com «[…]».'),
    'local_failed': (
        'Локальная модель перевода не ответила: {error}.', 'The local translation model did not answer: {error}.',
        'Das lokale Übersetzungsmodell hat nicht geantwortet: {error}.', 'Le modèle de traduction local n’a pas répondu : {error}.',
        'El modelo de traducción local no respondió: {error}.', 'Il modello di traduzione locale non ha risposto: {error}.',
        'O modelo de tradução local não respondeu: {error}.'),
    'local_failed_fallback': (
        'Локальная модель перевода не ответила, поэтому перевод сделан через SearchAPI.',
        'The local translation model did not answer, so SearchAPI was used.',
        'Das lokale Übersetzungsmodell hat nicht geantwortet, daher wurde SearchAPI verwendet.',
        'Le modèle de traduction local n’a pas répondu, SearchAPI a donc été utilisé.',
        'El modelo de traducción local no respondió, así que se usó SearchAPI.',
        'Il modello di traduzione locale non ha risposto, quindi è stato usato SearchAPI.',
        'O modelo de tradução local não respondeu, por isso foi usada a SearchAPI.'),
    'label_local': (
        'Машинный перевод: локальная модель {model}, без интернета.', 'Machine translation: local model {model}, offline.',
        'Maschinelle Übersetzung: lokales Modell {model}, offline.', 'Traduction automatique : modèle local {model}, hors ligne.',
        'Traducción automática: modelo local {model}, sin internet.', 'Traduzione automatica: modello locale {model}, offline.',
        'Tradução automática: modelo local {model}, sem internet.'),
    'label_searchapi': (
        'Машинный перевод: SearchAPI.io / Google AI Mode.', 'Machine translation: SearchAPI.io / Google AI Mode.',
        'Maschinelle Übersetzung: SearchAPI.io / Google AI Mode.', 'Traduction automatique : SearchAPI.io / Google AI Mode.',
        'Traducción automática: SearchAPI.io / Google AI Mode.', 'Traduzione automatica: SearchAPI.io / Google AI Mode.',
        'Tradução automática: SearchAPI.io / Google AI Mode.'),
    'too_long': (
        'Текст слишком длинный для перевода: {n} символов, а можно до {limit}. Переведите его частями или увеличьте лимит в Настройках → Перевод.',
        'The text is too long to translate: {n} characters, the limit is {limit}. Translate it in parts or raise the limit in Settings → Translation.',
        'Der Text ist zu lang: {n} Zeichen, erlaubt sind {limit}. Übersetze ihn in Teilen oder erhöhe das Limit unter Einstellungen → Übersetzung.',
        'Le texte est trop long : {n} caractères, la limite est {limit}. Traduisez-le par parties ou augmentez la limite dans Paramètres → Traduction.',
        'El texto es demasiado largo: {n} caracteres, el límite es {limit}. Tradúcelo por partes o sube el límite en Ajustes → Traducción.',
        'Il testo è troppo lungo: {n} caratteri, il limite è {limit}. Traducilo a parti o alza il limite in Impostazioni → Traduzione.',
        'O texto é demasiado longo: {n} caracteres, o limite é {limit}. Traduz por partes ou aumenta o limite em Definições → Tradução.'),
    'already': (
        'Модель перевода уже установлена.', 'The translation model is already installed.', 'Das Übersetzungsmodell ist bereits installiert.',
        'Le modèle de traduction est déjà installé.', 'El modelo de traducción ya está instalado.', 'Il modello di traduzione è già installato.',
        'O modelo de tradução já está instalado.'),
    'busy': (
        'Модель перевода уже загружается: {percent}%.', 'The translation model is already downloading: {percent}%.',
        'Das Übersetzungsmodell wird bereits geladen: {percent}%.', 'Le modèle de traduction se télécharge déjà : {percent} %.',
        'El modelo de traducción ya se está descargando: {percent}%.', 'Il modello di traduzione si sta già scaricando: {percent}%.',
        'O modelo de tradução já está a transferir: {percent}%.'),
    'nothing_to_cancel': (
        'Модель перевода сейчас не загружается.', 'The translation model is not downloading now.', 'Das Übersetzungsmodell wird gerade nicht geladen.',
        'Le modèle de traduction n’est pas en cours de téléchargement.', 'El modelo de traducción no se está descargando ahora.',
        'Il modello di traduzione non si sta scaricando ora.', 'O modelo de tradução não está a transferir agora.'),
    'status_installed': (
        'Модель перевода {model} установлена: перевод работает без интернета.', 'The translation model {model} is installed: translation works offline.',
        'Das Übersetzungsmodell {model} ist installiert: Übersetzen geht offline.', 'Le modèle de traduction {model} est installé : la traduction fonctionne hors ligne.',
        'El modelo de traducción {model} está instalado: la traducción funciona sin internet.', 'Il modello di traduzione {model} è installato: la traduzione funziona offline.',
        'O modelo de tradução {model} está instalado: a tradução funciona sem internet.'),
    'status_missing': (
        'Модель перевода {model} не установлена: перевод идёт через SearchAPI.', 'The translation model {model} is not installed: translation uses SearchAPI.',
        'Das Übersetzungsmodell {model} ist nicht installiert: übersetzt wird über SearchAPI.', 'Le modèle de traduction {model} n’est pas installé : la traduction passe par SearchAPI.',
        'El modelo de traducción {model} no está instalado: la traducción usa SearchAPI.', 'Il modello di traduzione {model} non è installato: la traduzione usa SearchAPI.',
        'O modelo de tradução {model} não está instalado: a tradução usa a SearchAPI.'),
    'deleted': (
        'Модель перевода удалена. Перевод теперь идёт через SearchAPI.', 'The translation model was deleted. Translation now uses SearchAPI.',
        'Das Übersetzungsmodell wurde gelöscht. Übersetzt wird jetzt über SearchAPI.', 'Le modèle de traduction a été supprimé. La traduction passe désormais par SearchAPI.',
        'El modelo de traducción se eliminó. Ahora la traducción usa SearchAPI.', 'Il modello di traduzione è stato eliminato. Ora la traduzione usa SearchAPI.',
        'O modelo de tradução foi apagado. A tradução passa a usar a SearchAPI.'),
}


def t(key: str, code: str, /, **values: Any) -> str:
    return _TEXT[key][_LANGS.index(_code(code))].format(**values)


# ---- settings -----------------------------------------------------------------------------------

def setting(config: Any, key: str) -> Any:
    value = config.get(f'translation.{key}', DEFAULTS[key]) if config is not None else DEFAULTS[key]
    default = DEFAULTS[key]
    if isinstance(default, bool):
        return bool(value) if isinstance(value, (bool, int)) else default
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return str(value if value is not None else default)


def model_name(config: Any) -> str:
    name = setting(config, 'model').strip()
    return name or DEFAULT_MODEL


def engine(config: Any) -> str:
    value = setting(config, 'engine').strip().casefold()
    return value if value in ENGINES else 'auto'


def _decimal(value: float, code: str) -> str:
    text = f'{value:.1f}'
    return text if _code(code) == 'en' else text.replace('.', ',')


def size_gb(model: str, code: str = 'en') -> str:
    size = MODEL_SIZES_GB.get(str(model or '').casefold(), MODEL_SIZES_GB[DEFAULT_MODEL])
    return _decimal(size, code)


def _remember(config: Any, **values: Any) -> None:
    """Persist translation flags at once (the answer to NEO's question is a setting)."""
    def mutate(data: dict[str, Any]) -> None:
        section = data.get('translation')
        if not isinstance(section, dict):
            section = data['translation'] = {}
        section.update(values)
    try:
        config.update(mutate)
    except Exception:
        logger.exception('Translation settings could not be saved')
        try:
            section = config.data.setdefault('translation', {})
            section.update(values)
        except Exception:
            pass


def _marker_path(config: Any):
    from pathlib import Path
    root = getattr(getattr(config, 'paths', None), 'root', None)
    return Path(root) / 'bootstrap' / 'translation_model.json' if root else None


def _mark_model(config: Any, model: str, *, present: bool) -> None:
    """Remember which translation model NEO downloaded, so the uninstaller can remove it."""
    path = _marker_path(config)
    if path is None:
        return
    try:
        models: list[str] = []
        if path.exists():
            models = [str(x) for x in (json.loads(path.read_text(encoding='utf-8')) or {}).get('models', []) if x]
        models = [m for m in models if m.casefold() != model.casefold()] + ([model] if present else [])
        if not models:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'models': models, 'pulled_by': 'NEO', 'time': time.strftime('%Y-%m-%dT%H:%M:%S')}), encoding='utf-8')
    except (OSError, ValueError, AttributeError):
        logger.debug('Translation model marker not updated', exc_info=True)


def _base_url(config: Any) -> str:
    return str(config.get('ollama.base_url', 'http://localhost:11434') or 'http://localhost:11434').rstrip('/')


# ---- is the model installed? --------------------------------------------------------------------

_present_cache: dict[str, tuple[float, bool]] = {}
_present_lock = threading.Lock()


def installed(config: Any, *, fresh: bool = False, timeout: float = 1.5, model: str = '') -> bool:
    """True when the translation model is installed in the local Ollama (cached for 20 s)."""
    model = str(model or '').strip() or model_name(config)
    key = _base_url(config) + '|' + model.casefold()
    now = time.monotonic()
    with _present_lock:
        cached = _present_cache.get(key)
        if cached and not fresh and now - cached[0] < 20.0:
            return cached[1]
    from jarvis.ollama_runtime import is_local_ollama_url, list_ollama_models, _model_keys
    present = False
    if is_local_ollama_url(_base_url(config)):
        try:
            present = bool(_model_keys(model) & list_ollama_models(_base_url(config), timeout=timeout))
        except Exception:
            present = False
    with _present_lock:
        _present_cache[key] = (time.monotonic(), present)
    return present


def forget_presence() -> None:
    with _present_lock:
        _present_cache.clear()


# ---- the install question --------------------------------------------------------------------------

@dataclass(slots=True)
class Request:
    text: str
    target: str
    source: str = ''
    code: str = 'ru'


_ask_lock = threading.Lock()
_ask: dict[str, Any] = {'request': None, 'at': 0.0, 'owner_scope': '', 'question_token': None}


def ask(request: Request) -> None:
    from jarvis.runtime.conversation_journal import current_scope
    with _ask_lock:
        _ask['request'], _ask['at'] = request, time.monotonic()
        _ask['owner_scope'], _ask['question_token'] = current_scope(), object()


def pending() -> Request | None:
    with _ask_lock:
        request = _ask['request']
        if request is not None and time.monotonic() - _ask['at'] <= _ASK_SECONDS:
            return request
        _ask['request'] = None
        return None


def forget_question() -> None:
    with _ask_lock:
        _ask['request'] = None


def owned_question_token() -> object | None:
    """Snapshot a live install question created in this conversation."""
    from jarvis.runtime.conversation_journal import current_scope
    with _ask_lock:
        if (_ask['owner_scope'] == current_scope() and _ask['request'] is not None
                and time.monotonic() - _ask['at'] <= _ASK_SECONDS):
            return _ask['question_token']
    return None


def forget_if_owned(token: object | None) -> bool:
    """Clear only the same owned install question, never a replacement."""
    if token is None:
        return False
    from jarvis.runtime.conversation_journal import current_scope
    with _ask_lock:
        if (_ask['owner_scope'] != current_scope() or _ask['question_token'] is not token
                or _ask['request'] is None):
            return False
        _ask['request'] = None
        return True


_YES = re.compile(
    r'^(?:да|ага|угу|давай|конечно|хорошо|ок(?:ей)?|устан(?:ови|овить|авливай)|скачай|скачивай|загрузи|качай|'
    r'yes|yeah|yep|sure|ok(?:ay)?|please|install(?:\s+it)?|download(?:\s+it)?|ja|gerne|installier\w*|lade\s+(?:es\s+)?herunter|'
    r'oui|d.accord|installe(?:-le)?|t[eé]l[eé]charge(?:-le)?|s[ií]|claro|vale|inst[aá]lalo|desc[aá]rgalo|certo|installalo|scaricalo|sim|instala(?:-o)?|transfere(?:-o)?)'
    r'(?:[\s,]+(?:да|давай|пожалуйста|please|bitte|s.il\s+te\s+pla[iî]t|por\s+favor|per\s+favore|it|его|её|ее|модель|the\s+model))*$', re.I)
_NO = re.compile(
    r'^(?:нет|неа|не\s+надо|не\s+нужно|не\s+устанавливай|не\s+хочу|отмена|no|nope|no\s+thanks|don.t|nein|nicht|non|pas\s+besoin|n[aã]o|no\s+gracias|no\s+grazie)'
    r'(?:[\s,]+(?:спасибо|не\s+надо|thanks|danke|merci|gracias|grazie|obrigad[oa]))*$', re.I)
# NEO 187 HREQ-014: «да да», «да, верно», «это то, что я имел в виду» agree;
# «да нет», «не знаю» and negations never become consent.
from jarvis.runtime.confirm_words187 import guard_no as _guard_no187, guard_yes as _guard_yes187
_YES, _NO = _guard_yes187(_YES), _guard_no187(_NO)


def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', str(text or '').casefold().replace('ё', 'е')).strip(' .,!?:;¿¡«»"\'')


def answer_question(agent: Any, text: str, code: str) -> tuple[str, str] | None:
    """(reply, detail) when ``text`` answers «Установить модель перевода?», otherwise None.

    Any other phrase drops the question (it was not answered: NEO may ask again next time)."""
    request = pending()
    if request is None:
        return None
    low = _norm(text)
    config = agent.config
    if _YES.match(low):
        forget_question()
        _remember(config, offer_install=False)
        message = installer().start(config, code, after=request)
        return message, 'translation.install_accepted'
    if _NO.match(low):
        forget_question()
        _remember(config, offer_install=False, searchapi_notice_shown=True)
        result = translate_with_searchapi(config, request)
        notice = t('declined', code)
        return (notice + '\n\n' + result.text if result.text else notice), 'translation.install_declined'
    forget_question()
    return None


# ---- voice commands about the model ---------------------------------------------------------------

_MODEL_WORDS = (r'(?:модел\w*\s+(?:для\s+)?перевод\w*|переводчик\w*|translation\s+model|translator\s+model|'
                r'[üu]bersetzungsmodell\w*|mod[eè]le\s+de\s+traduction|modelo\s+de\s+traducci[oó]n|modello\s+di\s+traduzione|modelo\s+de\s+tradu[cç][aã]o)')
_INSTALL_CMD = re.compile(
    r'^(?:установи|скачай|загрузи|поставь|install|download|installiere|lade|installe|t[eé]l[eé]charge|instala|descarga|installa|scarica|transfere)\s+'
    r'(?:(?:the|das|le|el|il|o|мне)\s+)?(?:локальн\w+\s+|local\s+)?' + _MODEL_WORDS + r'(?:\s+herunter)?$', re.I)
_CANCEL_CMD = re.compile(
    r'^(?:отмени|останови|прекрати|cancel|stop|abbrechen|brich|annule|arr[eê]te|cancela|det[eé]n|annulla|ferma|para)\s+'
    r'(?:загрузк\w+|скачивани\w+|the\s+download\s+of|download\s+of|den\s+download\s+des|le\s+t[eé]l[eé]chargement\s+du|la\s+descarga\s+del|il\s+download\s+del|a\s+transfer[eê]ncia\s+do)\s+'
    r'(?:(?:the)\s+)?' + _MODEL_WORDS + r'(?:\s+ab)?$', re.I)
_STATUS_CMD = re.compile(
    r'^(?:установлена\s+ли\s+|есть\s+ли\s+)' + _MODEL_WORDS + r'$|^(?:статус|состояние)\s+' + _MODEL_WORDS + r'$|'
    r'^(?:is\s+the\s+)?' + _MODEL_WORDS + r'\s+installed$|^translation\s+model\s+status$|^ist\s+das\s+' + _MODEL_WORDS + r'\s+installiert$|'
    r'^le\s+' + _MODEL_WORDS + r'\s+est.il\s+install[eé]$|^(?:est[aá]\s+instalado\s+el\s+)' + _MODEL_WORDS + r'$|'
    r'^(?:il\s+)?' + _MODEL_WORDS + r'\s+[eè]\s+installato$|^(?:o\s+)?' + _MODEL_WORDS + r'\s+est[aá]\s+instalado$', re.I)


def handle_command(agent: Any, text: str, code: str) -> tuple[str, str] | None:
    """«установи модель перевода», «отмени загрузку модели перевода», «модель перевода установлена?»."""
    low = _norm(text)
    if not low or not re.search(r'перевод|transla|[üu]bersetz|tradu', low):
        return None
    config = agent.config
    if _INSTALL_CMD.match(low):
        _remember(config, offer_install=False)
        return installer().start(config, code), 'translation.install'
    if _CANCEL_CMD.match(low):
        return (t('cancelled', code) if installer().cancel() else t('nothing_to_cancel', code)), 'translation.install_cancel'
    if _STATUS_CMD.match(low):
        snap = installer().snapshot()
        if snap['state'] == 'downloading':
            return t('busy', code, percent=snap['percent']), 'translation.status'
        key = 'status_installed' if installed(config, fresh=True) else 'status_missing'
        return t(key, code, model=model_name(config)), 'translation.status'
    return None


# ---- download / delete ---------------------------------------------------------------------------------

class Installer:
    """One background download of the translation model at a time, with progress events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[str] = deque(maxlen=32)
        self._state = 'idle'
        self._percent = 0
        self._done_bytes = 0
        self._total_bytes = 0
        self._error = ''
        self._model = ''
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._after: Request | None = None
        self._response: Any = None
        self.listeners: list[Callable[[dict[str, Any]], None]] = []

    # -- state
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {'state': self._state, 'percent': self._percent, 'done': self._done_bytes, 'total': self._total_bytes,
                    'error': self._error, 'model': self._model}

    def busy(self) -> bool:
        with self._lock:
            return self._state == 'downloading'

    def drain(self, limit: int = 16) -> list[str]:
        out: list[str] = []
        with self._lock:
            while self._events and len(out) < limit:
                out.append(self._events.popleft())
        return out

    def _emit(self, text: str) -> None:
        if text:
            with self._lock:
                self._events.append(text)

    def _set(self, **values: Any) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self, '_' + key, value)
        snap = self.snapshot()
        for listener in list(self.listeners):
            try:
                listener(snap)
            except Exception:
                logger.debug('Translation installer listener failed', exc_info=True)

    def queue_after(self, request: Request) -> None:
        with self._lock:
            self._after = request

    # -- actions
    def start(self, config: Any, code: str, *, after: Request | None = None, announce: bool = True, model: str = '') -> str:
        model = str(model or '').strip() or model_name(config)
        with self._lock:
            if self._state == 'downloading':
                if after is not None:
                    self._after = after
                return t('busy', code, percent=self._percent)
        if installed(config, fresh=True, model=model):
            if after is not None:
                result = translate_local(config, after)
                if result.ok:
                    return t('already', code) + '\n\n' + result.text
            return t('already', code)
        with self._lock:
            self._state, self._percent, self._done_bytes, self._total_bytes = 'downloading', 0, 0, 0
            self._error, self._model, self._after = '', model, after
            self._cancel.clear()
        self._thread = threading.Thread(target=self._run, args=(config, code, model, announce), name='NEO-TranslationModel', daemon=True)
        self._thread.start()
        return t('start', code, model=model, size=size_gb(model, code))

    def cancel(self) -> bool:
        with self._lock:
            if self._state != 'downloading':
                return False
            self._cancel.set()
            response = self._response
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        return True

    def _run(self, config: Any, code: str, model: str, announce: bool) -> None:
        import requests
        from jarvis.ollama_runtime import ensure_ollama_running
        base = _base_url(config)
        try:
            ready = ensure_ollama_running(base, timeout=float(config.get('ollama.startup_timeout_seconds', 20) or 20))
            if not ready.get('ready'):
                raise RuntimeError(t('no_ollama', code))
            reported = 0
            step = 25
            with requests.post(f'{base}/api/pull', json={'model': model, 'stream': True}, stream=True,
                               timeout=(10, 600)) as response:
                with self._lock:
                    self._response = response
                response.raise_for_status()
                totals: dict[str, int] = {}
                done: dict[str, int] = {}
                for line in response.iter_lines():
                    if self._cancel.is_set():
                        raise _Cancelled()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get('error'):
                        raise RuntimeError(str(event['error'])[:200])
                    digest = str(event.get('digest', '') or '')
                    if digest and event.get('total'):
                        totals[digest] = int(event.get('total') or 0)
                        done[digest] = int(event.get('completed') or 0)
                        total, completed = sum(totals.values()), sum(done.values())
                        percent = max(0, min(100, int(completed * 100 / total))) if total else 0
                        self._set(percent=percent, done_bytes=completed, total_bytes=total)
                        if announce and percent >= reported + step and percent < 100:
                            reported = percent - percent % step
                            self._emit(t('progress', code, percent=reported, done=_gb(completed, code), total=_gb(total, code)))
            if self._cancel.is_set():
                raise _Cancelled()
            forget_presence()
            if not installed(config, fresh=True, timeout=4.0, model=model):
                raise RuntimeError('Ollama did not list the model after the download')
            self._set(state='done', percent=100)
            # The downloaded model is the translation model from now on.
            _remember(config, offer_install=False, model=model)
            _mark_model(config, model, present=True)
            self._emit(t('done', code))
            with self._lock:
                after, self._after = self._after, None
            if after is not None:
                result = translate_local(config, after)
                if not result.ok and setting(config, 'fallback_to_searchapi'):
                    result = translate_with_searchapi(config, after)
                if result.text:
                    self._emit(t('queued_result', code, answer=result.text))
        except _Cancelled:
            self._set(state='cancelled')
            self._emit(t('cancelled', code))
        except Exception as exc:
            if self._cancel.is_set():
                self._set(state='cancelled')
                self._emit(t('cancelled', code))
            else:
                from jarvis.runtime.diagnostics import safe_error
                error = safe_error(exc)[:200]
                self._set(state='error', error=error)
                self._emit(t('failed', code, error=error))
                logger.info('Translation model download failed: %s', error)
        finally:
            with self._lock:
                self._response = None
                if self._state == 'downloading':
                    self._state = 'idle'
            snap = self.snapshot()
            for listener in list(self.listeners):
                try:
                    listener(snap)
                except Exception:
                    pass


class _Cancelled(Exception):
    pass


def _gb(value: int, code: str = 'en') -> str:
    return _decimal(value / 1e9, code)


_INSTALLER: list[Installer | None] = [None]


def installer() -> Installer:
    if _INSTALLER[0] is None:
        _INSTALLER[0] = Installer()
    return _INSTALLER[0]


def delete_model(config: Any, model: str = '') -> tuple[bool, str]:
    """Delete the translation model from the local Ollama: (ok, error)."""
    import requests
    from jarvis.ollama_runtime import is_local_ollama_url
    base = _base_url(config)
    if not is_local_ollama_url(base):
        return False, 'remote endpoint'
    if installer().busy():
        installer().cancel()
    model = str(model or '').strip() or model_name(config)
    try:
        requests.post(f'{base}/api/generate', json={'model': model, 'keep_alive': 0}, timeout=3)
    except Exception:
        pass
    try:
        response = requests.delete(f'{base}/api/delete', json={'model': model}, timeout=20)
        if response.status_code != 404:
            response.raise_for_status()
    except Exception as exc:
        from jarvis.runtime.diagnostics import safe_error
        return False, safe_error(exc)[:200]
    forget_presence()
    _mark_model(config, model, present=False)
    return True, ''


# ---- translation -------------------------------------------------------------------------------------

@dataclass(slots=True)
class Result:
    ok: bool
    text: str
    translated: str = ''
    engine: str = ''
    data: dict[str, Any] | None = None


_EXTRA_NAMES = {
    'uk': 'Ukrainian', 'be': 'Belarusian', 'kk': 'Kazakh', 'zh': 'Chinese', 'ja': 'Japanese', 'ko': 'Korean',
    'ar': 'Arabic', 'he': 'Hebrew', 'hi': 'Hindi', 'el': 'Greek', 'th': 'Thai', 'hy': 'Armenian', 'ka': 'Georgian',
    'vi': 'Vietnamese', 'id': 'Indonesian', 'az': 'Azerbaijani', 'no': 'Norwegian', 'nl': 'Dutch',
}


def english_name(code: str) -> str:
    code = str(code or '').casefold()
    if code in _EXTRA_NAMES:
        return _EXTRA_NAMES[code]
    from jarvis.integrations.local_translation import LANGUAGES
    name = next((x for x in LANGUAGES.get(code, ()) if x.isascii() and len(x) > 3 and ' ' not in x and '-' not in x), '')
    return name.capitalize() if name else code


def detect_source(text: str, ui_language: str = 'ru') -> str:
    """Offline script-based hint for the source language (the model needs one)."""
    sample = str(text or '')
    if re.search(r'[぀-ヿ]', sample):
        return 'ja'
    if re.search(r'[가-힯]', sample):
        return 'ko'
    if re.search(r'[一-鿿]', sample):
        return 'zh'
    for code, pattern in (('ar', r'[؀-ۿ]'), ('he', r'[֐-׿]'), ('hi', r'[ऀ-ॿ]'), ('el', r'[Ͱ-Ͽ]'),
                          ('th', r'[฀-๿]'), ('hy', r'[԰-֏]'), ('ka', r'[Ⴀ-ჿ]')):
        if re.search(pattern, sample):
            return code
    if re.search(r'[а-яё]', sample, re.I):
        if re.search(r'[іїєґ]', sample, re.I):
            return 'uk'
        if re.search(r'ў', sample, re.I):
            return 'be'
        if re.search(r'[әғқңөұүһ]', sample, re.I):
            return 'kk'
        return 'ru'
    from jarvis.integrations.local_translation import _detect_builtin_language
    return _detect_builtin_language(sample, ui_language)


def prompt(text: str, source: str, target: str) -> str:
    """The TranslateGemma prompt (one user message, two blank lines before the text)."""
    src, dst = english_name(source), english_name(target)
    return (f'You are a professional {src} ({source}) to {dst} ({target}) translator. Your goal is to accurately convey the meaning '
            f'and nuances of the original {src} text while adhering to {dst} grammar, vocabulary, and cultural sensitivities.\n'
            f'Produce only the {dst} translation, without any additional explanations or commentary. '
            f'Please translate the following {src} text into {dst}:\n\n\n{text}')


def _chunks(text: str, size: int = _CHUNK) -> list[str]:
    """Paragraph-aligned pieces; a long paragraph is cut at sentence ends."""
    pieces: list[str] = []
    for paragraph in re.split(r'(\n\s*\n)', text):
        if not paragraph:
            continue
        if pieces and len(pieces[-1]) + len(paragraph) <= size:
            pieces[-1] += paragraph
            continue
        while len(paragraph) > size:
            cut = max(paragraph.rfind('. ', 0, size), paragraph.rfind('! ', 0, size), paragraph.rfind('? ', 0, size), paragraph.rfind('\n', 0, size))
            cut = cut + 1 if cut > size // 3 else size
            pieces.append(paragraph[:cut])
            paragraph = paragraph[cut:]
        pieces.append(paragraph)
    return [p for p in pieces if p]


def _clean(output: str, original: str) -> str:
    output = re.sub(r'<think\b[^>]*>.*?</think\s*>', '', str(output or ''), flags=re.S | re.I).strip()
    if len(output) >= 2 and output[0] == output[-1] == '"' and not original.strip().startswith('"'):
        output = output[1:-1].strip()
    return output


def _generate(config: Any, model: str, text: str, source: str, target: str, session: Any = None,
              *, base_url: str | None = None) -> str:
    import requests
    from jarvis.ollama_client import _acquire_generation_slot, _release_generation_slot
    keep = max(0, min(60, setting(config, 'keep_alive_minutes')))
    options: dict[str, Any] = {'temperature': 0.0, 'num_ctx': 4096,
                               'num_predict': max(64, min(2048, int(len(text) * 1.5) + 64))}
    try:
        options['num_thread'] = max(1, min(16, int(config.get('ollama.num_thread', 4) or 4)))
    except (TypeError, ValueError):
        pass
    payload = {'model': model, 'prompt': prompt(text, source, target), 'stream': False, 'think': False,
               'keep_alive': f'{keep}m', 'options': options}
    http = session or requests
    _acquire_generation_slot()
    try:
        endpoint = base_url if base_url is not None else _base_url(config)
        response = http.post(f'{endpoint}/api/generate', json=payload,
                             timeout=(5, float(config.get('local_chat.timeout_seconds', 120) or 120)))
        response.raise_for_status()
        data = response.json() if response.content else {}
    finally:
        _release_generation_slot()
    if isinstance(data, dict) and data.get('error'):
        raise RuntimeError(str(data['error'])[:200])
    return _clean(str((data or {}).get('response', '') or ''), text)


def translate_local(config: Any, request: Request, *, session: Any = None) -> Result:
    # HIST-040: one immutable request context — every piece goes to the same
    # endpoint with the same model, and the result is labelled with exactly them.
    model = model_name(config)
    endpoint = _base_url(config)
    source = request.source or detect_source(request.text, str(config.get('app.language', 'ru') or 'ru'))
    if source == request.target:
        return Result(True, _answer(config, request, request.text, 'identity', source), request.text, 'identity',
                      {'provider': 'identity', 'source': source, 'target': request.target})
    started = time.perf_counter()
    pieces = _chunks(request.text)
    try:
        parts = [_generate(config, model, piece, source, request.target, session, base_url=endpoint) for piece in pieces]
    except Exception as exc:
        if {cls.__name__ for cls in type(exc).__mro__} & {'OperationCancelled', 'CancelledError'}:
            # HIST-039: a cancelled request ends here; it never becomes a remote fallback.
            return Result(False, t('cancelled187', request.code), '', 'local', {'cancelled': True, 'outcome': 'cancelled'})
        from jarvis.runtime.diagnostics import safe_error
        error = safe_error(exc)[:200]
        logger.info('Local translation failed: %s', error)
        return Result(False, t('local_failed', request.code, error=error), '', 'local', {'error': error})
    # HIST-041: an empty answer for a real piece is not silently dropped — the piece
    # stays in the original, marked, and the result is reported as partial.
    missing = [i for i, (piece, part) in enumerate(zip(pieces, parts)) if piece.strip() and not str(part or '').strip()]
    if missing and len(missing) == len([p for p in pieces if p.strip()]):
        return Result(False, t('local_failed', request.code, error='empty answer'), '', 'local', {'error': 'empty'})
    translated = '\n\n'.join(('[…] ' + pieces[i].strip()) if i in missing else str(part).strip()
                              for i, part in enumerate(parts) if pieces[i].strip())
    if not translated:
        return Result(False, t('local_failed', request.code, error='empty answer'), '', 'local', {'error': 'empty'})
    elapsed = (time.perf_counter() - started) * 1000
    logger.info('Local translation %s->%s: %d chars in, %d chars out, %.0f ms', source, request.target,
                len(request.text), len(translated), elapsed)
    data = {'provider': 'local_translategemma', 'model': model, 'endpoint': endpoint, 'source': source, 'target': request.target,
            'generated_translation': True, 'duration_ms': round(elapsed)}
    answer = _answer(config, request, translated, 'local', source, model=model)
    if missing:
        data.update(partial=True, untranslated_pieces=len(missing), pieces=len(pieces))
        answer += '\n' + t('partial187', request.code, n=len(missing), total=len(pieces))
    return Result(True, answer, translated, 'local', data)


def translate_with_searchapi(config: Any, request: Request, *, adapter: Any = None) -> Result:
    from jarvis.integrations.local_translation import SearchAPITranslationAdapter
    adapter = adapter or SearchAPITranslationAdapter(config)
    try:
        translated, data = adapter.translate(request.text, request.target, request.source)
    except ValueError as exc:
        return Result(False, str(exc), '', 'searchapi', {'error': 'invalid'})
    if not translated:
        return Result(False, searchapi_failure(config, data, request.code), '', 'searchapi', data)
    used = 'identity' if (data or {}).get('provider') == 'identity' else 'searchapi'   # HIST-042
    return Result(True, _answer(config, request, translated, used, str(data.get('source', '') or request.source)),
                  translated, used, data)


def searchapi_failure(config: Any, data: dict[str, Any], code: str) -> str:
    messages = {
        'ru': 'SearchAPI.io не вернул готовый перевод. Уточните языки или попробуйте более короткую фразу.',
        'en': 'SearchAPI.io did not return a translation. Specify both languages or try a shorter phrase.',
        'de': 'SearchAPI.io hat keine Übersetzung geliefert. Gib beide Sprachen an oder versuche einen kürzeren Satz.',
        'fr': "SearchAPI.io n'a pas fourni de traduction. Précise les deux langues ou essaie une phrase plus courte.",
        'es': 'SearchAPI.io no devolvió una traducción. Indica ambos idiomas o prueba una frase más corta.',
        'it': 'SearchAPI.io non ha restituito una traduzione. Specifica entrambe le lingue o prova una frase più breve.',
        'pt': 'SearchAPI.io não retornou uma tradução. Indique os dois idiomas ou tente uma frase mais curta.',
    }
    message = messages.get(_code(code), messages['en'])
    if (data or {}).get('error') == 'searchapi_key_missing':
        message += ' SearchAPI API key → Settings / API.'
    links = [f"{row.get('title', '')}: {row.get('url', '')}" for row in (data or {}).get('sources', [])[:3]]
    if links:
        message += '\n\n' + '\n'.join(links)
    return message


def _answer(config: Any, request: Request, translated: str, used: str, source: str = '', *, model: str = '') -> str:
    from jarvis.integrations.local_translation import _translation_answer
    code = _code(request.code)
    if code == 'ru':
        answer = _translation_answer(request.text, translated, request.target, source if request.source else '')
    else:
        answer = f'{request.text}\n→ {translated} ({request.target})'
    if setting(config, 'show_engine') and used in {'local', 'searchapi'}:
        answer += '\n' + (t('label_local', code, model=model or model_name(config)) if used == 'local' else t('label_searchapi', code))
    return answer


def translate(config: Any, request: Request, *, adapter: Any = None, session: Any = None) -> Result:
    """Translate ``request`` with the engine chosen in the settings (see the module docstring)."""
    limit = max(200, setting(config, 'max_chars'))
    if len(request.text) > limit:
        return Result(False, t('too_long', request.code, n=len(request.text), limit=limit), '', '', {'error': 'too_long'})
    # HIST-042: the same source and target language is no translation at all —
    # nothing is sent to a provider, nothing is installed, and the result says so.
    from jarvis.integrations.local_translation import _language_code
    same = _language_code(request.source) if request.source else ''
    if same and same == _language_code(request.target):
        return Result(True, _answer(config, request, request.text, 'identity', same), request.text, 'identity',
                      {'provider': 'identity', 'unchanged': True, 'same_language': True, 'source': same, 'target': same})
    chosen = engine(config)
    if chosen == 'searchapi':
        return translate_with_searchapi(config, request, adapter=adapter)
    if installed(config):
        result = translate_local(config, request, session=session)
        if result.ok or chosen == 'local' or not setting(config, 'fallback_to_searchapi') or (result.data or {}).get('cancelled'):
            if not result.ok:
                forget_presence()
            return result
        forget_presence()
        fallback = translate_with_searchapi(config, request, adapter=adapter)
        if fallback.ok:
            fallback.text += '\n' + t('local_failed_fallback', request.code)
        return fallback
    snap = installer().snapshot()
    if snap['state'] == 'downloading':
        if chosen == 'local':
            installer().queue_after(request)
            return Result(True, t('downloading_wait', request.code, percent=snap['percent']), '', 'queued', {'queued': True})
        fallback = translate_with_searchapi(config, request, adapter=adapter)
        if fallback.ok:
            fallback.text += '\n' + t('downloading_searchapi', request.code, percent=snap['percent'])
            return fallback
        installer().queue_after(request)
        return Result(True, t('downloading_wait', request.code, percent=snap['percent']), '', 'queued', {'queued': True})
    if setting(config, 'offer_install'):
        ask(request)
        return Result(True, t('ask', request.code, model=model_name(config), size=size_gb(model_name(config), request.code)), '', 'ask',
                      {'needs_answer': True})
    if chosen == 'local':
        return Result(False, t('local_missing', request.code), '', 'local', {'error': 'model_missing'})
    return translate_with_searchapi(config, request, adapter=adapter)

"""Typed, public settings shared with the desktop configuration.

Telegram supplies opaque IDs, never dotted config paths. Credentials, provider
URLs, executable paths and access-control policy are deliberately not editable.
No Qt imports or network calls are needed by this remote presentation layer.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
import re
from threading import RLock
from typing import Any

from jarvis.ui.theme_presets import THEME_PRESETS
from jarvis.integrations.telegram_setting_labels import LABELS, CHOICES, RU_OVERRIDES

LANGS = ("ru", "en", "de", "fr", "es", "it", "pt")
_LOCK = RLock()
MESSAGES = {
    "current": ("Текущее значение", "Current value", "Aktueller Wert", "Valeur actuelle", "Valor actual", "Valore attuale", "Valor atual"),
    "enter": ("Введите новое значение", "Enter a new value", "Neuen Wert eingeben", "Saisissez une nouvelle valeur", "Introduce un nuevo valor", "Inserisci un nuovo valore", "Digite um novo valor"),
    "saved": ("Сохранено; применение в окне NEO поставлено в очередь.", "Saved; application in the NEO window is queued.", "Gespeichert; die Anwendung im NEO-Fenster ist vorgemerkt.", "Enregistré ; l’application dans la fenêtre NEO est en attente.", "Guardado; la aplicación en la ventana de NEO está en cola.", "Salvato; l’applicazione nella finestra NEO è in coda.", "Salvo; a aplicação na janela do NEO está na fila."),
    "deferred": ("Сохранено. Очередь окна занята; перезапустите NEO для применения.", "Saved. The window queue is full; restart NEO to apply.", "Gespeichert. Die Fensterwarteschlange ist voll; NEO zum Anwenden neu starten.", "Enregistré. La file de la fenêtre est pleine ; redémarrez NEO pour appliquer.", "Guardado. La cola de la ventana está llena; reinicia NEO para aplicar.", "Salvato. La coda della finestra è piena; riavvia NEO per applicare.", "Salvo. A fila da janela está cheia; reinicie o NEO para aplicar."),
    "invalid": ("Значение не сохранено: проверьте формат и допустимый диапазон.", "Not saved: check the format and allowed range.", "Nicht gespeichert: Format und zulässigen Bereich prüfen.", "Non enregistré : vérifiez le format et la plage autorisée.", "No se ha guardado: comprueba el formato y el intervalo permitido.", "Non salvato: controlla il formato e l’intervallo consentito.", "Não salvo: confira o formato e o intervalo permitido."),
    "failed": ("Не удалось сохранить настройку.", "Could not save the setting.", "Die Einstellung konnte nicht gespeichert werden.", "Impossible d’enregistrer ce paramètre.", "No se pudo guardar el ajuste.", "Impossibile salvare l’impostazione.", "Não foi possível salvar a configuração."),
    "on": ("Включено", "On", "Ein", "Activé", "Activado", "Attivo", "Ativado"),
    "off": ("Выключено", "Off", "Aus", "Désactivé", "Desactivado", "Disattivo", "Desativado"),
    "back": ("Назад", "Back", "Zurück", "Retour", "Volver", "Indietro", "Voltar"),
    "lines": ("По одному значению на строку; «-» очищает список.", "One value per line; '-' clears the list.", "Ein Wert pro Zeile; '-' leert die Liste.", "Une valeur par ligne ; « - » vide la liste.", "Un valor por línea; '-' vacía la lista.", "Un valore per riga; '-' svuota l’elenco.", "Um valor por linha; '-' limpa a lista."),
    "empty": ("Пусто", "Empty", "Leer", "Vide", "Vacío", "Vuoto", "Vazio"),
}


def message(key: str, code: str) -> str:
    return MESSAGES[key][LANGS.index(code) if code in LANGS else 1]


@lru_cache(maxsize=2)
def _catalog(code: str) -> dict[str, str]:
    try:
        path = Path(__file__).resolve().parents[1] / "locales" / "ui" / (code + ".json")
        return json.loads(path.read_text(encoding="utf-8")).get("strings", {})
    except (OSError, ValueError, TypeError):
        return {}


@dataclass(frozen=True)
class Setting:
    id: str
    group: str
    path: str
    title: str
    english: str
    kind: str
    default: Any
    minimum: float = 0
    maximum: float = 100
    choices: tuple[Any, ...] = ()

    def label(self, code: str) -> str:
        if code == "ru":
            return RU_OVERRIDES.get(self.title, self.title)
        native = LABELS.get(self.title)
        if native and code in LANGS[1:]:
            return native[LANGS.index(code) - 1]
        return _catalog(code).get(self.title, self.english)

    def parse(self, value: Any) -> Any:
        if self.kind == "bool":
            if value not in ("0", "1"):
                raise ValueError("boolean")
            return value == "1"
        if self.kind == "enum":
            if not isinstance(value, str) or not re.fullmatch(r"\d{1,3}", value):
                raise ValueError("choice")
            index = int(value)
            if not 0 <= index < len(self.choices):
                raise ValueError("choice")
            return self.choices[index]
        text = str(value).strip()
        if self.kind in {"int", "float"}:
            if self.kind == "int" and not re.fullmatch(r"\d{1,5}", text):
                raise ValueError("integer")
            number = int(text) if self.kind == "int" else float(text.replace(",", "."))
            if not math.isfinite(number) or not self.minimum <= number <= self.maximum:
                raise ValueError("range")
            return number
        if self.kind == "color":
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", text):
                raise ValueError("color")
            return text.upper()
        if self.kind == "lines":
            if text == "-":
                return []
            values = list(dict.fromkeys(x.strip() for x in text.splitlines() if x.strip()))
            if len(values) > 32 or any(len(x) > 512 or any(ord(c) < 32 for c in x) for x in values):
                raise ValueError("lines")
            return values
        if self.kind == "hotkey":
            if text == "-":
                return ""
            if not re.fullmatch(r"(?:(?:Ctrl|Alt|Shift|Win)\+){1,4}(?:[A-Za-z0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Up|Down|Left|Right|Space|Home|End)", text):
                raise ValueError("hotkey")
            return text
        if self.kind == "text" and 0 < len(text) <= 128 and not any(ord(c) < 32 for c in text):
            return text
        raise ValueError("unsupported setting")


SETTINGS: dict[str, Setting] = {}


def _add(group, path, title, english, kind="bool", default=False, minimum=0, maximum=100, choices=()):
    # Stable IDs based on trusted field names; incoming paths never resolve here.
    sid = path.replace(".", "_")
    SETTINGS[sid] = Setting(sid, group, path, title, english, kind, default, minimum, maximum, tuple(choices))


_add("advanced", "app.language", "Язык", "Language", "enum", "ru", choices=LANGS)
for path, title, english, default in (
    ("ui.show_tool_details", "Показывать технические детали выполненных действий", "Show action details", False),
    ("ui.voice_window_visibility_commands_enabled", "Разрешить команды «покажи окно» / «скройся»", "Voice control of window visibility", True),
    ("routing.atlas2_enabled", "Включить Atlas 2.0 (Static → Semantic → Context → AI)", "Phrase Atlas", True),
    ("routing.semantic_enabled", "Семантический matcher для гибких формулировок", "Flexible phrase matching", True),
    ("routing.lazy_vision_enabled", "LazyVision — анализировать экран только по необходимости", "Analyze the screen only when requested", True),
    ("autoclicker.enabled", "Разрешить автокликер и голосовые команды", "Allow autoclicker commands", True),
    ("developer_mode.enabled", "Показывать отдельное окно Developer", "Developer window", False),
):
    _add("advanced", path, title, english, default=default)
_add("advanced", "routing.semantic_threshold", "Порог semantic matcher", "Phrase matching threshold", "float", .43, .3, .9)
_add("advanced", "autoclicker.cps", "Скорость", "Clicks per second", "float", 10, .5, 100)
_add("advanced", "autoclicker.button", "Кнопка мыши", "Mouse button", "enum", "left", choices=("left", "right", "middle"))
for field, title, english, default in (
    ("app_microphone_toggle", "Вкл./выкл. микрофон NEO", "Toggle NEO microphone", "Ctrl+Alt+M"),
    ("autoclicker_toggle", "Вкл./выкл. автокликер", "Toggle autoclicker", "Ctrl+Alt+C"),
    ("chat_scroll_up", "Прокрутка чата вверх", "Scroll chat up", "Ctrl+Alt+Up"),
    ("chat_scroll_down", "Прокрутка чата вниз", "Scroll chat down", "Ctrl+Alt+Down"),
    ("window_topmost_toggle", "Окно поверх остальных", "Toggle window on top", "Ctrl+Alt+T"),
):
    _add("advanced", "hotkeys." + field, title, english, "hotkey", default)

for field, title, english, default, minimum, maximum in (
    ("window_width", "Ширина", "Width", 322, 280, 2200),
    ("window_height", "Высота", "Height", 476, 320, 1800),
    ("chat_font_size_px", "Размер текста диалога", "Chat font size", 14, 8, 36),
    ("corner_radius", "Скругление углов", "Corner radius", 26, 0, 60),
    ("chat_height_px", "Высота чата", "Chat height", 0, 0, 1600),
):
    _add("window", "ui." + field, title, english, "int", default, minimum, maximum)
_add("window", "ui.drag_enabled", "Разрешить перетаскивание окна за верхнюю область", "Allow window dragging", default=True)
_add("window", "ui.robot_only_mode", "Режим «только робот»", "Robot-only mode")
_add("window", "ui.chat_font_family", "Шрифт диалогов", "Chat font", "text", "Segoe UI")
_add("window", "ui.animation_fps", "Частота анимаций", "Animation frame rate", "enum", 60, choices=(30, 60))
_add("window", "ui.window_theme_preset", "Цветовая схема", "Colour theme", "enum", "NEO Cyan", choices=tuple(THEME_PRESETS))
for field, title, english in (
    ("window", "Фон всего окна", "Window opacity"), ("panel", "Панели", "Panel opacity"),
    ("element", "Кнопки и поля", "Button opacity"), ("chat", "Диалоговая область", "Chat opacity"),
    ("bubble", "Сообщения", "Message opacity"), ("border", "Рамки", "Border opacity"),
):
    _add("window", "ui." + field + "_opacity_percent", title, english, "int", 100)
for field, title, english in (
    ("accent", "Акцент и выделение", "Accent colour"), ("window_color", "Основной фон окна", "Window colour"),
    ("panel_color", "Наполнение панелей", "Panel colour"), ("border_color", "Рамка окна", "Border colour"),
    ("text_color", "Основной текст", "Text colour"), ("muted_text_color", "Дополнительный текст", "Secondary text colour"),
    ("element_color", "Кнопки и поле ввода", "Button colour"), ("element_hover_color", "Элементы при наведении", "Hover colour"),
    ("chat_color", "Фон диалоговой области", "Chat colour"), ("chat_user_color", "Сообщения пользователя", "User message colour"),
    ("chat_assistant_color", "Сообщения робота", "Assistant message colour"),
):
    _add("window", "ui." + field, title, english, "color", THEME_PRESETS["NEO Cyan"]["window"][field])
for field, title, english, default in (
    ("show_on_startup", "Показывать сводку на день при запуске", "Show startup briefing", True),
    ("include_time", "Время запуска ассистента", "Briefing: time", True),
    ("include_weather", "Погода и температура", "Briefing: weather", True),
    ("include_system", "Полные сведения о компьютере — CPU / GPU / RAM / диски", "Briefing: computer", True),
    ("include_processes", "Краткие сведения о процессах", "Briefing: processes", False),
    ("include_devices", "Подключённые устройства компьютера", "Briefing: devices", True),
    ("include_bluetooth", "Подключённые Bluetooth-устройства", "Briefing: Bluetooth", True),
    ("include_services", "Проверять подключённые API-сервисы", "Check connected API services", False),
):
    _add("window", "briefing." + field, title, english, default=default)

for field, title, english, default in (
    ("show_robot", "Отображать робота", "Show robot", True),
    ("idle_animations", "Анимации в ожидании", "Idle animations", True),
    ("robot_headphones_enabled", "Наушники", "Headphones", True),
    ("robot_gif_enabled", "GIF-анимации", "GIF animations", False),
    ("robot_accessory_coffee", "Чашка кофе", "Coffee cup", False),
    ("robot_accessory_cigarette", "Сигарета", "Cigarette", False),
    ("robot_layout_edit_enabled", "Разрешить перетаскивание и изменение размеров информационных элементов", "Edit information layout", False),
    ("robot_info_clock_enabled", "Показывать текущее время", "Show clock", False),
    ("robot_info_track_enabled", "Показывать текущую песню и исполнителя", "Show current track", False),
    ("robot_info_temperature_enabled", "Показывать температуру города по умолчанию", "Show temperature", False),
    ("robot_info_temperature_show_city", "Показывать название города рядом с погодой", "Show city", True),
    ("timer_overlay_enabled", "Показывать активные таймеры и напоминания", "Show timers and reminders", True),
):
    _add("robot", "ui." + field, title, english, default=default)
for field, title, english, default, minimum, maximum in (
    ("robot_scale_percent", "Размер робота", "Robot scale", 100, 60, 180),
    ("robot_gif_scale_percent", "Масштаб GIF", "GIF scale", 100, 20, 180),
    ("robot_info_font_size", "Размер текста", "Information font size", 12, 8, 28),
    ("timer_overlay_font_size", "Размер текста таймера", "Timer font size", 12, 8, 48),
    ("timer_overlay_opacity_percent", "Прозрачность таймера", "Timer opacity", 88, 10, 100),
    ("sleep_after_seconds", "Засыпать через", "Idle sleep delay", 60, 0, 3600),
):
    _add("robot", "ui." + field, title, english, "int", default, minimum, maximum)
_add("robot", "ui.robot_theme_preset", "Цветовая схема", "Colour theme", "enum", "NEO Cyan", choices=tuple(THEME_PRESETS))
_add("robot", "ui.robot_active_animation", "Активная анимация", "Ambient animation", "enum", "off", choices=("off", "rain", "snow", "starfall", "cloudy", "starry_sky", "fireflies"))
_add("robot", "ui.timer_overlay_style", "Стиль таймера", "Timer style", "enum", "glass", choices=("glass", "minimal", "pill"))
for field, title, english, default in (
    ("robot_info_clock_position", "Положение часов", "Clock position", "top_center"),
    ("robot_info_track_position", "Положение трека", "Track position", "bottom_center"),
    ("robot_info_temperature_position", "Положение температуры", "Temperature position", "top_right"),
    ("timer_overlay_position", "Положение таймера", "Timer position", "bottom_left"),
):
    middle = ("center_left", "center", "center_right") if field.startswith("timer") else ("left_center", "right_center")
    _add("robot", "ui." + field, title, english, "enum", default, choices=("top_left", "top_center", "top_right", *middle, "bottom_left", "bottom_center", "bottom_right"))
for field, title, english, default in (
    ("robot_body_color", "Корпус", "Body colour", "#313C4F"), ("robot_shell_color", "Светлая оболочка головы", "Head colour", "#DDE7F4"),
    ("robot_screen_color", "Экран лица", "Face colour", "#101721"), ("robot_eye_color", "Глаза и эмоции", "Eye colour", "#57D7FF"),
    ("robot_trim_color", "Контуры и детали", "Trim colour", "#718198"), ("robot_glow_color", "Свечение вокруг робота", "Glow colour", "#57D7FF"),
    ("robot_headphones_color", "Цвет наушников", "Headphone colour", "#57D7FF"),
    ("robot_accessory_mug_color", "Цвет кружки", "Cup colour", "#DDE7F4"), ("robot_accessory_hand_color", "Цвет рук", "Hand colour", "#313C4F"),
    ("robot_moon_color", "Луна", "Moon colour", "#DDE7F4"), ("robot_cloud_color", "Облака", "Cloud colour", "#DCE7F3"),
    ("robot_rain_color", "Дождь", "Rain colour", "#91CFFF"), ("robot_starfall_color", "Звездопад / звёздное небо", "Star colour", "#EAF6FF"),
    ("robot_snow_color", "Снег", "Snow colour", "#F6FAFF"), ("timer_overlay_text_color", "Цвет текста", "Timer text colour", "#EAF2FF"),
    ("timer_overlay_background_color", "Цвет фона", "Timer background colour", "#0A0F18"), ("timer_overlay_border_color", "Цвет рамки", "Timer border colour", "#57D7FF"),
):
    _add("robot", "ui." + field, title, english, "color", default)

for path, title, english, default in (
    ("security.smart_downloads_enabled", "Smart Downloads", "Smart Downloads", False),
    ("security.vpn_notifications_enabled", "Уведомления VPN", "VPN notifications", False),
    ("security.latency_notifications_enabled", "Уведомления о задержке сети", "Network latency notifications", False),
    ("activity.enabled", "История действий", "Activity history", True),
    ("file_index.enabled", "Индекс файлов", "File index", True),
    ("ollama.vision_enabled", "Анализ изображений", "Image analysis", True),
):
    _add("privacy", path, title, english, default=default)
_add("privacy", "security.latency_threshold_ms", "Порог задержки (мс)", "Latency threshold (ms)", "int", 250, 50, 5000)
_add("privacy", "activity.retention_days", "Хранить историю (дней)", "History retention (days)", "int", 30, 1, 365)
for path, title, english in (
    ("downloads.directories", "Папки загрузок", "Download folders"),
    ("activity.excluded_apps", "Исключённые приложения", "Excluded applications"),
    ("activity.excluded_paths", "Исключённые папки", "Excluded folders"),
):
    _add("privacy", path, title, english, "lines", [])
_add("shelf", "shelf.enabled", "Книжная полка", "Bookshelf", default=True)
_add("shelf", "shelf.managed_copy_default", "Копировать файлы на полку", "Copy imported files", default=False)
_add("shelf", "shelf.restore_pinned", "Восстанавливать закреплённые окна", "Restore pinned windows", default=True)
_add("shelf", "shelf.font_size", "Размер шрифта", "Font size", "int", 14, 12, 28)
_add("shelf", "shelf.opacity_percent", "Непрозрачность", "Opacity", "int", 100, 25, 100)
_add("shelf", "hotkeys.bookshelf_toggle", "Горячая клавиша полки", "Bookshelf hotkey", "hotkey", "Ctrl+Alt+B")


def display_value(spec: Setting, value: Any, code: str) -> str:
    if spec.kind == "bool":
        return message("on" if value else "off", code)
    if isinstance(value, list):
        return "\n".join(str(x) for x in value) or message("empty", code)
    if spec.kind == "enum" and str(value) in CHOICES:
        return CHOICES[str(value)][LANGS.index(code) if code in LANGS else 1]
    return str(value) if value != "" else message("empty", code)


def apply_setting(config: Any, sid: str, raw: Any) -> tuple[Setting, Any, bool]:
    spec = SETTINGS.get(sid)
    if spec is None:
        raise ValueError("unknown setting")
    value = spec.parse(raw)
    with _LOCK:
        reload_config = getattr(config, "reload_if_changed", None)
        if callable(reload_config):
            reload_config()
        data = deepcopy(config.data)
        node = data
        parts = spec.path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        ui = data.setdefault("ui", {})
        if sid in {"ui_window_theme_preset", "ui_robot_theme_preset"}:
            group = "window" if sid == "ui_window_theme_preset" else "robot"
            ui.update(THEME_PRESETS[value][group])
            if group == "robot":
                ui["robot_accessory_mug_color"] = ui["robot_shell_color"]
                ui["robot_accessory_hand_color"] = ui["robot_body_color"]
        elif spec.kind == "color":
            ui["robot_theme_preset" if spec.group == "robot" else "window_theme_preset"] = "Custom"
        if sid == "ui_robot_starfall_color":
            ui["robot_starry_sky_color"] = value
        if sid in {"ui_window_opacity_percent", "ui_border_opacity_percent"}:
            ui["transparent_background" if sid == "ui_window_opacity_percent" else "transparent_borders"] = value == 0
        config.save(data)
    from jarvis.ui.desktop_events import request_desktop
    queued = request_desktop("settings.apply")
    return spec, value, queued

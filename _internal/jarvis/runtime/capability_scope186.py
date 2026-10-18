"""Public/internal scope of registered actions that have no catalogue phrases (CATALOG185-COVERAGE-01).

The per-language catalogues (``jarvis/locales/commands/*.json``) feed routing and the
command model, so they are not padded with untested phrases. Every registered action
ID that is not published there is classified here instead:

* ``public``   — a user command; ``examples`` are phrasings verified to reach this ID
  (tests/test_neo186_catalog_scope.py routes each one through the real Agent);
* ``context``  — reachable only with an object/document/dialog already in context;
* ``internal`` — a backend, UI button or alias of another public action (``via``).

``translate.translate`` is the one published-only ID: its schema has no action enum,
it is routed and stays public.
"""
from __future__ import annotations

SCOPE: dict[str, dict] = {
    # Office documents (NEO 178)
    "office.create": {"scope": "public", "examples": {"ru": "создай документ word отчёт", "en": "create a word document report"}},
    "office.open": {"scope": "public", "examples": {"ru": "открой документ отчёт.docx"}},
    "office.export": {"scope": "public", "examples": {"ru": "экспортируй документ в pdf", "en": "export the document to pdf"}},
    "office.add_sheet": {"scope": "public", "examples": {"ru": "добавь лист Бюджет"}},
    "office.add_slide": {"scope": "public", "examples": {"en": "add a slide Results"}},
    "office.get_cell": {"scope": "public", "examples": {"ru": "что в ячейке A1"}},
    "office.column_stat": {"scope": "public", "examples": {"ru": "сумма столбца B"}},
    "office.undo": {"scope": "public", "examples": {"ru": "отмени изменения в документе"}},
    "office.set_cell": {"scope": "context", "note": "the open spreadsheet"},
    "office.add_row": {"scope": "context", "note": "the open spreadsheet"},
    "office.replace": {"scope": "context", "note": "the open document"},
    "office.append": {"scope": "context", "note": "the open document"},
    "office.heading": {"scope": "context", "note": "the open document"},
    "office.read": {"scope": "context", "note": "the open document"},
    "office.save": {"scope": "context", "note": "the open document"},
    "office.summarize": {"scope": "context", "note": "the open document"},
    # Windows settings (NEO 178)
    "winsettings.open": {"scope": "public", "examples": {"ru": "открой настройки windows дисплей"}},
    "winsettings.check_updates": {"scope": "public", "examples": {"ru": "проверь обновления windows", "en": "check for windows updates"}},
    "winsettings.power_mode": {"scope": "public", "examples": {"ru": "включи режим энергосбережения"}},
    "winsettings.clear_clipboard_history": {"scope": "public", "examples": {"ru": "очисти историю буфера обмена"}},
    "winsettings.activation_status": {"scope": "public", "examples": {"ru": "статус активации windows"}},
    "winsettings.screen_timeout": {"scope": "public", "examples": {"ru": "отключать экран через 10 минут", "en": "set screen timeout to 10 minutes"}},
    "winsettings.sleep_timeout": {"scope": "public", "examples": {"ru": "переводить в сон через 30 минут"}},
    "winsettings.storage_info": {"scope": "internal", "via": "system_monitor.disks"},
    "winsettings.about": {"scope": "internal", "via": "system_monitor.summary"},
    "winsettings.toggle": {"scope": "context", "note": "a named Windows switch with an explicit on/off"},
    "winsettings.wallpaper": {"scope": "context", "note": "a chosen image file"},
    # System information
    "system_monitor.battery": {"scope": "public", "examples": {"ru": "сколько заряда батареи", "en": "what is the battery level"}},
    # NEO 187 HREQ-026: another video (not a seek); routed by jarvis.runtime.video_nav187
    "media.next": {"scope": "public", "examples": {"ru": "следующее видео", "en": "next video", "de": "nächstes Video",
                                                   "fr": "vidéo suivante", "es": "siguiente video", "it": "video successivo",
                                                   "pt": "próximo vídeo"}},
    "media.previous": {"scope": "public", "examples": {"ru": "предыдущее видео", "en": "previous video", "de": "vorheriges Video",
                                                       "fr": "vidéo précédente", "es": "video anterior", "it": "video precedente",
                                                       "pt": "vídeo anterior"}},
    # NEO 187: «отмени все таймеры» (jarvis.runtime.timer_cancel187) acts only when timers exist
    "timer.cancel_all": {"scope": "context", "note": "existing timers («отмени все таймеры»)"},
    # NEO 188 NEO-DOC-001: answers from ONE chosen file / comparison of exactly two (jarvis.runtime.document_scope188)
    "knowledge.ask_file": {"scope": "public", "examples": {"ru": "ответь по файлу отчёт.pdf: когда поездка?",
                                                           "en": "what does the file notes.md say about Kazan",
                                                           "de": "was steht in der Datei notizen.md über Kasan",
                                                           "fr": "que dit le fichier notes.md sur Kazan",
                                                           "es": "qué dice el archivo notas.md sobre Kazán",
                                                           "it": "cosa dice il file note.md su Kazan",
                                                           "pt": "o que diz o ficheiro notas.md sobre Kazan"}},
    "knowledge.compare": {"scope": "public", "examples": {"ru": "сравни файлы план.docx и бюджет.xlsx по срокам",
                                                          "en": "compare the files a.txt and b.txt on prices",
                                                          "de": "vergleiche die Dateien a.txt und b.txt",
                                                          "fr": "compare les fichiers a.txt et b.txt sur les prix",
                                                          "es": "compara los archivos a.txt y b.txt sobre precios",
                                                          "it": "confronta i file a.txt e b.txt",
                                                          "pt": "compara os ficheiros a.txt e b.txt sobre preços"}},
    # NEO 188 NEO-DOC-002: the exact excerpt behind a numbered source of the last document answer
    "knowledge.show_source": {"scope": "context", "note": "a numbered source of the last answer from local files"},
    "knowledge.open_source": {"scope": "context", "note": "a numbered source of the last answer from local files"},
    "knowledge.reveal_source": {"scope": "context", "note": "a numbered source of the last answer from local files"},
    # NEO 188 NEO-OFFICE-001: a new .xlsx with an editable chart from an explicit range (preview first)
    "office.copy_formula": {"scope": "context", "note": "formulas of an existing saved .xlsx (preview first)"},
    "office.chart": {"scope": "context", "note": "an existing saved .xlsx with the named sheet and range (preview first)"},
    # App skills (NEO 173): backends behind the ordinary open/launch commands
    "apps.create_folder": {"scope": "public", "examples": {"ru": "создай папку Проекты на рабочем столе"}},
    "apps.browser": {"scope": "internal", "via": "application.open"},
    "apps.chain": {"scope": "internal", "via": "multi-step tasks"},
    "apps.discord": {"scope": "internal", "via": "discord.*"},
    "apps.folder": {"scope": "internal", "via": "desktop.open"},
    "apps.settings": {"scope": "internal", "via": "system.open_settings"},
    "apps.steam": {"scope": "internal", "via": "application.open"},
    "apps.telegram": {"scope": "internal", "via": "application.open"},
    # Context and UI operations
    "desktop.close_folder": {"scope": "context", "note": "the folder NEO opened"},
    "timer.reschedule": {"scope": "context", "note": "the timer list «Change»"},
    "scenario.retry": {"scope": "internal", "via": "Settings → Scenarios «Retry step»"},
    "spotify.queue_status": {"scope": "internal", "via": "media queue preview"},
}

PUBLISHED_ONLY = {"translate.translate": {"scope": "public", "examples": {"ru": "переведи привет на английский"}}}


def scope_of(action_id: str) -> str:
    row = SCOPE.get(action_id) or PUBLISHED_ONLY.get(action_id)
    return str(row["scope"]) if row else "catalogue"

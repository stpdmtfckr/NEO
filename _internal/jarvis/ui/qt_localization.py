"""Application-owned Qt chrome language, independent of the operating system.

Only Qt framework contexts and standard button identities are translated here.
Application prose, provider errors, filenames, user text and brands are untouched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QLibraryInfo, QLocale, QObject, Qt, QTranslator
from PySide6.QtWidgets import QApplication, QColorDialog, QDialogButtonBox, QFileDialog, QMessageBox

from jarvis.language import LANGUAGES, language_code

_CODES = tuple(LANGUAGES)
_LOCALES = {"ru": "ru_RU", "en": "en_US", "de": "de_DE", "fr": "fr_FR", "es": "es_ES", "it": "it_IT", "pt": "pt_PT"}

# Explicit fallback remains available if a compact Qt distribution omits .qm.
# Source keys use Qt's English source spelling, never arbitrary application text.
_ROWS = {
    "OK": ("ОК", "OK", "OK", "OK", "Aceptar", "OK", "OK"),
    "Save": ("Сохранить", "Save", "Speichern", "Enregistrer", "Guardar", "Salva", "Guardar"),
    "Save All": ("Сохранить всё", "Save All", "Alles speichern", "Tout enregistrer", "Guardar todo", "Salva tutto", "Guardar tudo"),
    "Open": ("Открыть", "Open", "Öffnen", "Ouvrir", "Abrir", "Apri", "Abrir"),
    "Yes": ("Да", "Yes", "Ja", "Oui", "Sí", "Sì", "Sim"),
    "Yes to All": ("Да для всех", "Yes to All", "Ja zu allen", "Oui à tout", "Sí a todo", "Sì a tutto", "Sim para todos"),
    "No": ("Нет", "No", "Nein", "Non", "No", "No", "Não"),
    "No to All": ("Нет для всех", "No to All", "Nein zu allen", "Non à tout", "No a todo", "No a tutto", "Não para todos"),
    "Abort": ("Прервать", "Abort", "Abbrechen", "Abandonner", "Anular", "Interrompi", "Interromper"),
    "Retry": ("Повторить", "Retry", "Erneut versuchen", "Réessayer", "Reintentar", "Riprova", "Tentar novamente"),
    "Ignore": ("Пропустить", "Ignore", "Ignorieren", "Ignorer", "Ignorar", "Ignora", "Ignorar"),
    "Close": ("Закрыть", "Close", "Schließen", "Fermer", "Cerrar", "Chiudi", "Fechar"),
    "Cancel": ("Отмена", "Cancel", "Abbrechen", "Annuler", "Cancelar", "Annulla", "Cancelar"),
    "Discard": ("Не сохранять", "Discard", "Verwerfen", "Ne pas enregistrer", "Descartar", "Non salvare", "Descartar"),
    "Help": ("Справка", "Help", "Hilfe", "Aide", "Ayuda", "Aiuto", "Ajuda"),
    "Apply": ("Применить", "Apply", "Anwenden", "Appliquer", "Aplicar", "Applica", "Aplicar"),
    "Reset": ("Сбросить", "Reset", "Zurücksetzen", "Réinitialiser", "Restablecer", "Reimposta", "Repor"),
    "Restore Defaults": ("По умолчанию", "Restore Defaults", "Standardwerte", "Valeurs par défaut", "Valores predeterminados", "Valori predefiniti", "Valores predefinidos"),
    "Undo": ("Отменить", "Undo", "Rückgängig", "Annuler", "Deshacer", "Annulla", "Desfazer"),
    "Redo": ("Повторить", "Redo", "Wiederholen", "Rétablir", "Rehacer", "Ripeti", "Refazer"),
    "Cut": ("Вырезать", "Cut", "Ausschneiden", "Couper", "Cortar", "Taglia", "Cortar"),
    "Copy": ("Копировать", "Copy", "Kopieren", "Copier", "Copiar", "Copia", "Copiar"),
    "Paste": ("Вставить", "Paste", "Einfügen", "Coller", "Pegar", "Incolla", "Colar"),
    "Delete": ("Удалить", "Delete", "Löschen", "Supprimer", "Eliminar", "Elimina", "Eliminar"),
    "Select All": ("Выделить всё", "Select All", "Alles auswählen", "Tout sélectionner", "Seleccionar todo", "Seleziona tutto", "Selecionar tudo"),
    "Clear": ("Очистить", "Clear", "Leeren", "Effacer", "Borrar", "Cancella", "Limpar"),
    "Browse": ("Обзор", "Browse", "Durchsuchen", "Parcourir", "Examinar", "Sfoglia", "Procurar"),
    "Look in:": ("Папка:", "Look in:", "Suchen in:", "Regarder dans :", "Buscar en:", "Cerca in:", "Procurar em:"),
    "File name:": ("Имя файла:", "File name:", "Dateiname:", "Nom du fichier :", "Nombre de archivo:", "Nome file:", "Nome do ficheiro:"),
    "Files of type:": ("Тип файлов:", "Files of type:", "Dateityp:", "Type de fichiers :", "Tipo de archivos:", "Tipo di file:", "Tipo de ficheiros:"),
    "File type:": ("Тип файла:", "File type:", "Dateityp:", "Type de fichier :", "Tipo de archivo:", "Tipo di file:", "Tipo de ficheiro:"),
    "Directory:": ("Папка:", "Directory:", "Ordner:", "Dossier :", "Carpeta:", "Cartella:", "Pasta:"),
    "Folder:": ("Папка:", "Folder:", "Ordner:", "Dossier :", "Carpeta:", "Cartella:", "Pasta:"),
    "Back": ("Назад", "Back", "Zurück", "Retour", "Atrás", "Indietro", "Voltar"),
    "Forward": ("Вперёд", "Forward", "Vorwärts", "Suivant", "Adelante", "Avanti", "Avançar"),
    "Parent Directory": ("На уровень вверх", "Parent Directory", "Übergeordneter Ordner", "Dossier parent", "Carpeta superior", "Cartella superiore", "Pasta superior"),
    "Create New Folder": ("Создать папку", "Create New Folder", "Neuen Ordner erstellen", "Créer un dossier", "Crear carpeta", "Crea cartella", "Criar pasta"),
    "New Folder": ("Новая папка", "New Folder", "Neuer Ordner", "Nouveau dossier", "Nueva carpeta", "Nuova cartella", "Nova pasta"),
    "List View": ("Список", "List View", "Listenansicht", "Liste", "Vista de lista", "Elenco", "Vista de lista"),
    "Detail View": ("Подробно", "Detail View", "Detailansicht", "Détails", "Vista de detalles", "Dettagli", "Vista de detalhes"),
    "Show hidden files": ("Показывать скрытые файлы", "Show hidden files", "Versteckte Dateien anzeigen", "Afficher les fichiers cachés", "Mostrar archivos ocultos", "Mostra file nascosti", "Mostrar ficheiros ocultos"),
    "Rename": ("Переименовать", "Rename", "Umbenennen", "Renommer", "Cambiar nombre", "Rinomina", "Mudar o nome"),
    "Name": ("Имя", "Name", "Name", "Nom", "Nombre", "Nome", "Nome"),
    "Size": ("Размер", "Size", "Größe", "Taille", "Tamaño", "Dimensione", "Tamanho"),
    "Type": ("Тип", "Type", "Typ", "Type", "Tipo", "Tipo", "Tipo"),
    "Date Modified": ("Дата изменения", "Date Modified", "Änderungsdatum", "Date de modification", "Fecha de modificación", "Data di modifica", "Data de alteração"),
    "All Files (*)": ("Все файлы (*)", "All Files (*)", "Alle Dateien (*)", "Tous les fichiers (*)", "Todos los archivos (*)", "Tutti i file (*)", "Todos os ficheiros (*)"),
    "Choose": ("Выбрать", "Choose", "Auswählen", "Choisir", "Elegir", "Scegli", "Escolher"),
    "Choose File": ("Выберите файл", "Choose File", "Datei auswählen", "Choisir un fichier", "Elegir archivo", "Scegli un file", "Escolher ficheiro"),
    "Find Directory": ("Выберите папку", "Find Directory", "Ordner auswählen", "Choisir un dossier", "Elegir carpeta", "Scegli una cartella", "Escolher pasta"),
    "Show Details...": ("Показать подробности…", "Show Details...", "Details anzeigen…", "Afficher les détails…", "Mostrar detalles…", "Mostra dettagli…", "Mostrar detalhes…"),
    "Hide Details...": ("Скрыть подробности…", "Hide Details...", "Details ausblenden…", "Masquer les détails…", "Ocultar detalles…", "Nascondi dettagli…", "Ocultar detalhes…"),
    "Computer": ("Компьютер", "Computer", "Computer", "Ordinateur", "Equipo", "Computer", "Computador"),
    "Drive": ("Диск", "Drive", "Laufwerk", "Lecteur", "Unidad", "Unità", "Unidade"),
    "Folder": ("Папка", "Folder", "Ordner", "Dossier", "Carpeta", "Cartella", "Pasta"),
    "File": ("Файл", "File", "Datei", "Fichier", "Archivo", "File", "Ficheiro"),
    "Files": ("Файлы", "Files", "Dateien", "Fichiers", "Archivos", "File", "Ficheiros"),
    "%1 bytes": ("%1 байт", "%1 bytes", "%1 Bytes", "%1 octets", "%1 bytes", "%1 byte", "%1 bytes"),
    "Sidebar": ("Боковая панель", "Sidebar", "Seitenleiste", "Barre latérale", "Barra lateral", "Barra laterale", "Barra lateral"),
    "List of places and bookmarks": ("Список мест и закладок", "List of places and bookmarks", "Liste der Orte und Lesezeichen", "Liste des emplacements et favoris", "Lista de ubicaciones y marcadores", "Elenco di posizioni e segnalibri", "Lista de locais e marcadores"),
    "Select Color": ("Выберите цвет", "Select Color", "Farbe auswählen", "Choisir une couleur", "Elegir color", "Scegli un colore", "Escolher cor"),
    "Basic colors": ("Основные цвета", "Basic colors", "Grundfarben", "Couleurs de base", "Colores básicos", "Colori di base", "Cores básicas"),
    "Custom colors": ("Свои цвета", "Custom colors", "Benutzerdefinierte Farben", "Couleurs personnalisées", "Colores personalizados", "Colori personalizzati", "Cores personalizadas"),
    "Add to Custom Colors": ("Добавить в свои цвета", "Add to Custom Colors", "Zu benutzerdefinierten Farben hinzufügen", "Ajouter aux couleurs personnalisées", "Añadir a colores personalizados", "Aggiungi ai colori personalizzati", "Adicionar às cores personalizadas"),
    "Pick Screen Color": ("Выбрать цвет на экране", "Pick Screen Color", "Bildschirmfarbe auswählen", "Choisir une couleur à l’écran", "Elegir un color de la pantalla", "Scegli un colore dallo schermo", "Escolher uma cor do ecrã"),
    "Red:": ("Красный:", "Red:", "Rot:", "Rouge :", "Rojo:", "Rosso:", "Vermelho:"),
    "Green:": ("Зелёный:", "Green:", "Grün:", "Vert :", "Verde:", "Verde:", "Verde:"),
    "Blue:": ("Синий:", "Blue:", "Blau:", "Bleu :", "Azul:", "Blu:", "Azul:"),
    "Hue:": ("Тон:", "Hue:", "Farbton:", "Teinte :", "Tono:", "Tonalità:", "Tonalidade:"),
    "Sat:": ("Насыщенность:", "Sat:", "Sättigung:", "Saturation :", "Saturación:", "Saturazione:", "Saturação:"),
    "Val:": ("Яркость:", "Val:", "Helligkeit:", "Luminosité :", "Brillo:", "Luminosità:", "Luminosidade:"),
    "Alpha channel:": ("Прозрачность:", "Alpha channel:", "Alphakanal:", "Canal alpha :", "Canal alfa:", "Canale alfa:", "Canal alfa:"),
}

for _alias, _source in {"Go back": "Back", "Go forward": "Forward", "Go to the parent directory": "Parent Directory", "Create a New Folder": "Create New Folder", "Change to detail view mode": "Detail View", "Change to list view mode": "List View"}.items():
    _ROWS[_alias] = _ROWS[_source]

_CONTEXTS = frozenset({"QPlatformTheme", "QDialogButtonBox", "QMessageBox", "QLineEdit", "QWidgetTextControl", "QTextEdit", "QPlainTextEdit", "QFileDialog", "QFileSystemModel", "QFileSystemModelPrivate", "QAbstractFileIconProvider", "QInputDialog", "QColorDialog", "QFontDialog", "QPrintDialog", "QPrintPreviewDialog"})
_STANDARD_NAMES = ("Ok", "Save", "SaveAll", "Open", "Yes", "YesToAll", "No", "NoToAll", "Abort", "Retry", "Ignore", "Close", "Cancel", "Discard", "Help", "Apply", "Reset", "RestoreDefaults")
_STANDARD_SOURCES = ("OK", "Save", "Save All", "Open", "Yes", "Yes to All", "No", "No to All", "Abort", "Retry", "Ignore", "Close", "Cancel", "Discard", "Help", "Apply", "Reset", "Restore Defaults")


def _code(value: str) -> str:
    return value if value in LANGUAGES else "en"


def selected_ui_language(widget: Any = None) -> str:
    app = QApplication.instance()
    controller = getattr(app, "_neo_qt_locale_controller", None)
    if controller is not None:
        return controller.code
    owner = widget
    for _ in range(12):
        if owner is None:
            break
        config = getattr(owner, "config", None)
        if config is not None:
            return language_code(config)
        value = getattr(owner, "language", None)
        if isinstance(value, str) and value in LANGUAGES:
            return value
        owner = owner.parent() if callable(getattr(owner, "parent", None)) else None
    return "en"


def standard_text(source: str, code: str) -> str:
    row = _ROWS.get(source.replace("&", ""))
    return row[_CODES.index(_code(code))] if row else source


class _FrameworkFallback(QTranslator):
    def __init__(self, code: str, parent=None):
        super().__init__(parent)
        self.code = code

    def isEmpty(self):  # noqa: N802
        return False

    def translate(self, context, sourceText, disambiguation=None, n=-1):
        if context not in _CONTEXTS:
            return None
        source = str(sourceText or "")
        row = _ROWS.get(source.replace("&", ""))
        if row is None:
            return None
        translated = row[_CODES.index(self.code)]
        return "&" + translated if "&" in source else translated


def localize_standard_buttons(dialog, code: str | None = None) -> None:
    """Translate by standard-button identity; custom user captions stay intact."""
    selected = _code(code or selected_ui_language(dialog))
    boxes = [dialog] if isinstance(dialog, (QMessageBox, QDialogButtonBox)) else []
    if hasattr(dialog, "findChildren"):
        boxes.extend(dialog.findChildren(QDialogButtonBox))
    for box in boxes:
        enum = QMessageBox.StandardButton if isinstance(box, QMessageBox) else QDialogButtonBox.StandardButton
        for name, source in zip(_STANDARD_NAMES, _STANDARD_SOURCES):
            button = box.button(getattr(enum, name))
            if button is not None:
                caption = standard_text(source, selected)
                # Keep native dialog mnemonics when Qt supplied one.
                button.setText(("&" if "&" in button.text() else "") + caption)


class _LocaleController(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.code = "en"
        self.translators = []
        self.native_catalog_loaded = False
        self._applied = None
        app.installEventFilter(self)

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() in {QEvent.Type.Show, QEvent.Type.LanguageChange}:
            if isinstance(watched, (QMessageBox, QDialogButtonBox, QFileDialog)):
                localize_standard_buttons(watched, self.code)
        return False

    def apply(self, code: str, *, load_native: bool = True):
        signature = (_code(code), bool(load_native))
        if self._applied == signature:
            return self
        self.code = _code(code)
        QLocale.setDefault(QLocale(_LOCALES[self.code]))
        for translator in self.translators:
            self.app.removeTranslator(translator)
            translator.deleteLater()
        self.translators.clear()
        fallback = _FrameworkFallback(self.code, self)
        self.app.installTranslator(fallback)
        self.translators.append(fallback)
        self.native_catalog_loaded = False
        if load_native:
            folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
            candidates = (self.code, "pt_BR") if self.code == "pt" else (self.code,)
            for candidate in candidates:
                translator = QTranslator(self)
                if translator.load(str(Path(folder) / ("qtbase_" + candidate + ".qm"))):
                    self.app.installTranslator(translator)
                    self.translators.append(translator)
                    self.native_catalog_loaded = True
                    break
                translator.deleteLater()
        for window in self.app.topLevelWidgets():
            window.setLocale(QLocale(_LOCALES[self.code]))
            localize_standard_buttons(window, self.code)
        self._applied = signature
        return self


def install_ui_language(app: QApplication, code: str, *, load_native: bool = True):
    controller = getattr(app, "_neo_qt_locale_controller", None)
    if controller is None:
        controller = _LocaleController(app)
        app._neo_qt_locale_controller = controller
    return controller.apply(code, load_native=load_native)


def localized_message_box(parent, icon, title: str, text: str,
                          buttons=QMessageBox.StandardButton.Ok,
                          default_button=QMessageBox.StandardButton.NoButton, *, code: str | None = None):
    """Show pretranslated content with guaranteed native standard button labels."""
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(str(title))
    box.setText(str(text))
    box.setStandardButtons(buttons)
    if default_button != QMessageBox.StandardButton.NoButton:
        box.setDefaultButton(default_button)
    localize_standard_buttons(box, code)
    return QMessageBox.StandardButton(box.exec())


class LocalizedFileDialog(QFileDialog):
    """Explicit opt-in for NEO pickers; unrelated/third-party dialogs unchanged."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)

    @staticmethod
    def getOpenFileName(parent=None, caption="", dir="", filter="", selectedFilter="", options=QFileDialog.Option(0)):
        return QFileDialog.getOpenFileName(parent, caption, dir, filter, selectedFilter, options | QFileDialog.Option.DontUseNativeDialog)

    @staticmethod
    def getOpenFileNames(parent=None, caption="", dir="", filter="", selectedFilter="", options=QFileDialog.Option(0)):
        return QFileDialog.getOpenFileNames(parent, caption, dir, filter, selectedFilter, options | QFileDialog.Option.DontUseNativeDialog)

    @staticmethod
    def getSaveFileName(parent=None, caption="", dir="", filter="", selectedFilter="", options=QFileDialog.Option(0)):
        return QFileDialog.getSaveFileName(parent, caption, dir, filter, selectedFilter, options | QFileDialog.Option.DontUseNativeDialog)

    @staticmethod
    def getExistingDirectory(parent=None, caption="", dir="", options=QFileDialog.Option.ShowDirsOnly):
        return QFileDialog.getExistingDirectory(parent, caption, dir, options | QFileDialog.Option.DontUseNativeDialog)


class LocalizedColorDialog(QColorDialog):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)

    @staticmethod
    def getColor(initial=Qt.GlobalColor.white, parent=None, title="", options=QColorDialog.ColorDialogOption(0)):
        return QColorDialog.getColor(initial, parent, title, options | QColorDialog.ColorDialogOption.DontUseNativeDialog)

"""Export parser-checked manual examples. This NEVER executes a tool."""
from __future__ import annotations
import json
from pathlib import Path
from jarvis.runtime.feature_commands import parse_feature_command, exact_phrases
from jarvis.runtime.natural_commands import LANGUAGES
from jarvis.runtime.multilingual_commands import normalize_multilingual_command
from jarvis.runtime.social_atlas import catalog, category_count

EXAMPLES = {
    "chance.roll_die": ('брось кубик', 'roll a die', 'würfle', 'lance un dé', 'tira un dado', 'tira un dado', 'lança um dado'),
    "chance.flip_coin": ('подкинь монетку', 'flip a coin', 'wirf eine münze', 'lance une pièce', 'lanza una moneda', 'lancia una moneta', 'lança uma moeda'),
    "shelf.create": ("создай заметку Работа: Позвонить завтра", "create note Work: Call tomorrow", "erstelle eine Notiz Arbeit: Morgen anrufen", "crée une note Travail: Appeler demain", "crea una nota Trabajo: Llamar mañana", "crea una nota Lavoro: Chiamare domani", "cria uma nota Trabalho: Ligar amanhã"),
    "shelf.read": ("прочитай заметку Работа", "read note Work", "lies die Notiz Arbeit", "lis la note Travail", "lee la nota Trabajo", "leggi la nota Lavoro", "lê a nota Trabalho"),
    "shelf.update": ("измени заметку Работа: Позвонить сегодня", "update note Work: Call today", "ändere die Notiz Arbeit: Heute anrufen", "modifie la note Travail: Appeler aujourd'hui", "modifica la nota Trabajo: Llamar hoy", "modifica la nota Lavoro: Chiamare oggi", "altera a nota Trabalho: Ligar hoje"),
    "shelf.delete": ("удали заметку Работа", "delete note Work", "lösche die Notiz Arbeit", "supprime la note Travail", "elimina la nota Trabajo", "elimina la nota Lavoro", "apaga a nota Trabalho"),
    "shelf.pin": ("закрепи заметку Работа", "pin note Work", "fixiere die Notiz Arbeit", "épingle la note Travail", "fija la nota Trabajo", "fissa la nota Lavoro", "fixa a nota Trabalho"),
    "shelf.copy": ("скопируй заметку Работа", "copy note Work", "kopiere die Notiz Arbeit", "copie la note Travail", "copia la nota Trabajo", "copia la nota Lavoro", "copia a nota Trabalho"),
    "shelf.create.checklist": ("создай чек-лист Сборы: Ключи; Телефон", "create checklist Packing: Keys; Phone", "erstelle eine Checkliste Packen: Schlüssel; Telefon", "crée une liste de tâches Bagages: Clés; Téléphone", "crea una lista de tareas Equipaje: Llaves; Teléfono", "crea una lista di attività Bagaglio: Chiavi; Telefono", "cria uma lista de tarefas Bagagem: Chaves; Telefone"),
    "shelf.check": ("отметь пункт 1 в чек-листе Сборы", "check item 1 in checklist Packing", "markiere Punkt 1 in der Checkliste Packen", "coche l'élément 1 dans la liste de tâches Bagages", "marca el elemento 1 en la lista de tareas Equipaje", "seleziona l'elemento 1 nella lista di attività Bagaglio", "marca o item 1 na lista de tarefas Bagagem"),
    "mini_browser.open": ("открой мини браузер https://example.org", "open mini browser https://example.org", "öffne mini browser https://example.org", "ouvre le mini navigateur https://example.org", "abre el mini navegador https://example.org", "apri mini browser https://example.org", "abre mini navegador https://example.org"),
    "mini_browser.close.image": ("закрой изображение", "close the image", "schließe das Bild", "ferme l'image", "cierra la imagen", "chiudi l'immagine", "fecha a imagem"),
    "mini_browser.pin.image": ("закрепи изображение", "pin the image", "fixiere das Bild", "épingle l'image", "fija la imagen", "fissa l'immagine", "fixa a imagem"),
    "mini_browser.pin.image_unpin": ("открепи изображение", "unpin the image", "löse die Fixierung des Bildes", "désépingle l'image", "desfija la imagen", "sblocca l'immagine", "desfixa a imagem"),
    "mini_browser.zoom.image": ("масштаб изображения 150 процентов", "set the image zoom to 150 percent", "setze die Bildvergrößerung auf 150 Prozent", "règle le zoom de l'image à 150 pour cent", "ajusta el zoom de la imagen al 150 por ciento", "imposta lo zoom dell'immagine al 150 per cento", "define o zoom da imagem para 150 por cento"),
    "game_locations.find": ("где хранятся сохранения игры Minecraft", "where are saves for Minecraft", "wo sind die Spielstände von Minecraft", "où sont les sauvegardes de Minecraft", "dónde están las partidas de Minecraft", "dove sono i salvataggi di Minecraft", "onde estão os jogos guardados de Minecraft"),
    "activity.search": ("найди в истории активности Steam", "search activity history for Steam", "suche im Aktivitätsverlauf nach Steam", "cherche dans l'historique d'activité Steam", "busca en el historial de actividad Steam", "cerca nella cronologia attività Steam", "procura no histórico de atividade Steam"),
}


def export():
    output = {"method": "Selected-locale parser dry-run only; no OS/API/tool calls. Example object titles must exist for read/update/delete. Destructive actions retain confirmation.", "social_categories": category_count(), "languages": {}}
    for index, code in enumerate(LANGUAGES):
        examples = []
        for phrase, (tool, action) in exact_phrases(code).items():
            parsed = parse_feature_command(phrase, code)
            assert parsed == (tool, {"action": action}), (code, phrase)
            examples.append({"phrase": phrase, "tool": tool, "arguments": parsed[1]})
        for action, rows in EXAMPLES.items():
            phrase = rows[index]
            parsed = parse_feature_command(phrase, code)
            expected_tool, expected_action = action.split(".")[:2]
            assert parsed and parsed[0] == expected_tool and parsed[1]["action"] == expected_action, (code, phrase, parsed)
            examples.append({"phrase": phrase, "tool": parsed[0], "arguments": parsed[1]})
        output["languages"][code] = {"features": examples, "social": [{"phrase": p, "category": row[0]} for p, row in catalog(code).items()]}
    target = Path(__file__).resolve().parent.parent / "docs" / "COMMAND_EXAMPLES.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(target), "validated_feature_examples": sum(len(v["features"]) for v in output["languages"].values())}))


if __name__ == "__main__":
    export()

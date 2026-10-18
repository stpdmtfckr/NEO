"""Example NEO plugin: text statistics for the clipboard (needs only «clipboard.read»)."""
import re

from jarvis.security import Risk
from jarvis.tools.registry import ToolDefinition, ToolExecution

_TEXT = {
    'ru': 'Слов: {words}, символов: {chars}, предложений: {sentences}.',
    'en': 'Words: {words}, characters: {chars}, sentences: {sentences}.',
    'de': 'Wörter: {words}, Zeichen: {chars}, Sätze: {sentences}.',
    'fr': 'Mots : {words}, caractères : {chars}, phrases : {sentences}.',
    'es': 'Palabras: {words}, caracteres: {chars}, frases: {sentences}.',
    'it': 'Parole: {words}, caratteri: {chars}, frasi: {sentences}.',
    'pt': 'Palavras: {words}, caracteres: {chars}, frases: {sentences}.',
}
_EMPTY = {'ru': 'В буфере обмена нет текста.', 'en': 'There is no text on the clipboard.'}


def stats(text: str) -> dict:
    return {'words': len(re.findall(r'\w+', text)), 'chars': len(text),
            'sentences': len([s for s in re.split(r'[.!?…]+', text) if s.strip()])}


def register_plugin(context):
    def handler(arguments):
        code = str(context.config.get('app.language', 'en') or 'en')
        text = str(arguments.get('text', '') or '') or context.clipboard_text()
        if not text.strip():
            return ToolExecution(False, _EMPTY.get(code, _EMPTY['en']), {})
        values = stats(text)
        return ToolExecution(True, _TEXT.get(code, _TEXT['en']).format(**values), values)

    context.register(ToolDefinition(
        'text_stats', 'Word, character and sentence count of the clipboard text.',
        {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['count']}, 'text': {'type': 'string'}},
         'required': ['action'], 'additionalProperties': False},
        handler, lambda _args: Risk.SAFE))

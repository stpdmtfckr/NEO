"""NEO 183: one strict text-encoding detector for every file writer (filesystem, clipboard paste).

A mutation must never decode with ``errors="replace"``: that silently turns a CP1251 or UTF-16
file into U+FFFD and writes it back as UTF-8. Instead the existing bytes are classified once:
BOM-marked UTF-8/16/32, plain UTF-8, a clearly Cyrillic CP1251 text, binary, or unknown.
"""
from __future__ import annotations

from dataclasses import dataclass

_BOMS = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
         (b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))


@dataclass(frozen=True)
class TextEncoding:
    codec: str | None          # None: binary or unknown — the file must not be changed
    bom: bytes = b""
    newline: str = "\n"
    reason: str = ""           # "binary" | "unknown" when codec is None

    @property
    def ok(self) -> bool:
        return self.codec is not None


def _newline(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def looks_binary(data: bytes) -> bool:
    """NUL bytes or many control characters (tab/CR/LF/FF excluded) mean «not a text file»."""
    if not data:
        return False
    sample = data[:65536]
    if b"\x00" in sample:
        return True
    controls = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13))
    return controls / max(1, len(sample)) > 0.02


def detect(data: bytes) -> TextEncoding:
    for bom, codec in _BOMS:
        if data.startswith(bom):
            try:
                text = data[len(bom):].decode(codec)
            except UnicodeDecodeError:
                return TextEncoding(None, reason="unknown")
            return TextEncoding(codec, bom, _newline(text))
    if looks_binary(data):
        return TextEncoding(None, reason="binary")
    try:
        text = data.decode("utf-8")
        return TextEncoding("utf-8", b"", _newline(text))
    except UnicodeDecodeError:
        pass
    try:
        text = data.decode("cp1251")
    except UnicodeDecodeError:
        return TextEncoding(None, reason="unknown")
    letters = [c for c in text if c.isalpha()]
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    if letters and cyrillic / len(letters) >= 0.5:
        return TextEncoding("cp1251", b"", _newline(text))
    return TextEncoding(None, reason="unknown")


def decode(data: bytes, enc: TextEncoding) -> str:
    return data[len(enc.bom):].decode(str(enc.codec))


def encode(text: str, enc: TextEncoding) -> bytes:
    return enc.bom + text.encode(str(enc.codec))


def match_newlines(text: str, newline: str) -> str:
    """Use the file's own line breaks for appended text."""
    plain = text.replace("\r\n", "\n")
    return plain.replace("\n", "\r\n") if newline == "\r\n" else plain


_MESSAGES = {
    "binary": {"ru": "Файл не изменён: это двоичный файл, а не текст.", "en": "The file was not changed: it is a binary file, not text.",
               "de": "Die Datei wurde nicht geändert: Es ist eine Binärdatei, kein Text.", "fr": "Le fichier n’a pas été modifié : c’est un fichier binaire, pas du texte.",
               "es": "El archivo no se ha modificado: es un archivo binario, no texto.", "it": "Il file non è stato modificato: è un file binario, non testo.",
               "pt": "O ficheiro não foi alterado: é um ficheiro binário, não texto."},
    "unknown": {"ru": "Файл не изменён: не удалось определить его кодировку. Укажите кодировку явно, например cp1251.",
                "en": "The file was not changed: its text encoding could not be determined. Name the encoding, e.g. cp1251.",
                "de": "Die Datei wurde nicht geändert: Ihre Kodierung ist unbekannt. Gib die Kodierung an, z. B. cp1251.",
                "fr": "Le fichier n’a pas été modifié : son encodage est inconnu. Indiquez l’encodage, par ex. cp1251.",
                "es": "El archivo no se ha modificado: no se pudo determinar su codificación. Indique la codificación, p. ej. cp1251.",
                "it": "Il file non è stato modificato: la codifica non è riconoscibile. Indica la codifica, ad es. cp1251.",
                "pt": "O ficheiro não foi alterado: não foi possível determinar a codificação. Indique a codificação, p. ex. cp1251."},
    "invalid_encoding": {"ru": "Файл не изменён: текст нельзя записать в кодировке {encoding}.", "en": "The file was not changed: the text cannot be written in the {encoding} encoding.",
                         "de": "Die Datei wurde nicht geändert: Der Text lässt sich nicht in {encoding} schreiben.", "fr": "Le fichier n’a pas été modifié : le texte ne peut pas être écrit en {encoding}.",
                         "es": "El archivo no se ha modificado: el texto no se puede escribir en {encoding}.", "it": "Il file non è stato modificato: il testo non si può scrivere in {encoding}.",
                         "pt": "O ficheiro não foi alterado: o texto não pode ser escrito em {encoding}."},
    "locked": {"ru": "Файл не изменён: он открыт или заблокирован другой программой.", "en": "The file was not changed: another program has it open or locked.",
               "de": "Die Datei wurde nicht geändert: Ein anderes Programm hat sie geöffnet oder gesperrt.", "fr": "Le fichier n’a pas été modifié : un autre programme l’a ouvert ou verrouillé.",
               "es": "El archivo no se ha modificado: otro programa lo tiene abierto o bloqueado.", "it": "Il file non è stato modificato: un altro programma lo tiene aperto o bloccato.",
               "pt": "O ficheiro não foi alterado: outro programa tem-no aberto ou bloqueado."},
    "no_space": {"ru": "Файл не изменён: на диске нет места.", "en": "The file was not changed: the disk is full.",
                 "de": "Die Datei wurde nicht geändert: Der Datenträger ist voll.", "fr": "Le fichier n’a pas été modifié : le disque est plein.",
                 "es": "El archivo no se ha modificado: el disco está lleno.", "it": "Il file non è stato modificato: il disco è pieno.",
                 "pt": "O ficheiro não foi alterado: o disco está cheio."},
}


def message(kind: str, code: str, **values: str) -> str:
    row = _MESSAGES.get(kind, _MESSAGES["unknown"])
    return row.get(code, row["en"]).format(**values)

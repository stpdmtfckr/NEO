"""Text features of the NEO command model: hashed words, word pairs and character n-grams.

Hashing (CRC32, stable between runs and machines) keeps the model small and
language-agnostic: no vocabulary files, unseen words still share n-grams with
known ones, typos and inflections land near the right features.
"""
from __future__ import annotations

import re
import unicodedata
import zlib

import numpy as np

TOKEN = re.compile(r'\S+')
_EDGE = re.compile(r'^[^\w]+|[^\w]+$', re.U)


def normalize(text: str) -> str:
    value = unicodedata.normalize('NFC', str(text or '')).casefold().replace('ё', 'е')
    value = re.sub(r"[«»\"“”„‘’`´]", ' ', value)
    return ' '.join(value.split())


def tokens(text: str) -> list[tuple[str, int, int]]:
    """(normalized token, start, end) over the original text; punctuation at the edges is dropped."""
    out = []
    for match in TOKEN.finditer(str(text or '')):
        raw = match.group(0)
        core = _EDGE.sub('', unicodedata.normalize('NFC', raw).casefold().replace('ё', 'е'))
        if not core:
            continue
        lead = len(raw) - len(raw.lstrip('«»"“”„\'(.,;:!?¿¡'))
        tail = len(raw) - len(raw.rstrip('«»"“”„\'),.;:!?…'))
        out.append((core, match.start() + lead, match.end() - tail))
    return out


def _hash(value: str, dim: int) -> int:
    return zlib.crc32(value.encode('utf-8')) % dim


def sentence_features(text: str, code: str, context: str, dim: int) -> tuple[np.ndarray, float]:
    """Hashed feature indices of a whole request and their scale (1/sqrt(n))."""
    words = [t for t, _, _ in tokens(normalize(text))]
    names: list[str] = [f'L:{code}', f'n:{min(len(words), 12)}']
    if context:
        names.append(f'C:{context}')
    for index, word in enumerate(words):
        names.append('w:' + word)
        if context:
            names.append(f'C:{context}|w:{word}')
        if index == 0:
            names.append('f:' + word)
        if index + 1 < len(words):
            names.append(f'b:{word}_{words[index + 1]}')
        padded = f' {word} '
        for size in (3, 4):
            names.extend('g:' + padded[i:i + size] for i in range(max(1, len(padded) - size + 1)))
    unique = np.unique(np.fromiter((_hash(n, dim) for n in names), dtype=np.int64, count=len(names)))
    return unique.astype(np.int32), 1.0 / np.sqrt(max(1, len(unique)))


def token_features(words: list[str], position: int, intent: str, dim: int) -> np.ndarray:
    """Hashed features of one token for the slot tagger."""
    word = words[position]
    prev1 = words[position - 1] if position > 0 else '<s>'
    prev2 = words[position - 2] if position > 1 else '<s>'
    next1 = words[position + 1] if position + 1 < len(words) else '</s>'
    shape = 'd' if word.isdigit() else 'p' if not word.isalnum() else 'w'
    names = [
        'w0:' + word, 'p1:' + prev1, 'p2:' + prev2, 'n1:' + next1, 'p1p2:' + prev2 + '_' + prev1,
        'pre:' + word[:3], 'suf:' + word[-3:], 'shape:' + shape, 'pos:' + str(min(position, 8)),
        'fromend:' + str(min(len(words) - position, 8)), 'I:' + intent, 'I:' + intent + '|p1:' + prev1,
        'I:' + intent + '|pos:' + str(min(position, 8)), 'I:' + intent + '|w0:' + word,
    ]
    return np.unique(np.fromiter((_hash(n, dim) for n in names), dtype=np.int64, count=len(names))).astype(np.int32)

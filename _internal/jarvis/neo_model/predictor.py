"""Load a saved NEO command model and run it (intent + slots)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from jarvis.neo_model.features import sentence_features, token_features, tokens
from jarvis.neo_model.network import IntentNet, SlotNet, make_batch


class NeoCommandModel:
    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self.meta = json.loads((self.folder / 'meta.json').read_text(encoding='utf-8'))
        self.labels: list[str] = list(self.meta['labels'])
        self.tags: list[str] = list(self.meta['tags'])
        self.intent_net = IntentNet.load(self.folder / 'intent.npz')
        self.slot_net = SlotNet.load(self.folder / 'slots.npz')
        self.dim = int(self.meta['dim'])
        self.slot_dim = int(self.meta['slot_dim'])

    @property
    def version(self) -> str:
        return self.folder.name

    def ranked(self, text: str, code: str, context: str = '', top: int = 3) -> list[tuple[str, float]]:
        batch = make_batch([sentence_features(text, code, context, self.dim)], [0])
        probs = self.intent_net.predict(batch)[0]
        best = np.argsort(-probs)[:top]
        return [(self.labels[i], float(probs[i])) for i in best]

    def ranked_many(self, rows: list[tuple[str, str, str]]) -> np.ndarray:
        batch = make_batch([sentence_features(t, c, x, self.dim) for t, c, x in rows], [0] * len(rows))
        return self.intent_net.predict(batch)

    def slots(self, text: str, intent: str) -> dict[str, str]:
        """Slot name -> text span of the original request (case and punctuation inside kept)."""
        items = tokens(text)
        if not items:
            return {}
        words = [w for w, _, _ in items]
        probs = self.slot_net.predict([token_features(words, i, intent, self.slot_dim) for i in range(len(words))])
        tags = [self.tags[int(i)] for i in probs.argmax(axis=1)]
        found: dict[str, tuple[int, int]] = {}
        for (word, start, end), tag in zip(items, tags):
            if tag == 'O':
                continue
            if tag in found and found[tag][1] >= start - 1:
                found[tag] = (found[tag][0], end)
            elif tag not in found:
                found[tag] = (start, end)
        return {tag: text[start:end].strip() for tag, (start, end) in found.items() if text[start:end].strip()}

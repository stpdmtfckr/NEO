"""The NEO command model: a small neural network in plain numpy.

* IntentNet: hashed sentence features -> embedding bag (sum of rows) -> ReLU hidden
  layer -> softmax over intents plus "__none__" (not a command).
* SlotNet: hashed token features -> softmax over slot tags ("O" or a slot name).

Both train with sparse Adam (only rows of features present in a batch are updated),
so training runs on an ordinary CPU in minutes.

Storage: the large hashed tables are saved as int8 with one float16 scale per row
(«w1_q»/«w1_s», «w_q»/«w_s»); rows no training example ever touched are saved as
zeros, so an unknown word adds nothing instead of random noise. Older float16
files («w1», «w») still load.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - logits.max(axis=1, keepdims=True)
    np.exp(shifted, out=shifted)
    shifted /= shifted.sum(axis=1, keepdims=True)
    return shifted


class _SparseAdam:
    def __init__(self, shape: tuple[int, ...], lr: float) -> None:
        self.m = np.zeros(shape, dtype=np.float32)
        self.v = np.zeros(shape, dtype=np.float32)
        self.t = 0
        self.lr = lr

    def step_rows(self, weights: np.ndarray, rows: np.ndarray, grad: np.ndarray, t: int) -> None:
        b1, b2, eps = 0.9, 0.999, 1e-8
        m = self.m[rows] * b1 + (1 - b1) * grad
        v = self.v[rows] * b2 + (1 - b2) * grad * grad
        self.m[rows], self.v[rows] = m, v
        m_hat = m / (1 - b1 ** t)
        v_hat = v / (1 - b2 ** t)
        weights[rows] -= self.lr * m_hat / (np.sqrt(v_hat) + eps)

    def step(self, weights: np.ndarray, grad: np.ndarray, t: int) -> None:
        b1, b2, eps = 0.9, 0.999, 1e-8
        self.m = self.m * b1 + (1 - b1) * grad
        self.v = self.v * b2 + (1 - b2) * grad * grad
        weights -= self.lr * (self.m / (1 - b1 ** t)) / (np.sqrt(self.v / (1 - b2 ** t)) + eps)


def quantize_rows(weights: np.ndarray, touched: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Symmetric per-row int8: row ≈ q * scale. Untouched rows become exact zeros."""
    w = np.asarray(weights, dtype=np.float32)
    if touched is not None:
        w = np.where(touched[:, None], w, 0.0)
    scale = np.abs(w).max(axis=1) / 127.0
    safe = np.where(scale > 0, scale, 1.0)
    q = np.rint(w / safe[:, None]).clip(-127, 127).astype(np.int8)
    return q, scale.astype(np.float16)


class QuantizedRows:
    """Read-only int8 table; ``table[indices]`` returns float32 rows like an ndarray."""

    def __init__(self, q: np.ndarray, scale: np.ndarray) -> None:
        self.q, self.scale = q, scale

    @property
    def shape(self) -> tuple[int, int]:
        return self.q.shape

    @property
    def dtype(self):
        return self.q.dtype

    @property
    def nbytes(self) -> int:
        return self.q.nbytes + self.scale.nbytes

    def __getitem__(self, index):
        return self.q[index].astype(np.float32) * self.scale[index].astype(np.float32)[..., None]


def _table(data, name: str):
    if name + '_q' in data.files:
        return QuantizedRows(data[name + '_q'], data[name + '_s'])
    return data[name]          # float16 file from an earlier build


@dataclass
class Batch:
    indices: np.ndarray    # concatenated feature indices
    starts: np.ndarray     # start offset of every example
    scales: np.ndarray     # per-example scale
    labels: np.ndarray


def make_batch(rows: list[tuple[np.ndarray, float]], labels: Iterable[int]) -> Batch:
    lengths = np.fromiter((len(r[0]) for r in rows), dtype=np.int64, count=len(rows))
    starts = np.zeros(len(rows), dtype=np.int64)
    if len(rows) > 1:
        starts[1:] = np.cumsum(lengths)[:-1]
    indices = np.concatenate([r[0] for r in rows]) if rows else np.zeros(0, dtype=np.int32)
    return Batch(indices, starts, np.array([r[1] for r in rows], dtype=np.float32), np.fromiter(labels, dtype=np.int64))


class IntentNet:
    def __init__(self, dim: int, hidden: int, classes: int, seed: int = 7) -> None:
        rng = np.random.default_rng(seed)
        self.dim, self.hidden, self.classes = dim, hidden, classes
        self.w1 = (rng.standard_normal((dim, hidden)) * 0.1).astype(np.float32)
        self.b1 = np.zeros(hidden, dtype=np.float32)
        self.w2 = (rng.standard_normal((hidden, classes)) * np.sqrt(2.0 / hidden)).astype(np.float32)
        self.b2 = np.zeros(classes, dtype=np.float32)
        self.touched = np.zeros(dim, dtype=bool)

    def _hidden(self, batch: Batch) -> tuple[np.ndarray, np.ndarray]:
        gathered = self.w1[batch.indices].astype(np.float32, copy=False)
        summed = np.add.reduceat(gathered, batch.starts, axis=0) if len(batch.indices) else np.zeros((len(batch.starts), self.hidden), np.float32)
        pre = summed * batch.scales[:, None] + self.b1
        return pre, np.maximum(pre, 0.0)

    def predict(self, batch: Batch) -> np.ndarray:
        _, hidden = self._hidden(batch)
        return _softmax(hidden @ self.w2 + self.b2)

    def train(self, batches: Iterable[Batch], *, lr: float = 0.004, epochs: int = 1, log=None) -> None:
        opt_w1 = _SparseAdam(self.w1.shape, lr)
        opt_b1, opt_w2, opt_b2 = _SparseAdam(self.b1.shape, lr), _SparseAdam(self.w2.shape, lr), _SparseAdam(self.b2.shape, lr)
        step = 0
        batches = list(batches)
        rng = np.random.default_rng(11)
        for epoch in range(epochs):
            loss_sum, seen = 0.0, 0
            for batch in (batches[i] for i in rng.permutation(len(batches))):
                step += 1
                pre, hidden = self._hidden(batch)
                probs = _softmax(hidden @ self.w2 + self.b2)
                n = len(batch.labels)
                loss_sum += float(-np.log(probs[np.arange(n), batch.labels] + 1e-9).sum())
                seen += n
                grad_logits = probs
                grad_logits[np.arange(n), batch.labels] -= 1.0
                grad_logits /= n
                grad_w2 = hidden.T @ grad_logits
                grad_b2 = grad_logits.sum(axis=0)
                grad_hidden = grad_logits @ self.w2.T
                grad_hidden[pre <= 0] = 0.0
                grad_b1 = grad_hidden.sum(axis=0)
                per_example = grad_hidden * batch.scales[:, None]
                lengths = np.diff(np.append(batch.starts, len(batch.indices)))
                contrib = np.repeat(per_example, lengths, axis=0)
                rows, inverse = np.unique(batch.indices, return_inverse=True)
                grad_rows = np.zeros((len(rows), self.hidden), dtype=np.float32)
                np.add.at(grad_rows, inverse, contrib)
                opt_w1.step_rows(self.w1, rows, grad_rows, step)
                self.touched[rows] = True
                opt_b1.step(self.b1, grad_b1, step)
                opt_w2.step(self.w2, grad_w2, step)
                opt_b2.step(self.b2, grad_b2, step)
            if log:
                log(f'intent epoch {epoch + 1}/{epochs}: loss {loss_sum / max(1, seen):.4f}')

    def save(self, path: Path) -> None:
        touched = getattr(self, 'touched', None)
        w1_q, w1_s = quantize_rows(self.w1, touched if touched is not None and touched.any() else None)
        np.savez_compressed(path, w1_q=w1_q, w1_s=w1_s, b1=self.b1, w2=self.w2.astype(np.float16), b2=self.b2)

    @classmethod
    def load(cls, path: Path) -> 'IntentNet':
        data = np.load(path)
        net = cls.__new__(cls)
        net.w1 = _table(data, 'w1')      # stays int8 (or float16) in memory; rows are widened when used
        net.b1, net.w2, net.b2 = data['b1'].astype(np.float32), data['w2'].astype(np.float32), data['b2'].astype(np.float32)
        net.dim, net.hidden = net.w1.shape
        net.classes = net.w2.shape[1]
        return net


class SlotNet:
    """Linear softmax tagger over hashed token features."""

    def __init__(self, dim: int, tags: int, seed: int = 5) -> None:
        self.dim, self.tags = dim, tags
        self.w = np.zeros((dim, tags), dtype=np.float32)
        self.b = np.zeros(tags, dtype=np.float32)

    def predict(self, rows: list[np.ndarray]) -> np.ndarray:
        batch = make_batch([(r, 1.0) for r in rows], [0] * len(rows))
        summed = np.add.reduceat(self.w[batch.indices].astype(np.float32, copy=False), batch.starts, axis=0) if len(batch.indices) else np.zeros((len(rows), self.tags), np.float32)
        return _softmax(summed + self.b)

    def train(self, rows: list[np.ndarray], labels: list[int], *, lr: float = 0.02, epochs: int = 4, batch_size: int = 512, log=None) -> None:
        opt_w, opt_b = _SparseAdam(self.w.shape, lr), _SparseAdam(self.b.shape, lr)
        rng = np.random.default_rng(3)
        labels_arr = np.array(labels, dtype=np.int64)
        step = 0
        for epoch in range(epochs):
            order = rng.permutation(len(rows))
            loss_sum = 0.0
            for start in range(0, len(order), batch_size):
                step += 1
                chosen = order[start:start + batch_size]
                batch = make_batch([(rows[i], 1.0) for i in chosen], labels_arr[chosen])
                summed = np.add.reduceat(self.w[batch.indices], batch.starts, axis=0)
                probs = _softmax(summed + self.b)
                n = len(chosen)
                loss_sum += float(-np.log(probs[np.arange(n), batch.labels] + 1e-9).sum())
                grad = probs
                grad[np.arange(n), batch.labels] -= 1.0
                grad /= n
                lengths = np.diff(np.append(batch.starts, len(batch.indices)))
                contrib = np.repeat(grad, lengths, axis=0)
                rows_u, inverse = np.unique(batch.indices, return_inverse=True)
                grad_rows = np.zeros((len(rows_u), self.tags), dtype=np.float32)
                np.add.at(grad_rows, inverse, contrib)
                opt_w.step_rows(self.w, rows_u, grad_rows, step)
                opt_b.step(self.b, grad.sum(axis=0), step)
            if log:
                log(f'slot epoch {epoch + 1}/{epochs}: loss {loss_sum / max(1, len(order)):.4f}')

    def save(self, path: Path) -> None:
        w_q, w_s = quantize_rows(self.w)          # zero-initialised: untouched rows are already zero
        np.savez_compressed(path, w_q=w_q, w_s=w_s, b=self.b)

    @classmethod
    def load(cls, path: Path) -> 'SlotNet':
        data = np.load(path)
        net = cls.__new__(cls)
        net.w, net.b = _table(data, 'w'), data['b'].astype(np.float32)
        net.dim, net.tags = net.w.shape
        return net

"""Bounded in-memory PCM bridge between wake and full command recognition."""
from collections import deque
import math
import numpy as np


class AudioHistory:
    def __init__(self, seconds=12.0):
        self.seconds = float(seconds)
        self.blocks = deque()
        self.rate = 0

    def clear(self):
        self.blocks.clear()
        self.rate = 0

    def append(self, end, audio, rate):
        if self.rate and self.rate != rate:
            self.clear()
        self.rate = rate
        data = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not data.size or not math.isfinite(end):
            return
        self.blocks.append((end - data.size / rate, end, data.copy()))
        cutoff = end - self.seconds
        while self.blocks and self.blocks[0][1] <= cutoff:
            self.blocks.popleft()

    def since(self, start, now, max_age=11.0):
        if not start or not math.isfinite(start) or not 0 <= now-start <= max_age:
            return []
        result = []
        for beginning, end, data in self.blocks:
            if end <= start or beginning > now:
                continue
            offset = max(0, min(data.size, int(round((start-beginning)*self.rate))))
            if offset < data.size:
                result.append(data[offset:].copy())
        return result

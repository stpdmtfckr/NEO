"""Low frequency, log-only evidence of stalls in the actual Qt event loop."""
from __future__ import annotations

import logging
import time
from PySide6.QtCore import QObject, QTimer, Qt

logger = logging.getLogger(__name__)


class UiResponsivenessMonitor(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        self._previous = time.monotonic()
        self._last_report = self._previous
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.CoarseTimer)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._sample)
        self._timer.start()

    def _sample(self):
        now = time.monotonic()
        lag = max(0.0, now - self._previous - .5)
        self._previous = now
        if lag >= .4 and now - self._last_report >= 10:
            self._last_report = now
            logger.warning("Qt event loop delayed: lag_ms=%.0f", lag * 1000)


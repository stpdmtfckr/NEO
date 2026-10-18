"""NEO 172: time of one robot animation frame (advance + paint) per state, offscreen.

    python tools/frame_bench172.py [--frames 240] [--json out.json]

Used by tests/test_neo172_frames.py: every build must stay within the saved baseline,
so an optimisation elsewhere never makes the animation slower.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def measure(frames: int = 240, states: tuple[str, ...] | None = None) -> dict[str, dict[str, float]]:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtWidgets import QApplication
    from jarvis.ui.robot_widget import RobotWidget
    app = QApplication.instance() or QApplication([])
    widget = RobotWidget()
    widget.resize(520, 300)
    image = QImage(520, 300, QImage.Format_ARGB32_Premultiplied)
    results: dict[str, dict[str, float]] = {}
    for state in states or tuple(sorted(RobotWidget.STATES)):
        widget.set_state(state)
        for _ in range(20):                      # settle the transition, warm caches
            widget._advance_animation()
        samples = []
        for _ in range(frames):
            image.fill(Qt.transparent)
            started = time.perf_counter()
            widget._advance_animation()
            painter = QPainter(image)
            widget.render(painter, QPoint())
            painter.end()
            samples.append((time.perf_counter() - started) * 1000)
        samples.sort()
        results[state] = {'mean_ms': round(statistics.fmean(samples), 3), 'median_ms': round(statistics.median(samples), 3),
                          'p95_ms': round(samples[int(len(samples) * 0.95) - 1], 3)}
    widget.deleteLater()
    app.processEvents()
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--frames', type=int, default=240)
    parser.add_argument('--json', default='')
    args = parser.parse_args(argv)
    result = measure(args.frames)
    text = json.dumps(result, indent=1, ensure_ascii=False)
    print(text)
    if args.json:
        Path(args.json).write_text(text + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

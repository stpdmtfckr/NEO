from pathlib import Path
import unittest

from tests.release_contracts import assert_release

ROOT = Path(__file__).resolve().parents[1]
ROBOT = ROOT / "jarvis" / "ui" / "robot_widget.py"
VERSION = ROOT / "VERSION.txt"

from tests.release_contracts import assert_headphone_geometry

class Hotfix128HeadphoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = ROBOT.read_text(encoding="utf-8")

    def test_headphones_are_anchored_to_real_side_ears(self):
        assert_headphone_geometry(self, self.src)
        self.assertNotIn("cx - 69.0 * scale, cy - 19.0 * scale - drop", self.src)
        self.assertNotIn("cx + 50.0 * scale, cy - 19.0 * scale - drop", self.src)

    def test_band_meets_ear_cups_and_moves_with_head(self):
        self.assertIn("QPointF(cx - 119.0 * scale", self.src)
        self.assertIn("QPointF(cx + 119.0 * scale", self.src)
        self.assertIn("_draw_idle_toy(painter, head_x, head_y", self.src)

    def test_headphone_pads_are_visible(self):
        import re
        left, right, width = assert_headphone_geometry(self, self.src)
        self.assertIn('pad = QColor(self._colors["screen"])', self.src)
        block = self.src.split('pad = QColor(self._colors["screen"])', 1)[1].split("note_glyphs", 1)[0]
        pads = re.findall(r"QRectF\(cx ([+-]) ([\d.]+) \* scale, pad_y, ([\d.]+) \* scale", block)
        self.assertEqual(len(pads), 2)
        for (sign, offset, pad_width), outer in zip(pads, (-left, right)):
            x = float(offset) * (-1 if sign == "-" else 1)
            self.assertGreaterEqual(x, outer)
            self.assertLessEqual(x + float(pad_width), outer + width)

    def test_version_marker(self):
        # Old hotfix markers were folded into NEO releases; the release contract is shared (see release_contracts).
        assert_release(self, VERSION.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()

# NEO_PROJECT_SIGNATURE: s7Q!91_tA4#2pX8$0dR3%6mK5^1tZ9&4fV2*8cN7!0kL3@5r
from __future__ import annotations

import unittest
from pathlib import Path

from jarvis.integrations.bluetooth_devices import _friendly_bluetooth_name
from jarvis.integrations.telegram_keys import canonical_key, parse_remote_key_input


ROOT = Path(__file__).resolve().parents[1]


class TelegramRemoteKeyTests(unittest.TestCase):
    def test_single_special_key(self) -> None:
        parsed = parse_remote_key_input("Enter")
        self.assertEqual(parsed.mode, "key")
        self.assertEqual(parsed.keys, ("enter",))

    def test_chord_uses_plus(self) -> None:
        parsed = parse_remote_key_input("Ctrl+Backspace+Enter")
        self.assertEqual(parsed.mode, "chord")
        self.assertEqual(parsed.keys, ("ctrl", "backspace", "enter"))

    def test_known_keys_without_plus_are_sequential(self) -> None:
        parsed = parse_remote_key_input("Ctrl Backspace Enter")
        self.assertEqual(parsed.mode, "sequence")
        self.assertEqual(parsed.keys, ("ctrl", "backspace", "enter"))

    def test_normal_word_is_literal_unicode_text(self) -> None:
        parsed = parse_remote_key_input("где")
        self.assertEqual(parsed.mode, "text")
        self.assertEqual(parsed.text, "где")

    def test_requested_aliases(self) -> None:
        self.assertEqual(canonical_key("Control"), "ctrl")
        self.assertEqual(canonical_key("Правый Shift"), "rshift")
        self.assertEqual(canonical_key("Page Up"), "pageup")
        self.assertEqual(canonical_key("стрелочка влево"), "left")
        self.assertEqual(canonical_key("Fn"), "fn")


class BluetoothPresentationTests(unittest.TestCase):
    def test_service_nodes_are_hidden(self) -> None:
        for value in (
            "Device Identification Service",
            "Служба идентификации устройств",
            "Bluetooth LE Device",
            "Generic Attribute Service",
            "Microsoft Bluetooth LE Enumerator",
        ):
            self.assertEqual(_friendly_bluetooth_name(value), "")

    def test_real_product_name_is_kept(self) -> None:
        self.assertEqual(_friendly_bluetooth_name("Sony WH-1000XM5"), "Sony WH-1000XM5")


class TelegramMenuStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (ROOT / "jarvis" / "integrations" / "telegram_remote.py").read_text(encoding="utf-8")

    def test_home_has_full_audio_and_keyboard_entry(self) -> None:
        self.assertIn('("🎙 Ввод", "ui:mic")', self.source)
        self.assertIn('("🎧 Вывод", "ui:vol")', self.source)
        self.assertIn('("⌨️ Нажать клавишу", "ui:key")', self.source)
        self.assertIn('("🎵 Музыка", "ui:mm")', self.source)

    def test_system_menu_no_longer_duplicates_audio(self) -> None:
        start = self.source.index("    def _show_system_menu")
        end = self.source.index("    def _show_power_menu", start)
        block = self.source[start:end]
        self.assertNotIn('("🔊 Звук", "ui:vol")', block)
        self.assertNotIn('("🎙 Микрофон", "ui:mic")', block)

    def test_keyboard_helper_is_telegram_only(self) -> None:
        matches = []
        for path in (ROOT / "jarvis").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "execute_remote_key_input" in text:
                matches.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(sorted(matches), [
            "jarvis/integrations/telegram_keys.py",
            "jarvis/integrations/telegram_remote.py",
        ])


if __name__ == "__main__":
    unittest.main()

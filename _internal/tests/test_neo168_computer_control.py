"""NEO 168: keyboard parsing, ordinals, on-screen elements, screen cards, clipboard text, reply memory."""
from __future__ import annotations

import json
import unittest

from jarvis.runtime import clipboard168, input168, reply_memory168, screen_card168
from jarvis.runtime import ui_elements168 as elements


def _el(name, x, y, w=80, h=20, kind='Hyperlink'):
    return elements.Element(name, kind, (x, y, x + w, y + h))


class KeySpecTests(unittest.TestCase):
    def test_named_keys_in_several_languages(self):
        cases = [('пробел', 'ru', ('space',)), ('Enter', 'en', ('enter',)), ('Leertaste', 'de', ('space',)),
                 ('échap', 'fr', ('esc',)), ('flecha arriba', 'es', ('up',)), ('invio', 'it', ('enter',)),
                 ('seta esquerda', 'pt', ('left',)), ('F5', 'en', ('f5',)), ('эф пять', 'ru', ('f5',))]
        for text, code, keys in cases:
            with self.subTest(text=text):
                self.assertEqual(input168.parse_key_spec(text, code).keys, keys)

    def test_shortcuts_put_modifiers_first(self):
        self.assertEqual(input168.parse_key_spec('ctrl+s', 'en').keys, ('ctrl', 's'))
        self.assertEqual(input168.parse_key_spec('контрол шифт эс', 'ru').keys, ('ctrl', 'shift', 's'))
        self.assertEqual(input168.parse_key_spec('ctrl+s', 'en').label, 'Ctrl+S')

    def test_repeat_count_is_capped(self):
        self.assertEqual(input168.parse_key_spec('E 3 раза', 'ru').count, 3)
        self.assertEqual(input168.parse_key_spec('space twice', 'en').count, 2)
        self.assertEqual(input168.parse_key_spec('enter 50 times', 'en').count, 10)

    def test_timed_holds_are_not_supported(self):
        self.assertIsNone(input168.parse_key_spec('W на 2 секунды', 'ru'))
        self.assertIsNone(input168.parse_key_spec('w for 3 seconds', 'en'))

    def test_non_key_names_are_rejected(self):
        # «нажми Сохранить» is a button name, handled by input_control.click_control.
        self.assertIsNone(input168.parse_key_spec('Сохранить', 'ru'))
        self.assertIsNone(input168.parse_key_spec('a b c', 'en'))


class OrdinalTests(unittest.TestCase):
    def test_ordinals(self):
        cases = [('первую', 'ru', 1), ('3-ю', 'ru', 3), ('номер 5', 'ru', 5), ('последнюю', 'ru', -1),
                 ('third', 'en', 3), ('dritte', 'de', 3), ('deuxième', 'fr', 2), ('última', 'es', -1),
                 ('nona', 'it', 9), ('segundo', 'pt', 2), ('7', 'en', 7)]
        for text, code, value in cases:
            with self.subTest(text=text):
                self.assertEqual(input168.parse_ordinal(text, code), value)

    def test_zero_and_garbage(self):
        self.assertIsNone(input168.parse_ordinal('0', 'en'))
        self.assertIsNone(input168.parse_ordinal('банан', 'ru'))


class ElementTests(unittest.TestCase):
    def test_reading_order_rows_then_columns(self):
        found = [_el('c', 300, 102), _el('a', 10, 100), _el('d', 10, 200), _el('b', 150, 95)]
        self.assertEqual([e.name for e in elements.reading_order(found)], ['a', 'b', 'c', 'd'])

    def test_usable_drops_tiny_outside_unnamed_and_duplicates(self):
        window = (0, 0, 1000, 800)
        found = [_el('ok', 10, 100), _el('tiny', 10, 200, w=3), _el('outside', 1200, 100), _el('', 10, 300),
                 _el('ok', 10, 100), _el('Close', 900, 5, kind='Button'), _el('chrome', 10, 50)]
        kept = [e.name for e in elements.usable(found, window, top_floor=80)]
        self.assertEqual(kept, ['ok'])

    def test_page_links_skip_navigation_and_breadcrumb_twins(self):
        found = [_el('Images', 10, 100), _el('Python tutorial', 10, 200, w=300),
                 _el('Python tutorial › docs', 10, 215, w=300), _el('Second result', 10, 300, w=300)]
        self.assertEqual([e.name for e in elements.page_links(found)], ['Python tutorial', 'Second result'])

    def test_pick_supports_last(self):
        found = [_el('a', 0, 0), _el('b', 0, 30), _el('c', 0, 60)]
        self.assertEqual(elements.pick(found, 1).name, 'a')
        self.assertEqual(elements.pick(found, -1).name, 'c')
        self.assertIsNone(elements.pick(found, 4))
        self.assertIsNone(elements.pick(found, 0))

    def test_hints_are_numbered_and_forgotten(self):
        found = [_el('a', 0, 0), _el('b', 0, 30)]
        elements.remember(42, found)
        hwnd, element = elements.hinted(2)
        self.assertEqual((hwnd, element.name), (42, 'b'))
        elements.forget()
        self.assertEqual(elements.hinted(2), (0, None))

    def test_stale_hints_expire(self):
        elements.remember(7, [_el('a', 0, 0)])
        self.assertEqual(elements.hinted(1, max_age=-1.0), (0, None))
        elements.forget()


class StaleElementTests(unittest.TestCase):
    def test_moved_element_is_not_clicked(self):
        from jarvis.tools import ui_control168

        class Wrapper:
            def __init__(self, rect):
                self.rect = rect

            def rectangle(self):
                left, top, right, bottom = self.rect
                return type('R', (), {'left': left, 'top': top, 'right': right, 'bottom': bottom})()

        element = elements.Element('Send', 'Button', (100, 100, 180, 130), Wrapper((100, 100, 180, 130)))
        self.assertTrue(ui_control168._still_there(element))
        element.wrapper.rect = (100, 400, 180, 430)
        self.assertFalse(ui_control168._still_there(element))


class RepeatTests(unittest.TestCase):
    class Config:
        def get(self, key, default=None):
            return 'en' if key == 'app.language' else default

    def setUp(self):
        from jarvis.tools import ui_control168
        self.ui = ui_control168
        self.ui._LAST.clear()

    def test_nothing_to_repeat(self):
        result = self.ui.again(self.Config(), None)
        self.assertFalse(result.success)
        self.assertIn('Nothing to repeat', result.text)

    def test_repeat_click_refuses_when_window_changed(self):
        from unittest import mock
        element = elements.Element('Send', 'Button', (0, 0, 50, 20), object())
        self.ui._remember('click', hwnd=11, element=element, how='click')
        with mock.patch.object(elements, 'foreground', return_value=(22, 'x.exe', None)), \
                mock.patch.object(self.ui, '_press_element') as press:
            result = self.ui.again(self.Config(), None)
        press.assert_not_called()
        self.assertFalse(result.success)
        self.assertTrue(result.data.get('safe_failure'))

    def test_repeat_key_uses_the_same_key(self):
        from unittest import mock
        self.ui._remember('press', key='enter')
        with mock.patch.object(input168, 'press') as press:
            result = self.ui.again(self.Config(), None)
        self.assertTrue(result.success)
        self.assertEqual(press.call_args[0][0].keys, ('enter',))


class ScreenCardTests(unittest.TestCase):
    def test_card_renders_sections_and_skips_empty(self):
        raw = 'Here: ' + json.dumps({'app': 'Chrome', 'summary': 'A news page.', 'answer': '',
                                     'key_points': ['one', 'one', 'two'], 'visible_text': ['Hello'],
                                     'problems': [], 'next_steps': []})
        card = screen_card168.answer_text(raw, 'en')
        self.assertIn('🖥 Chrome', card)
        self.assertEqual(card.count('• one'), 1)
        self.assertIn('«Hello»', card)

    def test_non_json_reply_is_cleaned(self):
        self.assertEqual(screen_card168.answer_text('## Title\n- **bold** item', 'en'), 'Title\n• bold item')

    def test_prompt_mentions_language_and_ocr(self):
        prompt = screen_card168.build_prompt('de', window='Word', ocr_lines=['Rechnung 2024'])
        self.assertIn('German', prompt)
        self.assertIn('Rechnung 2024', prompt)

    def test_read_card_without_text(self):
        self.assertTrue(screen_card168.read_card('Notepad', [], 'ru').startswith('🖥 Notepad'))


class ClipboardDescribeTests(unittest.TestCase):
    def test_descriptions(self):
        self.assertIn('«Добрый день»', clipboard168.describe(clipboard168.Content('text', text='Добрый  день'), 'ru'))
        self.assertEqual(clipboard168.describe(clipboard168.Content('files', files=[r'C:\a\report.docx']), 'en'),
                         'file report.docx')
        many = clipboard168.Content('files', files=['1.txt', '2.txt', '3.txt', '4.txt', '5.txt'])
        self.assertIn('2', clipboard168.describe(many, 'de').split('noch')[-1])
        self.assertEqual(clipboard168.describe(clipboard168.Content('image', image_size=(1920, 1080)), 'fr'),
                         'une image 1920×1080')
        self.assertEqual(clipboard168.describe(clipboard168.Content('empty'), 'es'), 'nada')

    def test_preview_is_short(self):
        self.assertLessEqual(len(clipboard168.preview('x' * 500)), 60)


class ReplyMemoryTests(unittest.TestCase):
    def setUp(self):
        reply_memory168.clear()

    def test_last_and_previous(self):
        reply_memory168.remember('desktop', 'first')
        reply_memory168.remember('desktop', 'second')
        reply_memory168.remember('telegram:1', 'other chat')
        self.assertEqual(reply_memory168.last('desktop'), 'second')
        self.assertEqual(reply_memory168.last('desktop', back=2), 'first')
        self.assertEqual(reply_memory168.last('desktop', back=3), '')

    def test_copy_acknowledgements_are_not_remembered(self):
        reply_memory168.remember('desktop', 'real answer')
        reply_memory168.remember('desktop', 'Copied text', tools=['copy.reply'])
        self.assertEqual(reply_memory168.last('desktop'), 'real answer')


if __name__ == '__main__':
    unittest.main()


class MessageTests(unittest.TestCase):
    def test_every_reply_formats_in_every_language_with_the_same_placeholders(self):
        import string
        from jarvis.tools.ui_messages168 import MESSAGES, msg
        for name, row in MESSAGES.items():
            fields = [{f for _, f, _, _ in string.Formatter().parse(text) if f} for text in row]
            self.assertTrue(all(f == fields[0] for f in fields), name)
            for code in ('ru', 'en', 'de', 'fr', 'es', 'it', 'pt'):
                self.assertTrue(msg(name, code, **{f: 'x' for f in fields[0]}))


class PasteToDiskTests(unittest.TestCase):
    class Config:
        def get(self, key, default=None):
            return 'en' if key == 'app.language' else default

    def setUp(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest import mock
        from jarvis.tools import copy_tool168
        self.tool = copy_tool168
        self.root = Path(tempfile.mkdtemp())
        self.registry = SimpleNamespace(security=SimpleNamespace(assert_mutation_path_allowed=lambda p: p))
        self.clip = mock.patch.object(clipboard168, 'read')
        self.read = self.clip.start()
        self.addCleanup(self.clip.stop)

    def test_text_is_appended_on_its_own_line(self):
        note = self.root / 'note.txt'
        note.write_text('first', encoding='utf-8')
        self.read.return_value = clipboard168.Content('text', text='second')
        result = self.tool.paste_file(self.Config(), self.registry, str(note))
        self.assertTrue(result.success, result.text)
        self.assertEqual(note.read_text(encoding='utf-8'), 'first\nsecond\n')
        self.assertIn('note.txt', result.text)

    def test_only_text_goes_into_a_file(self):
        note = self.root / 'note.txt'
        note.write_text('', encoding='utf-8')
        self.read.return_value = clipboard168.Content('image', image_size=(10, 10))
        self.assertFalse(self.tool.paste_file(self.Config(), self.registry, str(note)).success)
        self.assertEqual(note.read_text(encoding='utf-8'), '')

    def test_files_are_copied_into_a_folder_without_overwriting(self):
        source = self.root / 'report.txt'
        source.write_text('data', encoding='utf-8')
        target = self.root / 'out'
        target.mkdir()
        (target / 'report.txt').write_text('old', encoding='utf-8')
        self.read.return_value = clipboard168.Content('files', files=[str(source)])
        result = self.tool.paste_folder(self.Config(), self.registry, str(target))
        self.assertTrue(result.success, result.text)
        self.assertEqual((target / 'report.txt').read_text(encoding='utf-8'), 'old')
        self.assertEqual((target / 'report (2).txt').read_text(encoding='utf-8'), 'data')

    def test_empty_clipboard_is_reported(self):
        self.read.return_value = clipboard168.Content('empty')
        self.assertFalse(self.tool.paste_folder(self.Config(), self.registry, str(self.root)).success)


class ButtonNameTests(unittest.TestCase):
    def test_filler_words_are_dropped_only_before_a_button_word(self):
        from jarvis.tools.ui_control168 import button_name
        self.assertEqual(button_name('на кнопку Отправить'), 'Отправить')
        self.assertEqual(button_name('on the button Send'), 'Send')
        self.assertEqual(button_name('auf die Schaltfläche Senden'), 'Senden')
        self.assertEqual(button_name('На главную'), 'На главную')
        self.assertEqual(button_name('Отмена'), 'Отмена')


class LayoutRepairTests(unittest.TestCase):
    def test_a_lone_latin_letter_is_a_key_not_a_wrong_layout_word(self):
        from jarvis.runtime.command_typos165 import fix_layout
        self.assertEqual(fix_layout('нажми E 3 раза', 'ru'), 'нажми E 3 раза')
        self.assertEqual(fix_layout('jnrhjq pfuheprb b ljrevtyns', 'ru'), 'открой загрузки и документы')

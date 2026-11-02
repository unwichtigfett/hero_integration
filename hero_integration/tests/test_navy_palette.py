"""Navy is opt-in, changes color properties only and keeps readable text pairs."""
from pathlib import Path
import re
import unittest

CSS = Path(__file__).resolve().parents[1] / 'public/css/navy_theme.css'


def luminance(color):
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    channels = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in channels]
    return sum(c * weight for c, weight in zip(channels, (.2126, .7152, .0722)))


def contrast(first, second):
    low, high = sorted((luminance(first), luminance(second)))
    return (high + .05) / (low + .05)


class TestNavyPalette(unittest.TestCase):
    def setUp(self):
        self.css = re.sub(r'/\*.*?\*/', '', CSS.read_text(), flags=re.S)
        first_block = self.css.split('{', 1)[1].split('}', 1)[0]
        self.tokens = dict(re.findall(r'(--[\w-]+):\s*([^;]+);', first_block))

    def test_rules_are_scoped_to_navy_and_do_not_change_geometry(self):
        for selector in re.findall(r'([^{}]+)\{', self.css):
            if selector.strip().startswith('@media'):
                continue
            for part in selector.split(','):
                self.assertIn('navy', part)
                self.assertIn('[data-', part)
        properties = re.findall(r'(?:^|[;{])\s*([\w-]+)\s*:', self.css, re.M)
        for property_name in properties:
            self.assertTrue(property_name.startswith('--') or property_name in {
                'color', 'background-color', 'border-color', 'color-scheme',
            }, property_name)
        self.assertNotRegex(self.css, r'--(?:gray|blue|green|red)-\d+:')

    def test_main_text_muted_text_and_placeholders_have_readable_contrast(self):
        for foreground in ('--ink-gray-8', '--ink-gray-6', '--ink-gray-3'):
            for background in ('--surface-base', '--surface-elevation-1', '--surface-elevation-3'):
                with self.subTest(foreground=foreground, background=background):
                    self.assertGreaterEqual(contrast(self.tokens[foreground], self.tokens[background]), 4.5)

    def test_primary_button_and_link_accents_have_readable_contrast(self):
        for background in ('--btn-primary', '--navy-primary-hover'):
            self.assertGreaterEqual(contrast('#ffffff', self.tokens[background]), 4.5)
        for background in ('--surface-base', '--surface-elevation-1', '--surface-elevation-3'):
            self.assertGreaterEqual(contrast(self.tokens['--link-color'], self.tokens[background]), 4.5)
        focus = self.tokens['--focus-default'].split()[-1]
        for background in ('--surface-base', '--surface-elevation-1', '--surface-elevation-3'):
            self.assertGreaterEqual(contrast(focus, self.tokens[background]), 3)

    def test_standard_theme_status_colors_are_inherited(self):
        self.assertNotRegex(self.css, r'--(?:surface|ink|outline)-(?:red|green|amber|yellow|orange)-\d+:')
        self.assertEqual(self.tokens['--surface-base'], '#0d1117')
        self.assertEqual(self.tokens['--surface-elevation-1'], '#161b22')

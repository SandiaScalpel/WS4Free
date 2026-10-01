import subprocess
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

TAILWIND = Path(settings.BASE_DIR) / 'bin' / 'tailwindcss'
SOURCE = Path(settings.BASE_DIR) / 'assets' / 'css' / 'app.css'
BUILT = Path(settings.BASE_DIR) / 'weather' / 'static' / 'weather' / 'css' / 'app.css'


@unittest.skipUnless(TAILWIND.exists(), 'Tailwind CLI not downloaded (run ./scripts/build-css.sh once)')
class CompiledCssTests(SimpleTestCase):
    def test_committed_css_is_up_to_date(self):
        # Tailwind emits only classes it finds in templates, so a template change
        # without a rebuild ships unstyled markup. Rebuild and compare.
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'app.css'
            subprocess.run([str(TAILWIND), '-i', str(SOURCE), '-o', str(out), '--minify'],
                           cwd=SOURCE.parent, check=True, capture_output=True)
            self.assertEqual(out.read_text(), BUILT.read_text(),
                             'weather/static/weather/css/app.css is stale — run ./scripts/build-css.sh')

"""Keep a visible chat message in place when older history arrives."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node") or shutil.which("node", path=os.path.expanduser("~/.local/bin"))
CHROME = shutil.which("google-chrome")
VITE = ROOT / "web-react/node_modules/vite/dist/node/index.js"


@unittest.skipUnless(NODE and CHROME and VITE.exists(), "requires node, Chrome and web dependencies")
class HistoryScroll(unittest.TestCase):
    def test_prepend_keeps_the_visible_message_on_phone_and_desktop(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests/web_scroll.mjs"), CHROME],
            capture_output=True, text=True, timeout=90,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

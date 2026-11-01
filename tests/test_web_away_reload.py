"""An Answer after a long time away opens the call at once; the page starts over after it."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node") or shutil.which("node", path=os.path.expanduser("~/.local/bin"))


@unittest.skipUnless(NODE, "requires node")
class AwayReload(unittest.TestCase):
    def test_answer_opens_the_call_before_the_reload(self):
        result = subprocess.run(
            [NODE, str(ROOT / "tests/web_away_reload.mjs")],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

"""The chat's echo of his message (web-react/src/data/echo.js), run under node.

Skipped where node is not installed: the web has no test runner of its own."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

ECHO = Path(__file__).resolve().parent.parent / "web-react" / "src" / "data" / "echo.js"
NODE = shutil.which("node") or shutil.which("node", path=os.path.expanduser("~/.local/bin"))

CHECK = """
import { echoLanded } from %r
const held = [{ kind: 'text', who: 'operator',
  text: '[A message that arrived before this one]\\nhi\\n\\n[Now]\\n\\nbuy milk' }]
const out = [echoLanded(held, { text: 'buy milk' }), echoLanded(held, { text: 'buy' })]
console.log(JSON.stringify(out))
"""


@unittest.skipUnless(NODE, "node is not installed")
class Echo(unittest.TestCase):
    def test_his_words_after_now_land_the_echo(self):
        """22 October 2026: his message showed twice, once with its time and once "sending",
        because it arrived while a held message waited and the turn began with the held part."""
        got = subprocess.run([NODE, "--input-type=module", "-e", CHECK % str(ECHO)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(got.stdout.strip(), "[true,false]", got.stderr)


if __name__ == "__main__":
    unittest.main()

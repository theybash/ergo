"""The chat's markdown (web-react/src/screens/chat/markdown.js), run under node.

Skipped where node is not installed. The one import, fileUrl, is stubbed so the file runs alone."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MD = Path(__file__).resolve().parent.parent / "web-react" / "src" / "screens" / "chat" / "markdown.js"
NODE = shutil.which("node") or shutil.which("node", path=os.path.expanduser("~/.local/bin"))
STUB = "const fileUrl = (a, n) => '/file/' + a + '/' + n"


@unittest.skipUnless(NODE, "node is not installed")
class Markdown(unittest.TestCase):
    def test_an_underscore_in_a_picture_name_is_not_italic(self):
        """22 October 2026: camera photos, named JPEG_2026..., showed as a broken picture."""
        src = MD.read_text().replace("import { fileUrl } from '../../data/api'", STUB)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "md.mjs").write_text(src)
            run = (f"import {{ light }} from {str(Path(d, 'md.mjs'))!r}\n"
                   "console.log(light('![p](JPEG_2026_1.jpg) _it_', 'creator'))")
            got = subprocess.run([NODE, "--input-type=module", "-e", run],
                                 capture_output=True, text=True, timeout=30)
        self.assertIn('<img src="/file/creator/JPEG_2026_1.jpg?thumb=1"', got.stdout, got.stderr)
        self.assertIn('target="_blank"', got.stdout)
        self.assertIn("<em>it</em>", got.stdout)

    def test_a_delegate_shows_a_picture_from_its_parents_outbox_only(self):
        """A delegate's picture, saved in its parent's outbox, must not show broken."""
        src = MD.read_text().replace("import { fileUrl } from '../../data/api'", STUB)
        with tempfile.TemporaryDirectory() as d:
            Path(d, "md.mjs").write_text(src)
            run = (f"import {{ light }} from {str(Path(d, 'md.mjs'))!r}\n"
                   "console.log(light('![a](/x/.platform/outbox/acme/a.png) "
                   "![b](/x/.platform/outbox/creator/b.png)', 'acme-core'))")
            got = subprocess.run([NODE, "--input-type=module", "-e", run],
                                 capture_output=True, text=True, timeout=30)
        self.assertIn('<img src="/file/acme/a.png?thumb=1"', got.stdout, got.stderr)
        self.assertIn('<img src="/file/acme-core/b.png?thumb=1"', got.stdout)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from commandcenter import broker


class TrustFolder(unittest.TestCase):
    def test_a_new_folder_is_marked_trusted_and_the_rest_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / ".claude.json"
            state.write_text(json.dumps({"theme": "dark", "projects": {"/x": {"a": 1}}}))
            folder = Path(d) / "agent"
            folder.mkdir()
            self.assertTrue(broker.trust_folder(str(folder), state))
            held = json.loads(state.read_text())
            self.assertTrue(held["projects"][str(folder.resolve())]["hasTrustDialogAccepted"])
            self.assertEqual(held["theme"], "dark")
            self.assertEqual(held["projects"]["/x"], {"a": 1})
            self.assertFalse(broker.trust_folder(str(folder), state), "already trusted")

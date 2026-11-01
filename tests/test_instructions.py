"""The CLAUDE.md files on Config: a save writes the file, logs what it replaced for the master
agent to read, and Put back the previous swaps the two."""

import json
import tempfile
from pathlib import Path

from commandcenter import config, instructions
from tests.harness import Case


class Instructions(Case):
    def test_a_save_is_written_logged_and_can_be_put_back(self):
        home = Path(tempfile.mkdtemp())
        (home / ".claude").mkdir()
        (home / ".claude" / "CLAUDE.md").write_text("Global rules.\n")
        (home / "ergo" / "assistant").mkdir(parents=True)
        md = home / "ergo" / "assistant" / "CLAUDE.md"
        md.write_text("Old rule.\nKeep this.\n")
        saved = instructions.HOME
        instructions.HOME = home
        config.HOME = home / "ergo"
        self.addCleanup(lambda: setattr(instructions, "HOME", saved))

        names = [f["name"] for f in instructions.listing()["files"]]
        self.assertEqual(names, ["Global, every Claude", "Ergo: assistant"])
        key = instructions.listing()["files"][1]["key"]
        instructions.save(key, "Keep this.\n")
        self.assertEqual(md.read_text(), "Keep this.\n")
        row = json.loads((config.VAR / "instructions" / "edits.jsonl").read_text().splitlines()[-1])
        self.assertEqual((row["before"], row["after"]), ("Old rule.\nKeep this.\n", "Keep this.\n"))
        with self.assertRaises(ValueError):
            instructions.save(key, "   ")
        instructions.restore(key)
        self.assertEqual(md.read_text(), "Old rule.\nKeep this.\n")

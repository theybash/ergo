"""The open list: OPEN.md in an agent's folder, read for the Open tab."""

import tempfile
import unittest
from pathlib import Path

from commandcenter import openlist


class OpenList(unittest.TestCase):
    def test_each_list_line_is_an_item_with_its_heading_and_line(self):
        """The owner, 22 October 2026: things talked about and never done, easy to pick up."""
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(openlist.listing({"cwd": d})["exists"], False)
            Path(d, "OPEN.md").write_text(
                "# Open\n\nSome prose.\n\n## Waiting\n- 22 Oct. Feed icons\n  by source.\n\n"
                "   not part of it\n1. Poetry in the feed\n")
            got = openlist.listing({"cwd": d})
        self.assertEqual(got["items"], [
            {"n": 6, "section": "Waiting", "text": "22 Oct. Feed icons by source."},
            {"n": 10, "section": "Waiting", "text": "Poetry in the feed"}])


if __name__ == "__main__":
    unittest.main()

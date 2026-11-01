"""Whether an agent is working, asked of Claude Code instead of guessed.

The CLI is never actually run here. `_probe` is the seam: a test that shelled out would be testing
whether this laptop has Claude Code installed, and it would be slow.
"""

import time

from commandcenter import live
from tests.harness import Case


class Fake:
    """A terminal, as far as this module cares: something with a working() answer."""

    def __init__(self, working=False):
        self._working = working

    def working(self):
        return self._working


class Reading(Case):
    def setUp(self):
        super().setUp()
        live.reset()
        self.calls = 0
        self.rows = []
        real = live._probe
        def probe():
            self.calls += 1
            if isinstance(self.rows, Exception):
                raise self.rows
            return self.rows
        live._probe = probe
        self.addCleanup(lambda: (setattr(live, "_probe", real), live.reset()))

    def test_busy_means_working(self):
        rec = self.agent("box")
        self.rows = [{"sessionId": rec["session_id"], "status": "busy"}]
        self.assertTrue(live.working(rec, Fake(working=False)))

    def test_anything_else_means_not_working(self):
        rec = self.agent("box")
        self.rows = [{"sessionId": rec["session_id"], "status": "idle"}]
        self.assertFalse(live.working(rec, Fake(working=True)),
                         "the CLI knows better than recent output does")

    def test_an_agent_the_cli_has_not_listed_falls_back_to_the_terminal(self):
        """True for the first second or two of a wake."""
        rec = self.agent("box")
        self.rows = [{"sessionId": "somebody-else", "status": "busy"}]
        self.assertTrue(live.working(rec, Fake(working=True)))
        self.assertFalse(live.working(rec, Fake(working=False)))

    def test_a_broken_cli_falls_back_rather_than_failing(self):
        rec = self.agent("box")
        self.rows = RuntimeError("claude: not found")
        self.assertTrue(live.working(rec, Fake(working=True)))
        self.assertFalse(live.working(rec, Fake(working=False)))

    def test_a_missing_cli_is_complained_about_once_not_every_refresh(self):
        rec = self.agent("box")
        self.rows = RuntimeError("claude: not found")
        with self.assertLogs("cc.live", level="WARNING") as caught:
            live.working(rec, Fake())
        self.assertEqual(len(caught.records), 1)
        for _ in range(5):
            live.reset()
            live._warned = True
            live.working(rec, Fake())

    def test_it_is_asked_once_and_the_answer_shared(self):
        """A quarter of a second per card per refresh is why this is cached."""
        rec = self.agent("box")
        self.rows = [{"sessionId": rec["session_id"], "status": "busy"}]
        for _ in range(10):
            live.working(rec, Fake())
        self.assertEqual(self.calls, 1)

    def test_the_answer_goes_stale_and_is_asked_again(self):
        rec = self.agent("box")
        self.rows = [{"sessionId": rec["session_id"], "status": "busy"}]
        live.sessions()
        live.sessions(max_age=0.0)
        self.assertEqual(self.calls, 2)

    def test_a_row_with_no_session_id_is_ignored_rather_than_stored_under_none(self):
        rec = self.agent("box")
        self.rows = [{"status": "busy"}, {"sessionId": rec["session_id"], "status": "idle"}]
        self.assertEqual(sorted(live.sessions()), [rec["session_id"]])

    def test_what_the_cli_said_is_readable_on_its_own(self):
        rec = self.agent("box")
        self.rows = [{"sessionId": rec["session_id"], "status": "busy", "name": "box"}]
        self.assertEqual(live.status_of(rec), "busy")
        self.assertIsNone(live.status_of({"session_id": "nothing"}))

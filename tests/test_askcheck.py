"""The ask check (The owner, 28 October 2026): Jev flags a line of a finished turn that holds an ask
of the owner on no list, and a fresh run of the same agent gets it. A line is flagged once."""

from unittest import mock

from commandcenter import askcheck, config, cron, memory
from tests.harness import Case

TURN = """Evening check done: two meetings read.
- Reporting deadlines view in FundOS. The owner said "I'll note it". Neither has a date.
- Boardwalk told Mohit and Ravi to come back in May."""


class Check(Case):
    def test_a_missed_ask_goes_to_a_fix_run_once(self):
        self.agent("vera")
        jev = {"answers": {"l0": {"noul": 0.02}, "l1": {"noul": 0.85}, "l2": {"noul": 0.02}}}
        with mock.patch.object(config, "TYPESAFE_KEY", "k"), \
                mock.patch.object(memory, "post_jev", return_value=jev):
            first = askcheck.check("vera", TURN)
            again = askcheck.check("vera", TURN)
        self.assertEqual([line for line, _ in first], [askcheck.lines_of(TURN)[1]])
        self.assertEqual(again, [], "a line is flagged once")
        fix = [j for j in cron.all() if j.get("reason") == askcheck.REASON]
        self.assertEqual(len(fix), 1)
        self.assertIn("Reporting deadlines view", fix[0]["task"])
        self.assertEqual(fix[0]["kind"], "fresh")

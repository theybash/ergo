"""What was up, so a restart can put it back. Answer 19.

Two bugs are pinned here, and both were found by building the thing rather than by reading it. A
clean shutdown sleeps every agent, so a restart that read the live record would restore nothing. And
`up` is what was observed while `wanted` is what was asked for, which are different facts.
"""

from commandcenter import status
from tests.harness import Case


class Agents(Case):
    def test_it_records_waking_where_the_next_daemon_will_read_it(self):
        """`were_awake` answers about the last daemon, not this one, and that is the whole point.

        A wake recorded now is invisible here and visible after a restart. Asserting the other way
        round is what the first version of this test did, and it failed for the right reason.
        """
        status.agent("box", True)
        self.assertEqual(status.were_awake(), [], "this daemon started with nothing awake")
        status.reset()
        self.assertEqual(status.were_awake(), ["box"], "the next one sees it")

    def test_the_boot_snapshot_survives_the_shutdown_that_sleeps_everything(self):
        """The bug: shutting down records every agent asleep, which is the answer being read."""
        status.agent("box", True)
        status.agent("other", True)
        status.reset()                      # a new daemon starts and reads the file
        self.assertEqual(status.were_awake(), ["box", "other"])
        status.agent("box", False)          # ...and then puts them all to sleep on the way out
        status.agent("other", False)
        self.assertEqual(status.were_awake(), ["box", "other"],
                         "the restore list must not be erased by the shutdown that follows it")

    def test_a_clean_shutdown_leaves_the_agent_restorable(self):
        """The bug the tests missed and a live restart found.

        The test above only proved that reads inside one daemon's life survive the shutdown. Nothing
        checked the file the next daemon actually reads, and that file said everything was asleep, so
        nothing was ever restored and it looked like it worked.
        """
        status.agent("box", True)
        status.stopping()
        status.agent("box", False)      # the daemon sleeping everything on its way out
        status.reset()                  # the next daemon starts and reads the file
        self.assertEqual(status.were_awake(), ["box"])

    def test_an_agent_put_to_sleep_on_purpose_is_not_restored(self):
        status.agent("box", True)
        status.agent("box", False)
        status.reset()
        self.assertEqual(status.were_awake(), [])

    def test_an_unreadable_file_starts_clean_rather_than_refusing_to_start(self):
        from commandcenter import config
        config.STATUS_FILE.write_text("{ not json at all")
        self.assertEqual(status.were_awake(), [])
        self.assertEqual(status.servers_wanted(), [])


class Servers(Case):
    def test_wanted_survives_a_shutdown_and_up_does_not(self):
        status.server("box", wanted=True, up=True, port=8100)
        status.server("box", up=False)      # the daemon stopping
        status.reset()
        self.assertEqual(status.servers_wanted(), ["box"])

    def test_a_deliberate_stop_clears_the_intent(self):
        status.server("box", wanted=True, up=True)
        status.server("box", wanted=False, up=False)
        status.reset()
        self.assertEqual(status.servers_wanted(), [])

    def test_an_older_file_with_only_up_is_read_as_wanted(self):
        import json
        from commandcenter import config
        config.STATUS_FILE.write_text(json.dumps(
            {"version": 1, "agents": {}, "servers": {"box": {"up": True}}}))
        self.assertEqual(status.servers_wanted(), ["box"])


class Jobs(Case):
    def test_a_job_in_flight_is_remembered_and_then_cleared(self):
        status.job_started("j1", "box")
        status.reset()
        self.assertEqual(list(status.jobs_interrupted()), ["j1"])
        self.assertEqual(status.jobs_interrupted(), {}, "reading it clears it")

    def test_a_job_that_finished_leaves_nothing_behind(self):
        status.job_started("j1", "box")
        status.job_finished("j1")
        status.reset()
        self.assertEqual(status.jobs_interrupted(), {})

    def test_forgetting_an_agent_clears_its_rows(self):
        status.agent("box", True)
        status.server("box", wanted=True)
        status.forget("box")
        status.reset()
        self.assertEqual(status.were_awake(), [])
        self.assertEqual(status.servers_wanted(), [])

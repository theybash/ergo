"""The scheduler. Six grill answers live in this file, and each one gets a test.

    1   a missed job is reported and never run late
    9   each finished run reports for itself
    32  four agents wake at once
    33  a job in flight when the daemon restarts is missed
    37  the agent says the code word; the platform does not guess
    41  a job wakes its agent and puts it back
    43  an agent that was already awake stays awake
"""

import threading
import time
from datetime import datetime

from commandcenter import clock, config, cron, status, telegram
from tests.harness import NINE_AM, Case


class Schedule(Case):
    """When a job is next due, and how many times it was due while nobody was looking."""

    def test_a_daily_job_due_later_today_fires_today(self):
        job = cron._blank(id="aa11", at="23:00")
        due = cron._next_after(job, NINE_AM)
        self.assertEqual(datetime.fromtimestamp(due).strftime("%d %H:%M"), "19 23:00")

    def test_a_daily_job_already_past_today_fires_tomorrow(self):
        job = cron._blank(id="aa11", at="03:00")
        due = cron._next_after(job, NINE_AM)
        self.assertEqual(datetime.fromtimestamp(due).strftime("%d %H:%M"), "20 03:00")

    def test_the_jitter_is_the_same_few_seconds_every_day(self):
        job = cron._blank(id="aa11bb22", at="07:00")
        first = cron._next_after(job, NINE_AM)
        second = cron._next_after(job, NINE_AM + 86400)
        self.assertEqual(first % 86400, second % 86400)
        self.assertLess(first % 60, config.JITTER_SECONDS)

    def test_two_jobs_at_the_same_time_do_not_fire_in_the_same_second(self):
        a = cron._blank(id="aaaa1111", at="07:00")
        b = cron._blank(id="bbbb2222", at="07:00")
        self.assertNotEqual(cron._next_after(a, NINE_AM), cron._next_after(b, NINE_AM))

    def test_an_interval_job_skips_the_intervals_that_passed(self):
        job = cron._blank(id="cc33", every=3600.0, created=NINE_AM - 3 * 86400)
        due = cron._next_after(job, NINE_AM)
        self.assertGreater(due, NINE_AM)
        self.assertLessEqual(due - NINE_AM, 3600.0)

    def test_a_minute_job_down_for_three_days_is_not_walked_step_by_step(self):
        job = cron._blank(id="dd44", every=60.0, created=NINE_AM - 3 * 86400)
        started = time.time()
        cron._next_after(job, NINE_AM)
        self.assertLess(time.time() - started, 0.05)

    def test_counting_what_was_missed(self):
        job = cron._blank(id="aa11", at="03:00")
        since = datetime(2026, 8, 16, 3, 0, 30).timestamp()
        self.assertEqual(cron._due_between(job, since, NINE_AM), 3)

    def test_a_run_just_before_its_own_jittered_boundary_is_not_missed(self):
        """The false alarm that would have gone out at nine about a job that ran at three."""
        job = cron._blank(id="aa11bb22", at="03:00")
        exact = datetime(2026, 8, 19, 3, 0, 0).timestamp()
        self.assertGreater(cron._jitter("aa11bb22"), 0, "this test needs a job with jitter")
        self.assertEqual(cron._due_between(job, exact, NINE_AM), 0)


class Outcome(Case):
    """Answer 37: the platform reads a code word and never infers one."""

    def test_the_code_word_on_its_own_line(self):
        self.assertEqual(cron._outcome_of("all done\n\ntask succeeded"), "succeeded")
        self.assertEqual(cron._outcome_of("Task Failed"), "failed")

    def test_backticks_or_bold_around_the_code_word_still_count(self):
        """Haiku, in the first real fresh run, copied the backticks from the brief."""
        self.assertEqual(cron._outcome_of("24 jobs.\n\n`task succeeded`"), "succeeded")
        self.assertEqual(cron._outcome_of("**task failed**"), "failed")

    def test_a_promise_to_say_it_later_is_not_saying_it(self):
        self.assertEqual(cron._outcome_of("I will end with task succeeded once I am done"),
                         "unclear")

    def test_the_last_word_wins(self):
        self.assertEqual(cron._outcome_of("task failed\n\nsecond go:\n\ntask succeeded"),
                         "succeeded")

    def test_no_reply_is_not_a_failure(self):
        self.assertEqual(cron._outcome_of(None), "no reply")
        self.assertEqual(cron._outcome_of(""), "no reply")


class Adding(Case):
    def test_a_job_needs_an_agent_that_exists(self):
        with self.assertRaises(KeyError):
            cron.add("nobody", "do a thing", at="07:00")

    def test_a_job_needs_one_kind_of_schedule_and_not_both(self):
        self.agent("box")
        with self.assertRaises(ValueError):
            cron.add("box", "a thing", at="07:00", every=600)
        with self.assertRaises(ValueError):
            cron.add("box", "a thing")

    def test_a_time_of_day_is_checked(self):
        self.agent("box")
        for bad in ("7", "25:00", "07:60", "seven"):
            with self.assertRaises(ValueError, msg=bad):
                cron.add("box", "a thing", at=bad)

    def test_an_interval_under_a_minute_is_a_loop(self):
        self.agent("box")
        with self.assertRaises(ValueError):
            cron.add("box", "a thing", every=30)

    def test_a_job_survives_a_reload(self):
        self.agent("box")
        made = cron.add("box", "check the pipeline", at="07:00")
        cron.reset()
        back = cron.get(made["id"])
        self.assertEqual(back["task"], "check the pipeline")
        self.assertEqual(back["next"], made["next"])

    def test_changing_the_schedule_drops_the_other_kind(self):
        self.agent("box")
        made = cron.add("box", "a thing", at="07:00")
        changed = cron.update(made["id"], every=3600)
        self.assertIsNone(changed["at"])
        self.assertEqual(changed["every"], 3600)


class Running(Case):
    def test_a_job_wakes_a_sleeping_agent_and_puts_it_back(self):
        """Answer 41, and the detail nobody wrote down until the grill: it goes back to sleep."""
        self.agent("box")
        fleet = self.fleet()
        made = cron.add("box", "check the pipeline", at="07:00", kind="continuity",
                        reason="reads the conversation")
        cron._run(made["id"])
        self.assertEqual(fleet.woken, ["box"])
        self.assertEqual(fleet.slept, ["box"], "a 3am job would hold 600 MB until morning")
        self.assertEqual(cron.get(made["id"])["last_outcome"], "succeeded")

    def test_an_agent_that_was_already_awake_stays_awake(self):
        """Answer 43: why will you sleep an agent that is supposed to be up?"""
        self.agent("box")
        fleet = self.fleet(awake=True)
        made = cron.add("box", "a thing", at="07:00", kind="continuity", reason="reads the chat")
        cron._run(made["id"])
        self.assertEqual(fleet.slept, [])

    def test_the_agent_is_told_which_words_to_end_with(self):
        self.agent("box")
        fleet = self.fleet()
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"])
        prompt = fleet.asked[0]["prompt"]
        self.assertIn(config.SUCCEEDED, prompt)
        self.assertIn(config.FAILED, prompt)
        self.assertIn("a thing", prompt)

    def test_a_run_that_says_nothing_recognisable_is_unclear_not_failed(self):
        self.agent("box")
        self.fleet(reply="I had a look and it seems fine")
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"])
        job = cron.get(made["id"])
        self.assertEqual(job["last_outcome"], "unclear")
        self.assertTrue(job["last_ran"], "the job did run; only the answer is unknown")

    def test_a_silent_agent_is_recorded_as_no_reply(self):
        self.agent("box")
        self.fleet(reply=None)
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"])
        job = cron.get(made["id"])
        self.assertEqual(job["last_outcome"], "no reply")
        self.assertFalse(job["last_ran"])

    def test_four_agents_wake_at_once_and_the_fifth_waits(self):
        """Answer 32. Each awake agent costs 400 to 700 MB on a laptop with 8 GB free."""
        fleet = self.fleet(delay=0.3)
        jobs = []
        for n in range(5):
            self.agent(f"box{n}")
            jobs.append(cron.add(f"box{n}", "a thing", at="07:00")["id"])
        threads = [threading.Thread(target=cron._run, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(fleet.peak, config.WAKE_AT_ONCE)
        self.assertEqual(len(fleet.asked), 5, "all five still ran, four at a time")

    def test_running_by_hand_does_not_move_the_schedule(self):
        self.agent("box")
        self.fleet()
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"], manual=True)
        self.assertEqual(cron.get(made["id"])["next"], made["next"])

    def test_every_run_is_written_to_the_log_file(self):
        self.agent("box")
        self.fleet()
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"])
        lines = config.JOB_LOG.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn(made["id"], lines[0])

    def test_a_check_that_prints_nothing_wakes_nobody(self):
        """The meetings job ran 192 sessions in four days to learn that a script printed nothing."""
        self.agent("box")
        fleet = self.fleet()
        made = cron.add("box", "post new meetings", every=1800, check="cat ids.txt")
        (self.dir / "box" / "ids.txt").write_text("")
        cron._run(made["id"])
        self.assertEqual(fleet.asked, [], "an empty check costs no run")
        self.assertEqual(cron.get(made["id"])["skips"], 1)
        (self.dir / "box" / "ids.txt").write_text("m-42\n")
        cron._run(made["id"])
        self.assertIn("m-42", fleet.asked[0]["prompt"], "the run is told what the check printed")


class Firing(Case):
    def test_a_job_fires_when_the_clock_reaches_it(self):
        self.agent("box")
        fleet = self.fleet()
        made = cron.add("box", "a thing", every=3600)
        cron._tick()
        self.assertEqual(fleet.asked, [], "nothing is due yet")
        clock.advance(3601)
        cron._tick()
        for _ in range(100):
            if fleet.asked:
                break
            time.sleep(0.02)
        self.assertEqual(len(fleet.asked), 1)

    def test_a_slow_job_is_not_started_twice_by_the_next_tick(self):
        self.agent("box")
        fleet = self.fleet(delay=0.5)
        cron.add("box", "a thing", every=60)
        clock.advance(61)
        cron._tick()
        cron._tick()
        time.sleep(0.8)
        self.assertEqual(len(fleet.asked), 1)

    def test_a_disabled_job_does_not_fire(self):
        self.agent("box")
        fleet = self.fleet()
        made = cron.add("box", "a thing", every=60)
        cron.update(made["id"], enabled=False)
        clock.advance(600)
        cron._tick()
        time.sleep(0.1)
        self.assertEqual(fleet.asked, [])


class Missed(Case):
    """Answer 1, the decision most schedulers get the other way round."""

    def setUp(self):
        super().setUp()
        self.sent = []
        real = telegram.notify
        telegram.notify = lambda text: self.sent.append(text) or True
        self.addCleanup(lambda: setattr(telegram, "notify", real))

    def test_a_job_due_while_the_daemon_was_down_is_reported_and_not_run(self):
        self.agent("box")
        fleet = self.fleet()
        made = cron.add("box", "the 3am job", at="03:00")
        # Two days pass with nothing running.
        cron.update(made["id"], task="the 3am job")
        job = cron.get(made["id"])
        cron._load()[made["id"]]["created"] = NINE_AM - 2 * 86400 - 6 * 3600
        cron._load()[made["id"]]["last_fire"] = None
        notes = cron.catch_up()
        self.assertEqual(fleet.asked, [], "answer 1: it must not run late on its own")
        self.assertEqual(len(notes), 1)
        self.assertIn("missed 2 runs", notes[0])
        self.assertIn(f"/rerun {made['id']}", notes[0], "the offer has to land somewhere")
        self.assertEqual(cron.get(made["id"])["last_outcome"], "missed")
        self.assertEqual(len(self.sent), 1)

    def test_the_same_missed_run_is_not_reported_twice(self):
        """Three restarts in one morning must not read as three separate failures."""
        self.agent("box")
        self.fleet()
        made = cron.add("box", "the 3am job", at="03:00")
        cron._load()[made["id"]]["created"] = NINE_AM - 2 * 86400
        cron.catch_up()
        first = cron.get(made["id"])["missed"]
        cron.catch_up()
        cron.catch_up()
        self.assertEqual(cron.get(made["id"])["missed"], first)
        self.assertEqual(len(self.sent), 1)

    def test_a_job_interrupted_by_a_restart_is_missed_not_done(self):
        """Answer 33. Its last recorded outcome said it succeeded."""
        self.agent("box")
        self.fleet()
        made = cron.add("box", "a long job", at="03:00")
        cron.update(made["id"], enabled=True)
        cron._load()[made["id"]]["last_outcome"] = "succeeded"
        cron._load()[made["id"]]["last_fire"] = NINE_AM - 60
        status.job_started(made["id"], "box")
        notes = cron.catch_up()
        self.assertEqual(cron.get(made["id"])["last_outcome"], "missed")
        self.assertTrue(any("interrupted" in n for n in notes))

    def test_a_job_that_ran_this_morning_is_not_missed(self):
        self.agent("box")
        self.fleet()
        made = cron.add("box", "a thing", at="03:00")
        cron._load()[made["id"]]["last_fire"] = NINE_AM - 6 * 3600 + 30
        self.assertEqual(cron.catch_up(), [])
        self.assertEqual(self.sent, [])


class Reporting(Case):
    """Answer 9: each finished run reports for itself, so three failures are three messages."""

    def setUp(self):
        super().setUp()
        self.sent = []
        real = telegram.notify
        telegram.notify = lambda text: self.sent.append(text) or True
        self.addCleanup(lambda: setattr(telegram, "notify", real))

    def test_three_failures_send_three_messages(self):
        self.agent("box")
        self.fleet(reply="it broke\ntask failed")
        made = cron.add("box", "a thing", every=3600)
        for _ in range(3):
            cron._run(made["id"])
        self.assertEqual(len(self.sent), 3)
        self.assertIn("failed", self.sent[0])

    def test_success_carries_the_answer_because_that_is_the_point_of_the_job(self):
        self.agent("box")
        self.fleet(reply="Two orders are stuck.\n\ntask succeeded")
        made = cron.add("box", "check the pipeline", at="07:00")
        cron._run(made["id"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Two orders are stuck", self.sent[0])

    def test_quiet_silences_success_and_never_failure(self):
        self.agent("box")
        fleet = self.fleet(reply="fine\ntask succeeded")
        made = cron.add("box", "a thing", every=3600, quiet=True)
        cron._run(made["id"])
        self.assertEqual(self.sent, [])
        fleet.reply = "broken\ntask failed"
        cron._run(made["id"])
        self.assertEqual(len(self.sent), 1)

    def test_a_job_shown_in_the_chat_reaches_the_agent_too(self):
        """The owner, 21 October 2026: "agent should know what i know, and vice versa". The card is in
        his chat, and the same result waits in the agent's mailbox."""
        from commandcenter import mailbox
        self.agent("box")
        self.fleet(reply="Three good posts today.\ntask succeeded")
        made = cron.add("box", "DAILY BRIEF for the owner. Read the front page.", at="09:00",
                        kind="fresh", show="chat")
        cron._run(made["id"])
        shown = cron.shown("box")
        self.assertEqual([r["name"] for r in shown], ["DAILY BRIEF for the owner"])
        self.assertIn("Three good posts", shown[0]["text"])
        self.assertEqual(mailbox.waiting("box", everything=True), 1)

    def test_the_fleet_card_line_is_a_newer_shown_result(self):
        """The owner, 21 October 2026: The assistant's 13:30 card was in his chat and Fleet still showed an
        older reply. The card's line is the newest of the two."""
        from commandcenter import server
        self.agent("box")
        self.fleet(reply="WhatsApp checked.\ntask succeeded")
        made = cron.add("box", "Read WhatsApp.", at="09:00", kind="fresh", show="chat")
        cron._run(made["id"])
        old = server.newest_said("box", "an old reply", "2000-01-01T00:00:00Z")
        self.assertEqual(old["last_message"], "WhatsApp checked.")
        self.assertTrue(old["last_message_at"].endswith("Z"))
        new = server.newest_said("box", "a new reply", "2999-01-01T00:00:00Z")
        self.assertEqual(new["last_message"], "a new reply")

    def test_moving_a_chat_job_into_the_conversation_drops_chat(self):
        self.agent("box")
        made = cron.add("box", "Read the news.", at="09:00", kind="fresh", show="chat")
        moved = cron.update(made["id"], kind="continuity", reason="the thread on the news")
        self.assertEqual((moved["kind"], moved["show"]), ("continuity", "context"))

    def test_a_failure_offers_a_rerun(self):
        self.agent("box")
        self.fleet(reply="task failed")
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"])
        self.assertIn(f"/rerun {made['id']}", self.sent[0])


# -- days, dates, one-off jobs and retries (the cron cleanup, 15 October 2026) ---------------

from datetime import datetime as _dt


class Calendar(Case):
    def setUp(self):
        super().setUp()
        self.rec = self.agent("box")

    def at(self, y, mo, d, h=12, mi=0):
        return _dt(y, mo, d, h, mi).timestamp()

    def test_weekdays_skip_the_weekend_without_a_wake(self):
        clock.freeze(self.at(2026, 10, 16, 12))  # a Friday, after 09:00
        job = cron.add(self.rec["id"], "check", at="09:00", days="weekdays")
        nxt = _dt.fromtimestamp(job["next"])
        self.assertEqual(nxt.weekday(), 0)  # Monday, not Saturday
        self.assertEqual(job["days"], [0, 1, 2, 3, 4])

    def test_named_days(self):
        self.assertEqual(cron.parse_days("mon,wed,fri"), [0, 2, 4])
        self.assertEqual(cron.parse_days("daily"), None)
        with self.assertRaises(ValueError):
            cron.parse_days("funday")

    def test_until_ends_the_job(self):
        clock.freeze(self.at(2026, 10, 15, 12))
        job = cron.add(self.rec["id"], "remind", at="09:00", until="2026-10-16")
        self.assertEqual(_dt.fromtimestamp(job["next"]).day, 16)
        clock.freeze(self.at(2026, 10, 16, 12))
        self.assertIsNone(cron._next_after(cron.get(job["id"]), clock.now()))

    def test_start_waits_for_its_date(self):
        clock.freeze(self.at(2026, 10, 15, 12))
        job = cron.add(self.rec["id"], "later", at="09:00", start="2026-10-20")
        self.assertEqual(_dt.fromtimestamp(job["next"]).day, 20)

    def test_a_one_off_runs_once(self):
        clock.freeze(self.at(2026, 10, 15, 12))
        job = cron.add(self.rec["id"], "check back", in_minutes=15)
        self.assertAlmostEqual(job["next"], clock.now() + 900, delta=1)
        self.assertIsNone(cron._next_after(cron.get(job["id"]), job["next"] + 1))
        when = cron.add(self.rec["id"], "at a time", once="2026-10-16 09:00")
        self.assertEqual(_dt.fromtimestamp(when["next"]).hour, 9)
        with self.assertRaises(ValueError):
            cron.add(self.rec["id"], "past", once="2026-10-01 09:00")

    def test_one_schedule_only(self):
        with self.assertRaises(ValueError):
            cron.add(self.rec["id"], "x", at="09:00", in_minutes=5)

    def test_missed_runs_count_only_allowed_days(self):
        job = cron.add(self.rec["id"], "check", at="09:00", days="weekdays")
        live = cron.get(job["id"])
        # Friday noon to Monday noon: only Monday 09:00 was due.
        self.assertEqual(cron._due_between(live, self.at(2026, 10, 16, 12), self.at(2026, 10, 19, 12)), 1)

    def test_an_interval_job_skips_wrong_days(self):
        clock.freeze(self.at(2026, 10, 17, 10))  # a Saturday
        job = cron.add(self.rec["id"], "poll", every=3600, days="weekdays")
        self.assertEqual(_dt.fromtimestamp(job["next"]).weekday(), 0)

    def test_retries_run_again_soon_after_a_failure(self):
        clock.freeze(self.at(2026, 10, 15, 8))
        job = cron.add(self.rec["id"], "flaky", at="09:00", retries=2)
        self.fleet(reply="site down\ntask failed")
        cron._run(job["id"])
        live = cron.get(job["id"])
        self.assertEqual(live["retry_count"], 1)
        self.assertAlmostEqual(live["next"], clock.now() + cron.RETRY_SECONDS, delta=1)
        cron._run(job["id"])
        cron._run(job["id"])
        live = cron.get(job["id"])
        self.assertEqual(live["retry_count"], 2)
        self.assertGreater(live["next"], clock.now() + cron.RETRY_SECONDS + 60)  # back to 09:00


    def test_a_job_that_will_not_run_again_leaves_the_list(self):
        clock.freeze(self.at(2026, 10, 15, 8))
        done = cron.add(self.rec["id"], "check back", in_minutes=10)
        flaky = cron.add(self.rec["id"], "check a site", in_minutes=10, retries=1)
        missed = cron.add(self.rec["id"], "missed while shut", in_minutes=20)
        clock.freeze(self.at(2026, 10, 15, 8, 15))
        self.fleet(reply="all fine\ntask succeeded")
        cron._run(done["id"])
        self.assertIsNone(cron.get(done["id"]), "it ran and will never run again")
        self.fleet(reply="site down\ntask failed")
        cron._run(flaky["id"])
        self.assertIsNotNone(cron.get(flaky["id"]), "its retry is still due")
        clock.freeze(self.at(2026, 10, 15, 9))
        cron.catch_up()                                     # the laptop was shut at 08:20
        cron._sweep(clock.now())
        self.assertIsNotNone(cron.get(missed["id"]), "too soon: a run may still be going")
        cron._sweep(clock.now() + cron.SPENT_AFTER)
        self.assertIsNone(cron.get(missed["id"]))

class Watching(Case):
    def test_a_change_under_a_watched_folder_runs_the_job_once_after_the_debounce(self):
        from commandcenter import watch
        config.WATCH_ROOTS = [str(self.dir)]
        self.agent("box")
        notes = self.dir / "box" / "notes"
        (notes / "deep").mkdir(parents=True)
        fleet = self.fleet()
        cron.add("box", "read what changed", watch=str(notes), debounce=1)
        watch.start()
        # A burst of writes, an ignored folder and an editor temp file.
        (notes / "a.md").write_text("one")
        (notes / "deep" / "b.md").write_text("two")
        (notes / "a.md").write_text("three")
        (notes / ".a.md.swp").write_text("vim")
        (notes / "node_modules").mkdir()
        (notes / "node_modules" / "x.js").write_text("x")
        deadline = time.time() + 6
        while not fleet.fresh and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(1.5)
        self.assertEqual(fleet.fresh, ["box"])
        prompt = fleet.asked[0]["prompt"]
        self.assertIn(str(notes / "a.md"), prompt)
        self.assertIn(str(notes / "deep" / "b.md"), prompt)
        self.assertNotIn("swp", prompt)
        self.assertNotIn("node_modules", prompt)


class Naming(Case):
    def test_a_name_skips_the_cold_start_lines(self):
        task = ("COLD START. This runs as a fresh session with no chat history. Before anything "
                "else, read the CLAUDE.md files. You are the initech agent. Use only the work "
                "identity. Evening meeting check, Initech half. Runs at 22:00.")
        self.assertEqual(cron._name(task), "Evening meeting check, Initech half")

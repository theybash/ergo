"""Nags (The owner, 19 October 2026): a late task keeps coming back until it is done. A nag run is
born for it with the owner's brief and the log so far, and a next nag is always left behind."""

from commandcenter import broker, clock, cron, fresh, nags, tasks
from tests.harness import Case
from tests.test_inbox import local


class Nags(Case):
    def test_a_late_task_gets_a_run_with_its_story_and_always_a_next_nag(self):
        self.agent("assistant")
        self.addCleanup(nags.reset)
        clock.freeze(local(2026, 10, 19, 16, 30))
        self.addCleanup(clock.unfreeze)
        real_worker = nags._start_worker
        nags._start_worker = lambda: None
        self.addCleanup(setattr, nags, "_start_worker", real_worker)
        seen = []
        real_run, real_leave, real_show = fresh.run, broker.leave, cron._show
        fresh.run = lambda job, prompt, timeout: (seen.append((job, prompt)) or
                                                  {"reply": "Called him\ntask succeeded"})
        broker.leave = lambda agent, text: None
        cron._show = lambda *a: None
        self.addCleanup(setattr, fresh, "run", real_run)
        self.addCleanup(setattr, broker, "leave", real_leave)
        self.addCleanup(setattr, cron, "_show", real_show)

        task = tasks.add_mine("Get the printouts", by="assistant", due=clock.now() - 1800,
                              brief="Needed before 22:00. Call him first.")
        tasks.set_nag(task["id"], "assistant", note="15:30 brief left")
        nags.check()
        nags._queue.get_nowait()
        nags.run(task["id"])

        job, prompt = seen[0]
        self.assertEqual((job["agent"], job["model"]), ("assistant", "haiku"))
        self.assertIn("Needed before 22:00. Call him first.", prompt)
        self.assertIn("15:30 brief left", prompt, "the run reads what the last one did")
        after = tasks.get(task["id"])
        self.assertEqual(after["nag_log"][-1]["text"], "Called him",
                         "a run that logged nothing still leaves its line")
        self.assertGreater(after["nag_at"], clock.now(), "and a next nag, set before it ran")

        tasks.tick(task["id"])
        self.assertIsNone(nags.due_at(tasks.get(task["id"])), "a ticked task nags no more")

    def test_a_task_moved_while_its_run_waits_is_not_nagged(self):
        self.agent("assistant")
        self.addCleanup(nags.reset)
        clock.freeze(local(2026, 10, 19, 19, 46))
        self.addCleanup(clock.unfreeze)
        real_run = fresh.run
        fresh.run = lambda *a: self.fail("a run for a task that is not due")
        self.addCleanup(setattr, fresh, "run", real_run)
        task = tasks.add_mine("Fix the notice date", by="assistant", due=clock.now() - 60)
        tasks.move(task["id"], local(2026, 10, 20, 18, 0), actor="assistant")
        self.assertIsNone(nags.run(task["id"]))
        self.assertIsNone(tasks.get(task["id"])["nag_at"], "the move's time stands")

    def test_an_urgent_brief_nags_every_five_minutes_with_no_limit_of_its_own(self):
        self.agent("assistant")
        self.addCleanup(nags.reset)
        clock.freeze(local(2026, 10, 20, 21, 55))
        self.addCleanup(clock.unfreeze)
        real_worker = nags._start_worker
        nags._start_worker = lambda: None
        self.addCleanup(setattr, nags, "_start_worker", real_worker)
        brief = "Urgent: a call every 5 minutes, no limit, until he ticks it or says stop."
        task = tasks.add_mine("Feed Asha's cats", by="assistant", due=clock.now() - 300,
                              brief=brief)
        seen = []

        def nag_run(job, prompt, timeout):
            seen.append(prompt)
            tasks.set_nag(task["id"], "assistant", note="call 3", next_in_minutes=5)
            return {"reply": "Called him\ntask succeeded"}
        real_run, real_leave, real_show = fresh.run, broker.leave, cron._show
        fresh.run, broker.leave, cron._show = nag_run, (lambda a, t: None), (lambda *a: None)
        self.addCleanup(setattr, fresh, "run", real_run)
        self.addCleanup(setattr, broker, "leave", real_leave)
        self.addCleanup(setattr, cron, "_show", real_show)

        nags.check()
        nags.run(nags._queue.get_nowait())
        self.assertIn(brief, seen[0])
        self.assertNotIn("two calls", seen[0], "the house cap is for a task with no brief")
        self.assertEqual(tasks.get(task["id"])["nag_at"], clock.now() + 300,
                         "the brief's five minutes stand, not the hour set before the run")

    def test_a_time_he_asked_for_rings_in_the_night_and_a_moved_nag_is_told(self):
        # 20 Oct 2026: he asked the assistant for a call at 23:45, and the night window moved it to
        # 08:00 without a word.
        self.agent("assistant")
        self.addCleanup(nags.reset)
        clock.freeze(local(2026, 10, 20, 23, 45))
        self.addCleanup(clock.unfreeze)
        real_worker, real_leave = nags._start_worker, broker.leave
        nags._start_worker = lambda: None
        told = []
        broker.leave = lambda agent, text: told.append((agent, text))
        self.addCleanup(setattr, nags, "_start_worker", real_worker)
        self.addCleanup(setattr, broker, "leave", real_leave)

        asked = tasks.add_mine("Clean up your bag", by="assistant", due=clock.now() - 10,
                               night_ok=True)
        chosen = tasks.add_mine("Pay the gym", by="assistant", due=clock.now() - 10)
        self.assertIsNone(asked["nag_waits_until"])
        self.assertEqual(chosen["nag_waits_until"], local(2026, 10, 21, 8, 0))

        nags.check()
        self.assertEqual(nags._queue.get_nowait(), asked["id"], "his own time rings at night")
        self.assertTrue(nags._queue.empty())
        self.assertEqual(tasks.get(chosen["id"])["nag_at"], local(2026, 10, 21, 8, 0))
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0][0], "assistant")
        self.assertIn("because of the night window", told[0][1])


class AlarmCovers(Case):
    def test_an_alarm_at_the_due_time_is_the_first_nag_but_a_nag_runs_own_alarm_is_not(self):
        """28 Oct 2026: an alarm at 11:10, then the nag run's own at 11:12."""
        from commandcenter import phone
        self.agent("assistant")
        self.addCleanup(nags.reset)
        clock.freeze(local(2026, 10, 28, 11, 0))
        self.addCleanup(clock.unfreeze)
        real_worker = nags._start_worker
        nags._start_worker = lambda: None
        self.addCleanup(setattr, nags, "_start_worker", real_worker)
        task = tasks.add_mine("Set up the Mac", by="assistant", due=local(2026, 10, 28, 11, 10))
        phone.alarm(11, 10, task_id=task["id"], by="assistant")
        clock.freeze(local(2026, 10, 28, 11, 10) + 1)
        nags.check()
        self.assertTrue(nags._queue.empty(), "the alarm is the reminder")
        self.assertIn("11:10 alarm", tasks.get(task["id"])["nag_log"][-1]["text"])
        clock.freeze(local(2026, 10, 28, 11, 11))
        phone.alarm(11, 13, task_id=task["id"], by="assistant")    # as a nag run would
        clock.freeze(local(2026, 10, 28, 11, 11) + 30)
        tasks.set_nag(task["id"], "assistant", note="alarm", next_at=clock.now() + 60)
        clock.freeze(local(2026, 10, 28, 11, 13))
        nags.check()
        self.assertFalse(nags._queue.empty(), "a nag run's own alarm does not stop the next nag")

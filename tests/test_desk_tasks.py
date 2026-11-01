"""The front desk owns his task list: asks done, move or drop, and asks once about tasks that look like
one (The owner, 18 October 2026). The list stays out of the opening; he reads it when asked."""

from commandcenter import clock, tasks, voice
from tests import test_voice


class DeskTasks(test_voice.Routes):
    def test_a_call_opens_without_the_task_list(self):
        tasks.add_mine("Get mask", due=clock.now() - 3600)
        first = voice.live_prompt()
        self.assertNotIn("Get mask", first)
        self.assertIn(voice.TASKS_LINE, first)

    def test_two_titles_that_say_the_same_are_asked_about_once(self):
        tasks.add_mine("Replace the jet shower in the master bedroom")
        b = tasks.add_mine("Install the jet shower in the master bedroom")
        self.assertEqual(len(voice.call("his_tasks")["maybe_same"]), 1)
        voice.call("task_keep", {"task": b["id"]})
        self.assertEqual(tasks.for_desk()["maybe_same"], [])

    def test_unlike_titles_are_not_a_pair(self):
        tasks.add_mine("Get mask")
        tasks.add_mine("Get printouts")
        self.assertEqual(tasks.for_desk()["maybe_same"], [])

    def test_the_demo_view_shows_none(self):
        tasks.add_mine("Get mask", due=clock.now() - 3600)
        with voice.scoped("demo"):
            self.assertIn("not shown", voice.call("his_tasks"))
            self.assertIn("not shown", voice.call("task_tick", {"task": "mask"}))

    def test_move_and_drop_from_his_answer(self):
        a = tasks.add_mine("Get mask", due=clock.now() - 3600)
        b = tasks.add_mine("Print the land record")
        said = voice.call("task_move", {"task": "mask", "due": "2026-11-02 18:30"})
        self.assertTrue(said["ok"])
        self.assertIn("18:30", said["said"])
        self.assertEqual(tasks.get(a["id"])["due"], __import__("datetime").datetime(2026, 11, 2, 18, 30).timestamp())
        self.assertTrue(voice.call("task_drop", {"task": "land record"})["ok"])
        self.assertEqual(tasks.get(b["id"])["state"], "dismissed")

    def test_a_bad_time_is_asked_again(self):
        tasks.add_mine("Get mask")
        self.assertIn("YYYY-MM-DD", voice.call("task_move", {"task": "mask", "due": "soon"}))

    def test_his_tasks_lists_due_first(self):
        tasks.add_mine("Later one", due=clock.now() + 86400 * 3)
        tasks.add_mine("Now one", due=clock.now() - 60)
        got = voice.call("his_tasks")
        self.assertEqual([t["title"] for t in got["due_or_overdue"]], ["Now one"])
        self.assertEqual([t["title"] for t in got["others"]], ["Later one"])

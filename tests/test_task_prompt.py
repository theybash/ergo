"""Prompt on an offer card: his words leave the card "talking", and Jev reads the exchange.

TypeSafe is never called. `memory.post_jev` is replaced, and so is the agent's last reply."""

from commandcenter import config, memory, tasks, transcript
from tests.test_tasks import TaskCase


class PromptCase(TaskCase):
    def setUp(self):
        super().setUp()
        self.jev = []
        self.answer = {"answers": {"fate": {"choice": "started", "probabilities": {"started": 0.95}}}}
        real_post, real_last, real_key = memory.post_jev, transcript.last_message, config.TYPESAFE_KEY
        memory.post_jev = lambda payload, key, timeout=None: self.jev.append(payload) or self.answer
        transcript.last_message = lambda rec: "Checking the PR first, then I will write it up."
        config.TYPESAFE_KEY = "test-key"
        self.addCleanup(lambda: setattr(memory, "post_jev", real_post))
        self.addCleanup(lambda: setattr(transcript, "last_message", real_last))
        self.addCleanup(lambda: setattr(config, "TYPESAFE_KEY", real_key))

    def prompt(self, task, words="Check the PR first for the exact permission"):
        return tasks.prompted(self.juno["id"], f"[About task {task['id']}: {task['title']}]\n\n{words}")

    def judge(self):
        held = tasks._prompts.pop(self.juno["id"])
        tasks._judge_prompt(self.juno["id"], held)

    def test_his_words_take_the_card_out_of_waiting(self):
        task = self.offer()
        self.assertEqual(self.prompt(task), task["id"])
        self.assertEqual(tasks.get(task["id"])["state"], "talking")

    def test_a_sure_start_is_a_yes_and_tells_the_agent_nothing_more(self):
        task = self.offer()
        self.prompt(task)
        self.judge()
        self.assertEqual(tasks.get(task["id"])["state"], "yes")
        self.assertEqual(self.sent, [])
        state = self.jev[0]["state"]
        self.assertIn("Check the PR first", state["owner_wrote"])
        self.assertIn("Checking the PR", state["agent_replied"])

    def test_the_card_keeps_his_words_and_the_answer(self):
        task = self.offer()
        self.answer = {"answers": {"fate": {"choice": "discussing", "probabilities": {"discussing": 0.9}}}}
        self.prompt(task, "Is it live")
        self.judge()
        thread = tasks.get(task["id"])["thread"]
        self.assertEqual([(t["who"], t["text"]) for t in thread],
                         [("owner", "Is it live"), ("agent", "Checking the PR first, then I will write it up.")])

    def test_a_sure_decline_is_a_no_with_his_words(self):
        task = self.offer()
        self.answer = {"answers": {"fate": {"choice": "declined", "probabilities": {"declined": 0.9}}}}
        self.prompt(task, "Not ours, drop it")
        self.judge()
        got = tasks.get(task["id"])
        self.assertEqual(got["state"], "no")
        self.assertIn("drop it", got["why"])

    def test_an_unsure_answer_leaves_it_talking(self):
        task = self.offer()
        self.answer = {"answers": {"fate": {"choice": "started", "probabilities": {"started": 0.6}}}}
        self.prompt(task)
        self.judge()
        self.assertEqual(tasks.get(task["id"])["state"], "talking")

    def test_still_discussing_stays_talking_and_the_buttons_still_answer(self):
        task = self.offer()
        self.answer = {"answers": {"fate": {"choice": "discussing", "probabilities": {"discussing": 0.97}}}}
        self.prompt(task)
        self.judge()
        self.assertEqual(tasks.get(task["id"])["state"], "talking")
        tasks.no(task["id"])
        self.assertEqual(tasks.get(task["id"])["state"], "no")

    def test_a_message_without_a_quote_or_for_another_agent_changes_nothing(self):
        task = self.offer()
        self.assertIsNone(tasks.prompted(self.juno["id"], "just a note"))
        self.assertIsNone(tasks.prompted("someone-else", f"[About task {task['id']}: x]"))
        self.assertEqual(tasks.get(task["id"])["state"], "offered")

    def test_a_turn_end_with_nothing_prompted_asks_nobody(self):
        tasks.prompt_settled(self.juno["id"])
        self.assertEqual(self.jev, [])

    def test_a_failing_jev_leaves_it_talking(self):
        task = self.offer()
        def boom(payload, key, timeout=None):
            raise OSError("down")
        memory.post_jev = boom
        self.prompt(task)
        self.judge()
        self.assertEqual(tasks.get(task["id"])["state"], "talking")

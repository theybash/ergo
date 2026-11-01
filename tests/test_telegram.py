"""The bridge. Answers 2, 8 and 44, plus the three v1 ideas that were ported.

Everything here runs against a local stub standing in for api.telegram.org. That is the only way to
test a bridge without a token, and it is also the only way to test what happens when the network
dies, which is the failure this laptop actually has most evenings.
"""

import json
import time

from commandcenter import config, cron, registry, telegram
from tests.harness import Case


class Stub(Case):
    """A fake Telegram. Records what was sent and hands out what was queued."""

    def setUp(self):
        super().setUp()
        self.sent = []
        self.actions = []
        self.queue = []
        self.reject_markdown = set()
        outer = self

        def send_message(handler, body):
            fields = {k: v[0] for k, v in
                      __import__("urllib.parse", fromlist=["x"]).parse_qs(body.decode()).items()}
            text, mode = fields.get("text", ""), fields.get("parse_mode")
            if mode == "Markdown" and any(m in text for m in outer.reject_markdown):
                return (400, "application/json",
                        {"ok": False, "description": "can't parse entities"})
            outer.sent.append({"text": text, "mode": mode, "chat": fields.get("chat_id")})
            return 200, "application/json", {"ok": True, "result": {"message_id": len(outer.sent)}}

        def chat_action(handler, body):
            outer.actions.append(1)
            return 200, "application/json", {"ok": True, "result": True}

        def get_updates(handler, body):
            out, outer.queue[:] = list(outer.queue), []
            if not out:
                time.sleep(0.2)
            return 200, "application/json", {"ok": True, "result": out}

        # No space in the template. The first version had "/bot {token}/{method}", so every path
        # arrived percent-encoded as "/bot%20tok/..." and matched no route: fourteen tests failing
        # for one character.
        self.srv = self.http({
            "/bottok/sendMessage": send_message,
            "/bottok/sendChatAction": chat_action,
            "/bottok/getUpdates": get_updates,
        })
        real_base = telegram.BASE
        telegram.BASE = self.srv.base + "/bot{token}/{method}"
        self.addCleanup(lambda: setattr(telegram, "BASE", real_base))
        config.TELEGRAM_TOKEN, config.TELEGRAM_OPERATOR = "tok", "4242"

    def from_operator(self, text, update_id=1):
        self.queue.append({"update_id": update_id,
                           "message": {"from": {"id": 4242}, "chat": {"id": 4242}, "text": text}})

    def run_bridge(self, until=1, seconds=6):
        telegram.start()
        for _ in range(int(seconds / 0.1)):
            if len(self.sent) >= until:
                break
            time.sleep(0.1)
        telegram.stop()


class Sending(Stub):
    def test_nothing_is_sent_without_a_token(self):
        config.TELEGRAM_TOKEN = ""
        self.assertFalse(telegram.configured())
        self.assertFalse(telegram.send("hello"))

    def test_a_message_arrives(self):
        self.assertTrue(telegram.send("hello"))
        self.assertEqual(self.sent[0]["text"], "hello")
        self.assertEqual(self.sent[0]["chat"], "4242")

    def test_a_reply_telegram_will_not_parse_is_sent_as_plain_text(self):
        """The message surviving matters more than how it looks."""
        self.reject_markdown.add("stray")
        self.assertTrue(telegram.send("a reply with a *stray asterisk"))
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.sent[0]["mode"])

    def test_a_long_reply_is_cut_into_pieces(self):
        telegram.send("word " * 3000)
        self.assertGreater(len(self.sent), 1)
        for piece in self.sent:
            self.assertLessEqual(len(piece["text"]), config.TELEGRAM_CHUNK)

    def test_it_cuts_on_a_blank_line_when_there_is_one(self):
        body = ("a" * 3000) + "\n\n" + ("b" * 3000)
        telegram.send(body)
        self.assertTrue(self.sent[0]["text"].endswith("a"))

    def test_the_same_message_twice_is_sent_once(self):
        """Two identical messages on a phone read as two separate events."""
        telegram.send("the same news")
        telegram.send("the same news")
        self.assertEqual(len(self.sent), 1)

    def test_the_duplicate_check_can_be_turned_off(self):
        telegram.send("the same news", quiet_duplicates=False)
        telegram.send("the same news", quiet_duplicates=False)
        self.assertEqual(len(self.sent), 2)

    def test_the_daemon_tells_him_nothing_on_its_own(self):
        """17 Oct 2026: "Telegram, my eyes don't go there." The assistant's replies still go."""
        config.TELEGRAM_ALERTS = False
        self.assertFalse(telegram.notify("job 1234 failed"))
        self.assertEqual(self.sent, [])
        self.assertTrue(telegram.send("The assistant's answer"))
        self.assertEqual(len(self.sent), 1)

    def test_alerts_come_back_when_switched_on(self):
        config.TELEGRAM_ALERTS = True
        self.addCleanup(setattr, config, "TELEGRAM_ALERTS", False)
        self.assertTrue(telegram.notify("job 1234 failed"))
        self.assertEqual(len(self.sent), 1)

    def test_an_empty_message_is_not_sent(self):
        self.assertFalse(telegram.send("   "))
        self.assertEqual(self.sent, [])


class Receiving(Stub):
    def test_only_the_operator_is_answered(self):
        """Answer 44. A bot address is guessable and this bridge can wake agents."""
        self.queue.append({"update_id": 1, "message": {"from": {"id": 9999},
                                                       "chat": {"id": 9999}, "text": "/agents"}})
        self.from_operator("/agents", update_id=2)
        self.run_bridge(until=1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["chat"], "4242")

    def test_it_answers_which_agents_exist(self):
        self.agent("box")
        self.from_operator("/agents")
        self.run_bridge()
        self.assertIn("box", self.sent[0]["text"])
        self.assertIn("asleep", self.sent[0]["text"])

    def test_it_answers_the_schedule(self):
        self.agent("box")
        cron.add("box", "check the pipeline", at="07:00")
        self.from_operator("/jobs")
        self.run_bridge()
        self.assertIn("check the pipeline", self.sent[0]["text"])

    def test_an_unknown_command_says_so_and_offers_the_list(self):
        self.from_operator("/nonsense")
        self.run_bridge()
        self.assertIn("no such command", self.sent[0]["text"])
        self.assertIn("/rerun", self.sent[0]["text"])

    def test_a_plain_message_with_no_master_says_that_plainly(self):
        self.from_operator("what is the pipeline doing")
        self.run_bridge()
        self.assertIn("No master is registered", self.sent[0]["text"])

    def test_a_plain_message_goes_to_the_master(self):
        """Answer 2: the master, always. Answer 8: it is woken, unlike a note from the dashboard."""
        self.agent("chief", role="master")
        fleet = self.fleet(reply="two orders are stuck")
        self.from_operator("what is the pipeline doing")
        self.run_bridge()
        self.assertEqual(fleet.asked[0]["agent"], "chief")
        self.assertIn("over Telegram", fleet.asked[0]["prompt"])
        self.assertIn("two orders are stuck", self.sent[0]["text"])
        self.assertEqual(fleet.woken, ["chief"])

    def test_rerun_from_the_phone_starts_the_job(self):
        self.agent("box")
        fleet = self.fleet()
        job = cron.add("box", "a thing", at="07:00")
        self.from_operator(f"/rerun {job['id']}")
        self.run_bridge()
        for _ in range(50):
            if fleet.asked:
                break
            time.sleep(0.05)
        self.assertEqual(len(fleet.asked), 1)

    def test_where_it_got_to_is_kept_so_a_restart_does_not_replay(self):
        self.from_operator("/agents", update_id=77)
        self.run_bridge()
        self.assertEqual(json.loads(config.TELEGRAM_FILE.read_text())["offset"], 78)

    def quick_retry(self):
        """Retry after 50 ms, not a second: the same failures, in a tenth of the time."""
        real = telegram.RETRY_FIRST
        telegram.RETRY_FIRST = 0.05
        self.addCleanup(setattr, telegram, "RETRY_FIRST", real)

    def test_a_dead_network_does_not_kill_the_bridge(self):
        self.quick_retry()
        telegram.BASE = "http://127.0.0.1:1/bot{token}/{method}"
        thread = telegram.start()
        time.sleep(0.5)
        self.assertTrue(thread.is_alive(), "the router falls over most evenings; it has to survive")
        telegram.stop()

    def test_it_recovers_when_the_network_comes_back(self):
        dead = "http://127.0.0.1:1/bot{token}/{method}"
        alive = telegram.BASE
        self.quick_retry()
        telegram.BASE = dead
        telegram.start()
        time.sleep(0.5)
        self.agent("box")
        self.from_operator("/agents")
        telegram.BASE = alive
        for _ in range(80):
            if self.sent:
                break
            time.sleep(0.1)
        telegram.stop()
        self.assertEqual(len(self.sent), 1)

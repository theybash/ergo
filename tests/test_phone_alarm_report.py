"""An alarm that will not ring is said out loud: by a call to the owner, and to the agent that set it."""

import time

from commandcenter import clock, phone
from tests.harness import Case


class AlarmReport(Case):
    def setUp(self):
        super().setUp()
        self.said, self.told = [], []
        from commandcenter import broker, inbox
        real_add, real_tell = inbox.add, broker.send
        self.calls = []
        def add(agent, text, urgency="today", **kw):
            self.calls.append((agent, urgency))
            self.said.append(text)
            return {"id": "x"}
        inbox.add = add
        broker.send = lambda agent, text, wake=True: self.told.append((agent, text, wake)) or "held"
        self.addCleanup(setattr, inbox, "add", real_add)
        self.addCleanup(setattr, broker, "send", real_tell)
        self.agent("globex")

    def wait_for(self, what):
        for _ in range(50):
            if what():
                return
            time.sleep(0.02)

    def test_a_refused_alarm_is_reported_once_to_owner_and_the_agent(self):
        got = phone.alarm(15, 59, "Book the meeting room", by="globex")
        phone.done(got["id"], {"ok": False, "error": "The time 15:59 has already passed"})
        self.wait_for(lambda: self.said and self.told)
        self.assertEqual(len(self.said), 1)
        self.assertIn("15:59 alarm (Book the meeting room) will not ring", self.said[0])
        self.assertEqual(self.calls[0], ("globex", "urgent"), "a call, in the name of the agent that set it")
        self.assertEqual(self.told[0][0], "globex")
        self.assertFalse(self.told[0][2], "the agent is not woken for it")
        phone._report(dict(phone._load()[-1]), "again")
        self.assertEqual(len(self.said), 1, "said once")

    def test_an_alarm_still_waiting_near_its_time_is_reported(self):
        got = phone.alarm(7, 30, "Gym", by="globex")
        clock.advance(phone._due(7, 30, clock.now()) - clock.now() - 10)
        phone._check(got["id"])
        self.wait_for(lambda: self.said)
        self.assertIn("has not reached your phone", self.said[0])

    def test_a_taken_alarm_is_not_reported(self):
        got = phone.alarm(7, 30, "Gym", by="globex")
        phone.done(got["id"], {"ok": True})
        phone._check(got["id"])
        time.sleep(0.1)
        self.assertEqual(self.said, [])

    def test_an_alarm_queued_while_ergo_is_not_connected_is_reported_early(self):
        phone.on_listening(lambda: False)
        phone.DEAF_CHECK, real = 0.05, phone.DEAF_CHECK
        self.addCleanup(setattr, phone, "DEAF_CHECK", real)
        got = phone.alarm(23, 59, "Gym", by="globex")
        self.assertIn("not connected", got["said"])
        self.wait_for(lambda: self.said)
        self.assertIn("Ergo on your phone is not connected", self.said[0])

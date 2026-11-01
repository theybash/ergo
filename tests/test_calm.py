"""Calm mode: one switch, read by every agent, that holds offers, nudges and reminder calls."""

from commandcenter import calm, clock, inbox, phone, tasks, voice
from tests import test_voice


class Calm(test_voice.Routes):
    def turn(self, on):
        calm.save({"on": on})

    def test_it_is_off_until_he_turns_it_on_and_stays_on(self):
        self.assertFalse(calm.on())
        self.turn(True)
        self.assertTrue(calm.on())
        self.assertTrue(calm.state()["since"])
        code, got = self.hit("GET", "/api/calm")
        self.assertTrue(got["on"])
        self.assertIn("Hold offers", got["rules"])
        self.assertTrue(self.hit("GET", "/api/state")[1]["calm"])

    def test_only_the_console_turns_it(self):
        code, _ = self.hit("POST", "/api/calm", {"on": True})
        self.assertEqual(code, 403)
        self.assertFalse(calm.on())

    def test_a_brief_that_is_not_urgent_is_held(self):
        rec = self.agent("box")
        self.turn(True)
        code, got = self.hit("POST", "/api/inbox", {"agent": rec["id"], "text": "Lunch?",
                                                    "urgency": "today"})
        self.assertEqual(code, 409)
        self.assertIn("Calm mode is on", got["error"])
        code, _ = self.hit("POST", "/api/inbox", {"agent": rec["id"], "text": "Soon.",
                                                  "urgency": "now"})
        self.assertEqual(code, 409)
        code, _ = self.hit("POST", "/api/inbox", {"agent": rec["id"], "text": "Flight moved.",
                                                  "urgency": "urgent"})
        self.assertEqual(code, 200)

    def test_an_offer_is_held(self):
        rec = self.agent("box")
        self.turn(True)
        code, _ = self.hit("POST", "/api/tasks", {"agent": rec["id"], "title": "Draft it",
                                                  "source": "mail", "deliverable": "a draft"})
        self.assertEqual(code, 409)

    def test_the_alarm_check_does_not_call(self):
        called = []
        real = inbox.add
        inbox.add = lambda *a, **k: called.append(a) or {"id": "x"}
        self.addCleanup(setattr, inbox, "add", real)
        got = phone.alarm(7, 0, "gym")
        phone.stopped(got["id"])
        self.turn(True)
        clock.freeze(clock.now() + phone.CHECK_AFTER)
        phone.stop_check(got["id"])
        self.assertEqual(called, [])

    def test_desk_is_told_and_says_nothing_of_it_when_off(self):
        self.assertNotIn("Calm mode", voice.live_prompt())
        self.turn(True)
        self.assertIn("Never say overdue", voice.live_prompt())
        self.assertIn("Never say overdue", voice.briefing())

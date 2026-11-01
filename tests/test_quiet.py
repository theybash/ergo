"""Quiet time: for a nap, nothing rings until it ends."""

from commandcenter import clock, inbox, phone, quiet
from tests import test_voice


class Quiet(test_voice.Routes):
    def test_a_nap_holds_calls_and_moves_alarms_to_the_end(self):
        self.addCleanup(quiet.reset)
        rec = self.agent("box")
        code, got = self.hit("POST", "/api/quiet", {"minutes": 60, "by": rec["id"]})
        self.assertEqual(code, 200)
        self.assertTrue(got["on"])
        self.assertEqual(self.hit("GET", "/api/state")[1]["quiet"], got["until"])
        code, _ = self.hit("POST", f"/api/agents/{rec['id']}/call", {"reason": "Wake up."})
        self.assertNotEqual(code, 200)
        inbox.add(rec["id"], "The flight moved.", urgency="urgent", judge=False)
        self.assertIsNone(inbox.ring_check())
        self.assertIsNone(inbox.ringing())
        soon = clock.now() + 600
        import datetime
        at = datetime.datetime.fromtimestamp(soon)
        alarm = phone.alarm(at.hour, at.minute, "gym")
        self.assertIn("quiet until", alarm["said"])
        self.assertTrue(any(c.get("kind") == "quiet" for c in phone.commands()["commands"]))
        quiet.set_for(0)
        self.assertFalse(quiet.on())
        self.assertEqual(inbox.ringing()["text"], "The flight moved.")

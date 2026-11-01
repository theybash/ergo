"""The daily quota alert: past the day's line, one full-screen notice on the phone a day."""

import datetime

from commandcenter import calm, clock, phone, quota
from tests import harness


def event(used, resets_at=None):
    week = {"utilization": used}
    if resets_at:
        week["resetsAt"] = resets_at
    return {"type": "rate_limit_event",
            "rate_limit_info": {"unifiedWindows": {"seven_day": week}}}


class Quota(harness.Case):
    def setUp(self):
        super().setUp()
        # Saturday 17 October 2026, 20:16 local: 5 days 15 h 46 min since Monday 04:30.
        clock.freeze(datetime.datetime(2026, 10, 17, 20, 16).timestamp())

    def notices(self):
        return [c for c in phone.commands()["commands"] if c["kind"] == "notice"]

    def test_the_line_is_fourteen_a_day_since_monday_half_past_four(self):
        self.assertAlmostEqual(quota.line(), 14 * (5 + 15.766 / 24), delta=0.1)

    def test_the_reset_from_the_stream_wins(self):
        resets = datetime.datetime(2026, 10, 19, 4, 30).timestamp()
        self.assertAlmostEqual(quota.line(resets_at=resets), quota.line(), delta=0.01)

    def test_under_the_line_says_nothing(self):
        quota.seen(event(0.56))
        self.assertEqual(self.notices(), [])
        self.assertEqual(quota.state()["used"], 56)

    def test_over_the_line_shows_one_notice_a_day(self):
        quota.seen(event(0.81))
        quota.seen(event(0.82))
        got = self.notices()
        self.assertEqual(len(got), 1)
        self.assertIn("81%", got[0]["text"])
        self.assertIn("You decide", got[0]["text"])
        clock.freeze(clock.now() + 86400)
        quota.seen(event(0.97))
        # Yesterday's notice is stale and dropped; today's is new.
        self.assertEqual([n["text"][:26] for n in self.notices()], ["The week's usage is 97%. T"])

    def test_calm_mode_holds_it(self):
        calm.save({"on": True})
        quota.seen(event(0.9))
        self.assertEqual(self.notices(), [])

    def test_other_events_and_bad_rows_are_ignored(self):
        self.assertIsNone(quota.seen({"type": "result"}))
        self.assertIsNone(quota.seen({"type": "rate_limit_event", "rate_limit_info": "x"}))

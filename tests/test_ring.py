"""The front desk calls: an urgent item rings the phone, day or night, at most once in fifteen minutes."""

from commandcenter import clock, inbox, telegram
from tests.harness import Case


class Ring(Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        real = telegram.notify
        telegram.notify = lambda text, **kw: self.sent.append(text) or True
        self.addCleanup(setattr, telegram, "notify", real)

    def test_an_urgent_item_rings_at_once_and_a_now_item_never_does(self):
        inbox.add("portfolio-manager", "The broker login is locked.", urgency="now")
        self.assertIsNone(inbox.ringing())
        inbox.add("portfolio-manager", "A payment failed and the order expires in 20 minutes.",
                  urgency="urgent")
        ring = inbox.ringing()
        self.assertEqual(ring["agent"], "portfolio-manager")
        self.assertIn("payment failed", ring["text"])
        # Telegram waits for the call to go unanswered, so it never covers the ringing banner.
        self.assertEqual(self.sent, [])
        inbox._telegram_if_unanswered(ring["id"], ring)
        self.assertTrue(any("Missed call from the front desk" in t for t in self.sent))

    def test_urgent_rings_in_quiet_hours_too(self):
        clock.freeze(clock.now() + 15 * 3600)  # 00:00, inside 23:30 to 07:00
        self.assertTrue(inbox.quiet())
        inbox.add("job-search", "The offer letter must be signed in 30 minutes.", urgency="urgent")
        self.assertIsNotNone(inbox.ringing())

    def test_every_urgent_item_rings_with_no_gap(self):
        inbox.add("a1", "First urgent thing.", urgency="urgent")
        first = inbox.ringing()
        clock.freeze(clock.now() + 20)
        inbox.add("a2", "Second urgent thing.", urgency="urgent")
        self.assertNotEqual(inbox.ringing()["id"], first["id"])
        self.assertEqual(inbox.ringing()["agent"], "a2")

    def test_a_ring_stays_offered_for_ten_minutes(self):
        inbox.add("a1", "Urgent.", urgency="urgent")
        clock.freeze(clock.now() + 9 * 60)
        self.assertIsNotNone(inbox.ringing())
        clock.freeze(clock.now() + inbox.RING_SECONDS)
        self.assertIsNone(inbox.ringing())

    def test_call_me_back_rings_the_same_item_ten_minutes_later(self):
        inbox.add("a1", "Urgent thing.", urgency="urgent")
        ring = inbox.ringing()
        inbox.callback(ring["id"])
        self.assertIsNone(inbox.ringing())
        clock.freeze(clock.now() + inbox.CALLBACK_SECONDS - 5)
        inbox.tick()
        self.assertIsNone(inbox.ringing())
        clock.freeze(clock.now() + 10)
        inbox.tick()
        self.assertEqual(inbox.ringing()["id"], ring["id"])

    def test_urgent_is_read_first(self):
        inbox.add("a1", "Today thing.", urgency="today")
        inbox.add("a2", "Urgent thing.", urgency="urgent")
        self.assertEqual(inbox.listing()["waiting"][0]["urgency"], "urgent")

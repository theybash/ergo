"""A question or permission box left a minute rings the owner once, outside quiet hours.

The ring itself is inbox.ring_check, tested in test_inbox. Here only the escalation is."""

import threading

from commandcenter import clock, inbox
from tests.test_inbox import InboxCase, local


class AskCall(InboxCase):
    def setUp(self):
        super().setUp()
        # The one-minute timer is real time; the tests drive the clock instead.
        real = threading.Timer
        class Quiet:
            def __init__(self, *a, **k): pass
            def start(self): pass
            daemon = True
        inbox.threading.Timer = Quiet
        self.addCleanup(lambda: setattr(inbox.threading, "Timer", real))
        self.rings = []
        real_ring = inbox.ring_check
        inbox.ring_check = lambda: self.rings.append(1)
        self.addCleanup(lambda: setattr(inbox, "ring_check", real_ring))
        clock.freeze(local(2026, 10, 14, 12, 0))

    def box(self, ask_id="a1", agent="acme"):
        return inbox.ask_opened(agent, {"id": ask_id, "tool": "Bash", "summary": "npm test"})

    def later(self, seconds):
        clock.freeze(clock.now() + seconds)

    def test_a_box_left_a_minute_turns_urgent_once(self):
        item = self.box()
        self.assertEqual(inbox.escalate_asks(), [])
        self.later(61)
        self.assertEqual(inbox.escalate_asks(), [item["id"]])
        self.assertEqual(inbox._held()["items"][item["id"]]["urgency"], "urgent")
        self.later(600)
        self.assertEqual(inbox.escalate_asks(), [])

    def test_answering_first_means_no_call(self):
        item = self.box()
        inbox.ask_closed("acme", "a1")
        self.later(61)
        self.assertEqual(inbox.escalate_asks(), [])
        self.assertEqual(inbox._held()["items"][item["id"]]["urgency"], "now")

    def test_quiet_hours_hold_it_until_they_end(self):
        clock.freeze(local(2026, 10, 14, 1, 0))
        self.box()
        self.later(120)
        self.assertEqual(inbox.escalate_asks(), [])
        clock.freeze(local(2026, 10, 14, 9, 0))
        self.assertEqual(len(inbox.escalate_asks()), 1)

    def test_several_boxes_escalate_together_for_one_ring(self):
        self.box("a1", "acme")
        self.box("a2", "personal-assistant")
        self.later(61)
        self.assertEqual(len(inbox.escalate_asks()), 2)

    def test_an_ordinary_item_never_escalates(self):
        self.add(text="The dentist moved to four.", urgency="now")
        self.later(3600)
        self.assertEqual(inbox.escalate_asks(), [])

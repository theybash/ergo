"""Cords: what happens to one after it finishes.

A cord is drawn on the console while it runs. Once it is over it belongs to the log, not the screen,
and the operator found a failed call still drawn twenty minutes later.
"""

import threading
import time

from commandcenter import cords
from tests.harness import Case


class Finishing(Case):
    def test_a_finished_call_leaves_the_list(self):
        """Transfers always did this and calls never did."""
        cord = {"id": "abc123", "kind": "call", "from": "one", "to": "two",
                "instruction": "ask", "state": "running", "budget": 4, "used": 1,
                "summary": None, "created": time.time(), "messages": []}
        with cords._lock:
            cords._cords[cord["id"]] = cord
        cords._forget_soon(cord["id"], after=0.05)
        for _ in range(60):
            if not cords.get("abc123"):
                break
            time.sleep(0.05)
        self.assertIsNone(cords.get("abc123"))

    def test_it_is_gone_from_the_list_the_console_reads(self):
        with cords._lock:
            cords._cords["keep"] = {"id": "keep", "kind": "call", "from": "a", "to": "b",
                                    "state": "running", "created": time.time(), "messages": []}
        self.assertEqual([c["id"] for c in cords.all()], ["keep"])
        cords._forget_soon("keep", after=0.05)
        for _ in range(60):
            if not cords.all():
                break
            time.sleep(0.05)
        self.assertEqual(cords.all(), [])

    def test_forgetting_one_that_has_already_gone_is_harmless(self):
        cords._forget_soon("never-existed", after=0.05)
        time.sleep(0.3)
        self.assertEqual(cords.all(), [])

    def test_a_cord_needs_two_different_agents(self):
        self.agent("box")
        with self.assertRaises(ValueError):
            cords.transfer("box", "box", "do a thing")

    def test_a_cord_to_an_agent_that_does_not_exist(self):
        self.agent("box")
        with self.assertRaises(KeyError):
            cords.transfer("box", "nobody", "do a thing")

    def test_both_ends_must_be_awake(self):
        """Waking one here would hide a decision the operator should make: waking costs memory."""
        self.agent("one")
        self.agent("two")
        with self.assertRaises(RuntimeError):
            cords.transfer("one", "two", "do a thing")


class Budget(Case):
    def test_a_hangup_on_the_last_message_completes_the_call(self):
        """Seventeen calls at budget 2 or 3 timed out, though the caller hung up or never could."""
        self.agent("one")
        self.agent("two")
        said = iter(["What is the plan?", "The plan is X.", "Thanks.\nHANGUP: the plan is X."])
        real_ask, real_awake = cords.ask, cords.broker.awake
        cords.ask = lambda agent, prompt, **kw: next(said)
        cords.broker.awake = lambda agent: True
        try:
            cord = cords.call("one", "two", "ask", budget=2)
            for _ in range(60):
                if cords.get(cord["id"])["state"] != "running":
                    break
                time.sleep(0.05)
        finally:
            cords.ask, cords.broker.awake = real_ask, real_awake
        done = cords.get(cord["id"])
        self.assertEqual((done["state"], done["summary"]), ("completed", "the plan is X."))


class Waiting(Case):
    def test_an_agent_still_working_gets_past_the_timeout(self):
        """On 8 Oct Creator answered a call after ten minutes of work, and the call had failed at five."""
        rec = self.agent("two")
        started = time.time()

        class Engine:
            def working(self):
                return time.time() - started < 2.5

        def send(agent, text, wake=False):
            threading.Timer(2.0, self.transcript_for, (rec, [self.said("the answer")])).start()
            return "sent"

        real = cords.broker.send, cords.broker.awake, cords.broker.get
        cords.broker.send, cords.broker.awake = send, lambda agent: True
        cords.broker.get = lambda agent: Engine()
        try:
            reply = cords.ask("two", "a question", timeout=1.0, working_limit=10.0)
        finally:
            cords.broker.send, cords.broker.awake, cords.broker.get = real
        self.assertEqual(reply, "the answer")

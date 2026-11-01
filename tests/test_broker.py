"""Sending to an agent, and the wait that makes it arrive.

The terminal is faked, because what is being tested is when the broker types, not what a pty does
with the bytes.
"""

import time

from commandcenter import broker, config, mailbox
from tests.harness import Case


# A real terminal that has been sitting idle has drawn something at some point, so "never drawn
# anything" is a startup condition rather than an idle one. Tests that meant "idle" said None at
# first, and the suite took sixteen seconds waiting for a cap that a real agent never hits.
IDLE = -30.0
DRAWING = 0.0


class FakeTerminal:
    def __init__(self, alive=True, last_output=None, accepts=True):
        self._alive = alive
        self.last_output = last_output
        self.accepts = accepts
        self.submitted = []
        self.pid = 4242

    def alive(self):
        return self._alive

    def submit(self, text):
        self.submitted.append(text)
        return self.accepts

    def working(self):
        return False


class Sending(Case):
    def install(self, term):
        broker._terminals["box"] = term
        self.addCleanup(broker.reset)

    def test_a_message_to_an_awake_agent_is_typed(self):
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE)
        self.install(term)
        self.assertEqual(broker.send("box", "hello"), "sent")
        self.assertEqual(term.submitted, ["hello"])

    def test_a_message_to_a_sleeping_agent_is_held_not_delivered(self):
        """Waking costs memory and ten seconds. Grey exists so an agent can cost nothing."""
        self.agent("box")
        self.assertEqual(broker.send("box", "a note"), "held")
        self.assertEqual(mailbox.waiting("box"), 1)

    def test_a_message_left_waits_even_for_an_awake_agent(self):
        """Typing it in would start a turn, and a turn on a cold cache costs the whole chat."""
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE)
        self.install(term)
        broker.leave("box", "the job found two things")
        self.assertEqual(term.submitted, [])
        self.assertEqual(mailbox.waiting("box", everything=True), 1)
        self.assertEqual(mailbox.waiting("box"), 0, "a job result never shows on his card")

    def test_what_was_left_goes_in_front_of_the_next_message(self):
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE)
        self.install(term)
        broker.leave("box", "the job found two things")
        broker.send("box", "hello")
        self.assertEqual(len(term.submitted), 1)
        typed = term.submitted[0]
        self.assertLess(typed.index("the job found two things"), typed.index("hello"))
        self.assertEqual(mailbox.waiting("box"), 0)

    def test_a_failed_send_keeps_what_was_left(self):
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE, accepts=False)
        self.install(term)
        broker.leave("box", "the job found two things")
        self.assertFalse(broker.send("box", "hello"))
        self.assertEqual(mailbox.waiting("box", everything=True), 1)
        self.assertEqual(mailbox.waiting("box"), 0, "put back still quiet")

    def test_it_waits_for_a_terminal_that_is_still_drawing(self):
        """The bug: a cord reported "sent" and the text never appeared, because the interface was
        mid-redraw. Silence is the readiness signal, and nothing was silent yet."""
        config.PROMPT_QUIET = 0.3
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + DRAWING)
        self.install(term)
        started = time.time()
        broker.send("box", "hello")
        self.assertGreater(time.time() - started, 0.25, "it should have waited for quiet")
        self.assertEqual(term.submitted, ["hello"])

    def test_a_quiet_terminal_is_not_waited_for(self):
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE)
        self.install(term)
        started = time.time()
        broker.send("box", "hello")
        self.assertLess(time.time() - started, 0.5, "there was nothing to wait for")

    def test_a_terminal_that_has_drawn_nothing_yet_is_waited_for_to_the_cap(self):
        """That is a startup condition: the process has not painted its interface, so typing is lost.

        The cap is what stops it waiting for ever, and it is lowered here rather than spending eight
        seconds of the suite proving one branch.
        """
        from commandcenter import config
        config.PROMPT_WAIT = 0.6
        self.agent("box")
        term = FakeTerminal(last_output=None)
        self.install(term)
        started = time.time()
        broker.send("box", "hello")
        self.assertGreaterEqual(time.time() - started, 0.5)
        self.assertEqual(term.submitted, ["hello"], "it sends anyway rather than holding for ever")

    def test_a_terminal_that_refuses_the_text_says_so(self):
        self.agent("box")
        self.install(FakeTerminal(last_output=time.time() + IDLE, accepts=False))
        self.assertFalse(broker.send("box", "hello"))

    def test_a_dead_terminal_is_the_same_as_no_terminal(self):
        self.agent("box")
        self.install(FakeTerminal(alive=False, last_output=time.time() + IDLE))
        self.assertEqual(broker.send("box", "a note"), "held")
        self.assertFalse(broker.awake("box"))

    def test_an_urgent_message_held_mid_task_goes_in_when_the_task_ends(self):
        """The owner: "Some things are time sensitive and should not wait for my prompt." A quiet
        one still waits, and nothing goes in while the turn runs or twice after it ends."""
        broker.SETTLE = broker.RECHECK = 0.2
        self.addCleanup(setattr, broker, "SETTLE", 1.0)
        self.addCleanup(setattr, broker, "RECHECK", 5.0)
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE)
        term.working = lambda: term.busy
        term.busy = True
        self.install(term)
        broker._modes["box"] = "chat"
        broker.leave("box", "a job result")
        self.assertEqual(broker.send("box", "a task report", wake=True), "held")
        time.sleep(0.4)
        self.assertEqual(term.submitted, [], "never into a running turn")
        self.assertEqual(mailbox.waiting("box", everything=True), 2)

        sent = []
        self.addCleanup(setattr, broker, "send", broker.send)
        broker.send = lambda aid, text, wake=False: sent.append((aid, text, wake)) or "sent"
        term.busy = False
        broker.turn_ended("box")
        broker.turn_ended("box")
        time.sleep(0.4)
        self.assertEqual(len(sent), 1, "once, however often the end is seen")
        self.assertIn("a task report", sent[0][1])
        self.assertTrue(sent[0][2])
        self.assertEqual(mailbox.waiting("box", everything=True), 0)

        broker.leave("box", "another job result")
        broker.turn_ended("box")
        time.sleep(0.4)
        self.assertEqual(len(sent), 1, "a quiet one alone waits for his next message")

    def test_an_urgent_message_goes_in_when_nobody_watches_the_console(self):
        """The console feed sees a spinner stop only while somebody watches. With nobody, the
        broker looks at the terminal again until the task ends."""
        broker.SETTLE = broker.RECHECK = 0.1
        self.addCleanup(setattr, broker, "SETTLE", 1.0)
        self.addCleanup(setattr, broker, "RECHECK", 5.0)
        self.agent("box")
        term = FakeTerminal(last_output=time.time() + IDLE)
        term.working = lambda: term.busy
        term.busy = True
        self.install(term)
        broker._modes["box"] = "chat"
        self.assertEqual(broker.send("box", "an alarm check", wake=True), "held")
        sent = []
        self.addCleanup(setattr, broker, "send", broker.send)
        broker.send = lambda aid, text, wake=False: sent.append(text) or "sent"
        time.sleep(0.3)
        self.assertEqual(sent, [])
        term.busy = False
        time.sleep(0.3)
        self.assertEqual(sent, ["an alarm check"])

    def test_state_is_green_or_grey_and_nothing_between(self):
        self.agent("box")
        self.assertEqual(broker.state("box"), "grey")
        self.install(FakeTerminal())
        self.assertEqual(broker.state("box"), "green")


class Restoring(Case):
    def test_it_wakes_what_the_last_daemon_had_awake(self):
        from commandcenter import status
        self.agent("box")
        self.agent("other")
        status.agent("box", True)
        status.stopping()
        status.agent("box", False)
        status.reset()
        woken = []
        real = broker.start
        broker.start = lambda agent_id: woken.append(agent_id)
        self.addCleanup(lambda: setattr(broker, "start", real))
        self.assertEqual(broker.restore(), ["box"])
        for _ in range(50):
            if woken:
                break
            time.sleep(0.05)
        self.assertEqual(woken, ["box"], "and not the agent that was asleep")

    def test_an_agent_whose_record_has_gone_is_skipped(self):
        from commandcenter import registry, status
        self.agent("box")
        status.agent("box", True)
        status.reset()
        registry.forget("box")
        self.assertEqual(broker.restore(), [])

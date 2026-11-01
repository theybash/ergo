"""The chat engine: the parser, and the switch between the two engines.

The parser gets its own tests against a real captured turn, eighteen lines of it, because that is
where the rules live and every one of them was learned by watching the stream rather than reading
a document. The broker tests use a fake `claude` that speaks the same stream-json, so the whole
path can run with no model, no money and no waiting.
"""

import json
import time
from pathlib import Path

from commandcenter import broker, chat, config, registry
from tests.harness import Case

HERE = Path(__file__).resolve().parent
# One real turn of `claude -p --output-format stream-json --include-partial-messages`, captured
# from the CLI on this machine. The agent was asked to count to five.
CAPTURED = HERE / "events.jsonl"
FAKE = HERE / "fake_claude.py"


def frames_for(lines):
    out = []
    stream = chat.Stream(out.append)
    for line in lines:
        stream.feed(line)
    return out, stream


class Parsing(Case):
    def test_a_real_turn_becomes_the_frames_the_browser_draws(self):
        """The whole contract, against the file the CLI actually wrote."""
        out, _ = frames_for(CAPTURED.read_text().splitlines())
        self.assertEqual(out, [
            {"t": "live", "phase": "start"},
            {"t": "live", "phase": "delta", "text": "1\n2"},
            {"t": "live", "phase": "delta", "text": "1\n2\n3\n4"},
            {"t": "live", "phase": "delta", "text": "1\n2\n3\n4\n5"},
            {"t": "live", "phase": "end"},
            {"t": "settled"},
        ])

    def test_a_delta_carries_the_whole_reply_so_far(self):
        """Cumulative, not the new piece. A dropped frame then costs nothing: the next one has
        every character, so a phone on a bad connection repairs itself instead of losing a line."""
        out, _ = frames_for(CAPTURED.read_text().splitlines())
        deltas = [f["text"] for f in out if f.get("phase") == "delta"]
        self.assertEqual(deltas[-1], "1\n2\n3\n4\n5")
        for earlier, later in zip(deltas, deltas[1:]):
            self.assertTrue(later.startswith(earlier))

    def test_thinking_is_not_shown(self):
        """The turn opens with a thinking block and its deltas. A chat is what was said."""
        lines = CAPTURED.read_text().splitlines()
        self.assertTrue(any("thinking_delta" in ln for ln in lines), "the fixture lost its thinking")
        out, _ = frames_for(lines)
        self.assertEqual(out[0], {"t": "live", "phase": "start"},
                         "nothing at all before the first word of the answer")

    def test_the_session_id_is_read_from_the_init_event(self):
        _, stream = frames_for(CAPTURED.read_text().splitlines())
        self.assertEqual(stream.session_id, "f55985da-5b9a-462e-9a53-691f80b696aa")

    def test_a_result_row_is_recognised_wherever_type_sits_in_it(self):
        """The real result row does not start with "type". Anything that peeks at the front of the
        line misses the end of every turn and leaves the chat thinking for ever."""
        row = json.dumps({"duration_api_ms": 1391, "total_cost_usd": 0.002, "type": "result"})
        self.assertFalse(row.startswith('{"type"'))
        out, _ = frames_for([row])
        self.assertEqual(out, [{"t": "settled"}])

    def test_an_assistant_snapshot_does_not_double_the_live_text(self):
        """Those rows are the whole message so far, sent again after every block. Adding them to
        the live text prints the reply twice."""
        out, stream = frames_for(CAPTURED.read_text().splitlines()[:13])
        self.assertEqual(stream.live_text, "1\n2\n3\n4\n5")
        self.assertEqual(len([f for f in out if f.get("phase") == "delta"]), 3,
                         "three text deltas, and the snapshot beside them is not a fourth")

    def test_text_before_a_tool_leaves_the_live_reply_when_the_tool_starts(self):
        """That text is a finished message in the session file, drawn from the history. Kept in
        the live reply too, it showed twice for as long as the tool ran."""
        out = []
        stream = chat.Stream(out.append)
        stream.handle({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Let me measure one thing first."}}})
        stream.handle({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": "ping -c 3 host"}}}})
        self.assertEqual(stream.live_text, "")
        self.assertIn({"t": "live", "phase": "delta", "text": ""}, out)
        stream.handle({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 2,
            "delta": {"type": "text_delta", "text": "Clean."}}})
        self.assertEqual(stream.live_text, "Clean.")

    def test_state_is_said_once_per_change(self):
        out = []
        stream = chat.Stream(out.append)
        stream.set_state("thinking")
        stream.set_state("thinking")
        stream.handle({"type": "result"})
        self.assertEqual([f for f in out if f["t"] == "state"],
                         [{"t": "state", "state": "thinking"},
                          {"t": "state", "state": "idle"}])

    def test_a_tool_call_is_one_line_named_after_the_tool(self):
        out = []
        stream = chat.Stream(out.append)
        stream.handle({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": "ls -la"}}}})
        self.assertEqual(out, [{"t": "tool", "name": "Bash", "summary": "command: ls -la"}])

    def test_a_tool_whose_input_arrives_in_pieces_is_named_once_it_is_whole(self):
        """The input streams in as json fragments, so naming the tool at the start would say
        "Bash" with an empty command, which is the only part anybody reads."""
        out = []
        stream = chat.Stream(out.append)
        stream.handle({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}}})
        self.assertEqual(out, [], "nothing said yet, the command is still arriving")
        for piece in ('{"comm', 'and": "ls', ' -la"}'):
            stream.handle({"type": "stream_event", "event": {
                "type": "content_block_delta", "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": piece}}})
        stream.handle({"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 1}})
        self.assertEqual(out, [{"t": "tool", "name": "Bash", "summary": "command: ls -la"}])

    def test_a_permission_ask_becomes_a_card(self):
        """The line as the CLI wrote it, captured from a real run. The summary is one line because
        a card is one line; the whole command travels beside it for the tap that opens it."""
        out, stream = frames_for([json.dumps({
            "type": "control_request", "request_id": "r1", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash", "display_name": "Bash",
                "input": {"command": "curl -s https://example.com"},
                "decision_reason": "This command requires approval",
                "tool_use_id": "toolu_01MLfX1Zsw"}})])
        self.assertEqual(out, [{"t": "ask", "id": "r1", "kind": "perm", "tool": "Bash",
                                "summary": "command: curl -s https://example.com",
                                "input": {"command": "curl -s https://example.com"}}])
        self.assertEqual(stream.pending(), out, "and it is sent again to whoever attaches next")

    def test_the_question_tool_arrives_as_the_same_ask(self):
        """AskUserQuestion is not a second channel. It is a permission ask whose tool happens to be
        a question, so the name is the only thing that tells the two apart."""
        asked = {"questions": [{"question": "Which colour?", "header": "Colour",
                                "options": [{"label": "red", "description": "warm"}],
                                "multiSelect": False}]}
        out, _ = frames_for([json.dumps({
            "type": "control_request", "request_id": "r2", "request": {
                "subtype": "can_use_tool", "tool_name": "AskUserQuestion",
                "input": asked, "requires_user_interaction": True}})])
        self.assertEqual(out, [{"t": "ask", "id": "r2", "kind": "question",
                                "questions": asked["questions"]}])

    def test_a_control_request_nobody_can_answer_is_not_a_card(self):
        """Claude Code sends other control requests and will send more. A card with a yes and a no
        for a request neither button fits never goes away."""
        out, stream = frames_for([json.dumps({
            "type": "control_request", "request_id": "r3",
            "request": {"subtype": "mcp_message", "server_name": "platform"}})])
        self.assertEqual(out, [])
        self.assertEqual(stream.pending(), [])

    def test_a_turn_that_ends_takes_its_open_ask_with_it(self):
        """Claude only waits inside a turn. An interrupt ends the turn, and the card it was
        waiting on is dead the moment the result row lands."""
        out, stream = frames_for([
            json.dumps({"type": "control_request", "request_id": "r4", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}}}),
            json.dumps({"subtype": "error_during_execution",
                        "terminal_reason": "aborted_streaming", "type": "result"})])
        self.assertEqual(out[-2:], [{"t": "ask_done", "id": "r4"}, {"t": "settled"}])
        self.assertEqual(stream.pending(), [])

    def test_a_line_that_is_not_json_is_dropped(self):
        out, _ = frames_for(["", "not json at all", "{"])
        self.assertEqual(out, [])


class CommandLine(Case):
    def test_the_chat_engine_asks_for_typed_events_and_the_terminal_does_not(self):
        rec = self.agent("box")
        argv = broker.argv_for(rec, False, engine="chat")
        for flag in ("-p", "--input-format", "stream-json", "--verbose",
                     "--include-partial-messages", "--permission-prompts"):
            self.assertIn(flag, argv)
        self.assertNotIn("-p", broker.argv_for(rec, False))

    def test_the_chat_engine_is_started_so_that_it_can_ask(self):
        """Two flags, and the first one is the whole of slice two. With --permission-prompts host
        alone the ask never comes: the turn ends in seven seconds with the tool refused, which is
        an afternoon nobody gets back. --permission-prompt-tool stdio is what makes Claude ask on
        the pipe we already read.

        The second flag skips nothing by itself. It only lets the mode be set to bypassPermissions
        later, and without it that one option in the browser can never be chosen."""
        argv = broker.argv_for(self.agent("box"), False, engine="chat")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "host")
        self.assertEqual(argv[argv.index("--permission-prompt-tool") + 1], "stdio")
        self.assertIn("--allow-dangerously-skip-permissions", argv)
        self.assertNotIn("none", argv, "nothing is refused unasked any more")

    def test_the_engine_still_starts_in_the_agents_own_permission_mode(self):
        """--allow-dangerously-skip-permissions is a permission to choose, not a choice."""
        rec = self.agent("box", permission_mode="acceptEdits")
        argv = broker.argv_for(rec, False, engine="chat")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")

    def test_both_engines_name_the_same_conversation(self):
        """This is what makes one session file. Everything else about them may differ."""
        rec = self.agent("box")
        for resume, flag in ((False, "--session-id"), (True, "--resume")):
            term = broker.argv_for(rec, resume)
            engine = broker.argv_for(rec, resume, engine="chat")
            self.assertEqual(term[term.index(flag) + 1], rec["session_id"])
            self.assertEqual(engine[engine.index(flag) + 1], rec["session_id"])

    def test_remote_control_stays_out_of_the_chat_engine(self):
        """Remote Control is somebody taking over a session. A headless process has no screen to
        take over."""
        config.REMOTE_CONTROL = True
        argv = broker.argv_for(self.agent("box"), False, engine="chat")
        self.assertNotIn("--remote-control", argv)


class Fake(Case):
    """The broker against a fake claude that speaks stream-json. Real processes, no model."""

    def setUp(self):
        super().setUp()
        config.CLAUDE_BIN = str(FAKE)
        self.addCleanup(broker.shutdown_all, True)

    def frames(self, agent_id):
        got = []
        broker.listen(agent_id, got.append)
        self.addCleanup(broker.unlisten, agent_id, got.append)
        return got

    def until(self, test, cap=15.0):
        deadline = time.time() + cap
        while time.time() < deadline:
            if test():
                return True
            time.sleep(0.05)
        return False


class Switching(Fake):
    """Which engine runs, and what a tab switch may and may not do to it."""

    def test_switching_leaves_exactly_one_engine_alive(self):
        """From grey, which is where most agents are. The switch starts nothing, the first
        message starts the chat engine, and that engine is the only one running."""
        self.agent("box")
        got = self.frames("box")
        self.assertEqual(broker.switch("box", "chat"), "chat")
        self.assertIsNone(broker.get("box"))
        self.assertIsNone(broker.chat("box"), "the chat engine waits for the first message")
        self.assertIn({"t": "mode", "mode": "chat"}, got)

        self.assertEqual(broker.send("box", "hello"), "sent")
        engine = broker.chat("box")
        self.assertTrue(engine.alive())
        self.assertIsNone(broker.get("box"), "one engine at a time, always")

        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertEqual(got[got.index({"t": "mode", "mode": "chat"}) + 1],
                         {"t": "echo", "text": "hello"},
                         "the message appears the instant it is taken, before any answer")
        self.assertIn({"t": "live", "phase": "delta", "text": "you said hello"}, got)
        self.assertIn({"t": "live", "phase": "end"}, got)
        self.assertEqual(engine.state, "idle")

    def test_switching_never_wakes_a_sleeping_agent(self):
        """Opening a tab is not a decision to spend memory and tokens."""
        self.agent("box")
        self.assertEqual(broker.switch("box", "terminal"), "terminal")
        self.assertIsNone(broker.get("box"))
        self.assertFalse(broker.awake("box"))
        self.assertEqual(broker.switch("box", "chat"), "chat")
        self.assertIsNone(broker.chat("box"))
        self.assertFalse(broker.awake("box"))

    def test_going_back_to_the_terminal_keeps_the_agent_awake(self):
        """The person was mid conversation and asked for the real terminal."""
        self.agent("box")
        broker.switch("box", "chat")
        self.assertEqual(broker.send("box", "hello"), "sent")
        engine = broker.chat("box")
        self.assertEqual(broker.switch("box", "terminal"), "terminal")
        self.assertFalse(engine.alive())
        self.assertIsNone(broker.chat("box"))
        self.assertTrue(broker.awake("box"))

    def test_a_chat_engine_counts_as_awake(self):
        self.agent("box")
        broker.switch("box", "chat")
        self.assertEqual(broker.state("box"), "grey")
        broker.send("box", "hello")
        self.assertEqual(broker.state("box"), "green")
        self.assertTrue(broker.sleep("box"))
        self.assertTrue(self.until(lambda: not broker.awake("box")))
        self.assertEqual(broker.mode("box"), "off")

    def test_waking_an_agent_that_is_chatting_ends_the_chat_engine(self):
        """Any path to a terminal, not only the switch. Two claude processes on one session id
        make the second one start a copy of the conversation rather than join it."""
        self.agent("box")
        broker.switch("box", "chat")
        broker.send("box", "hello")
        engine = broker.chat("box")
        term = broker.start("box")
        self.assertFalse(engine.alive())
        self.assertIsNone(broker.chat("box"))
        self.assertTrue(term.alive())
        self.assertEqual(broker.mode("box"), "terminal")

    def test_a_message_in_chat_mode_is_never_held(self):
        """A mailbox exists because typing at a sleeping terminal loses the text. A chat engine
        has no such problem: the message is what starts it."""
        from commandcenter import mailbox
        self.agent("box")
        broker.switch("box", "chat")
        self.assertEqual(broker.send("box", "hello", wake=False), "sent")
        self.assertEqual(mailbox.waiting("box"), 0)

    # -- a tab switch starts nothing and stops nothing ---------------------------
    #
    # The console posts the mode whenever a tab opens. So these are the tests of an incident:
    # opening the Chat tab on a live agent killed its terminal and started nothing in its place,
    # and the Terminal tab then went grey and asked to wake the agent that had just been working.

    def test_opening_the_chat_tab_does_not_kill_a_live_terminal(self):
        """Looking is not deciding. The terminal somebody is working in survives the tab."""
        self.agent("box")
        got = self.frames("box")
        term = broker.start("box")
        self.assertTrue(self.until(term.alive))

        self.assertEqual(broker.switch("box", "chat"), "chat")
        self.assertTrue(term.alive(), "the terminal is still running behind the chat tab")
        self.assertIs(broker.get("box"), term)
        self.assertEqual(broker.state("box"), "green")
        self.assertIsNone(broker.chat("box"), "and nothing was started beside it")
        self.assertIn({"t": "mode", "mode": "chat"}, got)

    def test_a_look_at_the_chat_and_back_leaves_the_same_terminal_running(self):
        """Chat, then terminal, on an awake agent. One terminal throughout and never a grey
        moment in between, because there was never anything to restart."""
        self.agent("box")
        term = broker.start("box")
        self.assertTrue(self.until(term.alive))

        broker.switch("box", "chat")
        self.assertTrue(broker.awake("box"), "not grey for an instant")
        self.assertEqual(broker.switch("box", "terminal"), "terminal")
        self.assertIs(broker.get("box"), term, "the same terminal, not a second one")
        self.assertTrue(term.alive())
        self.assertIsNone(broker.chat("box"))

    # -- a typed message goes to the chat engine, whatever else is running ------
    #
    # The second incident, and the one these tests exist for. Routing used to follow whatever was
    # alive, so the owner typed a question in the Chat tab of an agent that was awake in a
    # terminal, Claude drew the question on a screen nobody was looking at, and the chat showed a
    # note and then silence. The cards ride the chat engine's own pipe, so none of them appeared.
    # Chat goes to `claude -p` and the terminal is the terminal.

    def idle_terminal(self, name="box"):
        """An awake agent in chat mode whose terminal has finished painting itself."""
        config.PROMPT_WAIT, config.PROMPT_QUIET = 0.6, 0.1
        self.agent(name)
        term = broker.start(name)
        self.assertTrue(self.until(term.alive))
        # Silence is the readiness signal, and it is the same signal working() reads. A terminal
        # that has just drawn its first line still counts as busy, which is correct and is why
        # this waits rather than sending at once.
        self.assertTrue(self.until(lambda: not term.working(), cap=5.0))
        broker.switch(name, "chat")
        return term

    def test_a_message_in_chat_mode_closes_an_idle_terminal_and_starts_the_chat_engine(self):
        """The rule, in one test. Chat goes to the chat engine even when a pty is sitting there."""
        term = self.idle_terminal()
        got = self.frames("box")

        self.assertEqual(broker.send("box", "hello"), "sent")
        self.assertFalse(term.alive(), "the terminal is closed, and closed before anything starts")
        engine = broker.chat("box")
        self.assertTrue(engine and engine.alive())
        self.assertEqual(broker.mode("box"), "chat", "and the tab is still the chat tab")

        # The turn really ran on the chat engine, which is the whole point: the words stream and
        # the cards have a pipe to arrive on.
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertIn({"t": "live", "phase": "delta", "text": "you said hello"}, got)

    def test_after_the_swap_exactly_one_engine_is_alive_and_it_is_the_chat_engine(self):
        """Two claude processes on one session id write two files, and one file is the only thing
        that keeps the terminal and the chat showing one conversation."""
        term = self.idle_terminal()
        self.assertEqual(broker.send("box", "hello"), "sent")

        self.assertFalse(term.alive())
        self.assertIsNone(broker.get("box"), "and the record says so at once, not eventually")
        rows = broker.snapshot()
        self.assertEqual(list(rows), ["box"])
        self.assertEqual(rows["box"]["engine"], "chat")

    def test_the_person_is_told_once_that_the_terminal_was_closed(self):
        """A screen that vanishes with no explanation reads as a fault. It is said, and said once,
        before the message is echoed, so the reason arrives before the wait does."""
        self.idle_terminal()
        got = self.frames("box")

        broker.send("box", "hello")
        notes = [f for f in got if f["t"] == "note"]
        self.assertEqual(len(notes), 1, "once, not once per frame")
        self.assertEqual(notes[0]["text"], broker.SWAP_NOTE)
        self.assertLess(got.index(notes[0]), got.index({"t": "echo", "text": "hello"}))

    def test_a_message_in_chat_mode_is_held_when_the_terminal_is_mid_task(self):
        """The one guard. Killing a terminal that is running a command throws the command away,
        which is a different thing from closing an idle one, so the message waits instead."""
        from commandcenter import mailbox
        term = self.idle_terminal()
        got = self.frames("box")
        # The interface repaints while it works, so recent output is what busy looks like.
        term.last_output = time.time()

        self.assertEqual(broker.send("box", "hello"), "held")
        self.assertTrue(term.alive(), "the work is still running")
        self.assertIs(broker.get("box"), term)
        self.assertIsNone(broker.chat("box"), "and nothing was started beside it")
        self.assertEqual(mailbox.waiting("box"), 1)
        self.assertEqual([f["text"] for f in got if f["t"] == "note"], [broker.BUSY_NOTE_QUIET])
        self.assertEqual([f for f in got if f["t"] == "echo"], [],
                         "nothing was sent, so nothing is echoed as sent")

    # -- the terminal keeps every path it had ------------------------------------
    #
    # Cron, cords and Telegram all send to an agent that is not in chat mode, and they all want a
    # terminal. Only the chat tab changed.

    def test_terminal_mode_still_routes_to_the_terminal_with_and_without_wake(self):
        from commandcenter import mailbox
        config.PROMPT_WAIT, config.PROMPT_QUIET = 0.6, 0.1
        self.agent("box")
        broker.switch("box", "terminal")

        # Nothing running and nobody asked to wake it: the mailbox holds it, as it always did.
        self.assertEqual(broker.send("box", "later"), "held")
        self.assertEqual(mailbox.waiting("box"), 1)
        self.assertFalse(broker.awake("box"))

        # wake=True opens a terminal, not a chat engine.
        self.assertEqual(broker.send("box", "now", wake=True), "sent")
        term = broker.get("box")
        self.assertTrue(term and term.alive())
        self.assertIsNone(broker.chat("box"))

        # And a live terminal takes the next message down the same pty.
        self.assertTrue(self.until(lambda: not term.working(), cap=5.0))
        self.assertEqual(broker.send("box", "again"), "sent")
        self.assertIs(broker.get("box"), term)
        self.assertIsNone(broker.chat("box"))
        self.assertEqual(broker.mode("box"), "terminal")

    def test_off_mode_still_routes_to_the_terminal_with_and_without_wake(self):
        """Off is where every agent is after a daemon restart, and it is what cron sends into."""
        from commandcenter import mailbox
        config.PROMPT_WAIT, config.PROMPT_QUIET = 0.6, 0.1
        self.agent("box")
        self.assertEqual(broker.mode("box"), "off")

        self.assertEqual(broker.send("box", "later"), "held")
        self.assertEqual(mailbox.waiting("box"), 1)
        self.assertFalse(broker.awake("box"))

        self.assertEqual(broker.send("box", "now", wake=True), "sent")
        term = broker.get("box")
        self.assertTrue(term and term.alive())
        self.assertIsNone(broker.chat("box"), "waking is a terminal, and it still is")
        self.assertEqual(broker.mode("box"), "terminal", "a wake is the mode, not only a process")

    def test_a_message_in_chat_mode_with_nothing_running_starts_the_chat_engine(self):
        """The common case, because agents are grey most of the time."""
        self.agent("box")
        got = self.frames("box")
        broker.switch("box", "chat")
        self.assertIsNone(broker.chat("box"))

        self.assertEqual(broker.send("box", "hello"), "sent")
        engine = broker.chat("box")
        self.assertTrue(engine.alive())
        self.assertIsNone(broker.get("box"), "one engine at a time, always")
        self.assertEqual([f for f in got if f["t"] == "note"], [],
                         "nothing to explain: this turn streams")

    def test_switching_to_the_terminal_swaps_a_live_chat_engine(self):
        """The one swap a switch may make. The Terminal tab is blank without a pty, and the
        person asked to see it, so the chat engine ends and a terminal takes its place."""
        self.agent("box")
        broker.switch("box", "chat")
        broker.send("box", "hello")
        engine = broker.chat("box")
        self.assertTrue(engine.alive())

        self.assertEqual(broker.switch("box", "terminal"), "terminal")
        self.assertFalse(engine.alive(), "dead before the terminal may start, always")
        self.assertIsNone(broker.chat("box"))
        term = broker.get("box")
        self.assertTrue(term and term.alive())

    def test_the_engine_says_so_when_it_dies(self):
        self.agent("box")
        broker.switch("box", "chat")
        broker.send("box", "hello")
        got = self.frames("box")
        engine = broker.chat("box")
        engine.terminate(force=True)
        self.assertTrue(self.until(lambda: any(f["t"] == "note" for f in got)), got)
        self.assertIsNone(broker.chat("box"), "and it is gone, so the next message starts a new one")


class Asking(Fake):
    """Claude stopping to ask, and the answer finding its way back.

    Slice one started the engine with --permission-prompts none, so anything that needed approval
    was refused and the model explained afterwards that it had not been allowed. Now it asks on
    the same pipe the answer comes back on, and waits without a time limit, which is what the
    operator asked for: let it stay until I say yes.
    """

    QUESTION = "Which colour do you prefer?"

    def stopped(self, message="ASK-PERM please"):
        """An agent in chat mode, stopped at an ask. Returns the frames so far and the ask."""
        self.agent("box")
        broker.switch("box", "chat")
        got = self.frames("box")
        self.assertEqual(broker.send("box", message), "sent")
        self.assertTrue(self.until(lambda: any(f["t"] == "ask" for f in got)), got)
        return got, [f for f in got if f["t"] == "ask"][0]

    def replied(self, got):
        """The text of the last live delta, which is what the turn ended up saying."""
        deltas = [f["text"] for f in got if f.get("phase") == "delta"]
        return deltas[-1] if deltas else None

    # -- permission asks ---------------------------------------------------------

    def test_a_permission_ask_stops_the_turn_and_puts_a_card_up(self):
        got, ask = self.stopped()
        self.assertEqual(ask["kind"], "perm")
        self.assertEqual(ask["tool"], "Bash")
        self.assertEqual(ask["summary"], "command: curl -s https://example.com")
        self.assertEqual(ask["input"], {"command": "curl -s https://example.com"})
        self.assertNotIn({"t": "settled"}, got, "the turn is waiting, not finished")

    def test_yes_lets_the_tool_run_and_the_turn_finish(self):
        got, ask = self.stopped()
        self.assertTrue(broker.answer("box", ask["id"], allow=True))
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertEqual(self.replied(got), "allowed")
        self.assertIn({"t": "ask_done", "id": ask["id"]}, got,
                      "the card goes for every browser watching, not only the one that answered")

    def test_no_refuses_the_tool_and_the_turn_finishes_anyway(self):
        """Denied is an answer. The model is told and says so, which is the difference between a
        refusal and silence."""
        got, ask = self.stopped()
        self.assertTrue(broker.answer("box", ask["id"], allow=False))
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertEqual(self.replied(got), "denied")

    def test_an_ask_that_is_already_answered_cannot_be_answered_again(self):
        """Two phones on one agent is the normal case, so the second tap finds a stale card."""
        _, ask = self.stopped()
        self.assertTrue(broker.answer("box", ask["id"], allow=True))
        self.assertFalse(broker.answer("box", ask["id"], allow=True))
        self.assertFalse(broker.answer("box", "not-an-ask-at-all", allow=True))

    # -- question asks -----------------------------------------------------------

    def test_a_question_ask_carries_its_options(self):
        _, ask = self.stopped("ASK-QUESTION please")
        self.assertEqual(ask["kind"], "question")
        question = ask["questions"][0]
        self.assertEqual(question["question"], self.QUESTION)
        self.assertEqual([o["label"] for o in question["options"]], ["red", "blue"])
        self.assertFalse(question["multiSelect"])

    def test_the_label_that_was_chosen_is_what_the_turn_gets(self):
        """The answers go back with the questions they answer, not on their own. The whole input
        as it arrived, plus an answers object keyed by the exact question text."""
        got, ask = self.stopped("ASK-QUESTION please")
        self.assertTrue(broker.answer("box", ask["id"], answers={self.QUESTION: "blue"}))
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertEqual(self.replied(got), "blue")

    def test_free_text_is_passed_through_as_it_was_typed(self):
        """The other box on the card. Nothing checks it against the options, and nothing should:
        the point of that box is the answer nobody offered."""
        got, ask = self.stopped("ASK-QUESTION please")
        broker.answer("box", ask["id"], answers={self.QUESTION: "purple, actually"})
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertEqual(self.replied(got), "purple, actually")

    def test_a_skipped_question_is_a_deny_that_says_nobody_chose(self):
        """Skip is no on a question. It is a deny of the tool use, so no answer is invented, and
        its words say the question went unanswered rather than that an option was refused."""
        got, ask = self.stopped("ASK-QUESTION please")
        self.assertTrue(broker.answer("box", ask["id"], allow=False))
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertEqual(self.replied(got), "denied: " + broker.SKIPPED)
        self.assertIn({"t": "ask_done", "id": ask["id"]}, got)

    def test_stop_halts_a_turn_that_is_still_writing(self):
        """The stop button in the composer, pressed while the reply is still coming in."""
        self.agent("box")
        broker.switch("box", "chat")
        got = self.frames("box")
        self.assertEqual(broker.send("box", "SLOW please"), "sent")
        self.assertTrue(self.until(lambda: any(f.get("phase") == "delta" for f in got)), got)
        self.assertTrue(broker.stop("box"))
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertIn({"t": "state", "state": "idle"}, got)
        self.assertNotIn("word99", self.replied(got))

    # -- a card that outlives the page it was drawn on ---------------------------

    def test_a_waiting_ask_is_there_for_the_next_page_that_attaches(self):
        """A phone locks its screen and the socket goes. The engine is still waiting, so the card
        has to come back, frame for frame."""
        _, ask = self.stopped()
        self.assertEqual(broker.pending_asks("box"), [ask])
        broker.answer("box", ask["id"], allow=True)
        self.assertEqual(broker.pending_asks("box"), [], "answered, so there is nothing to redraw")

    def test_the_engine_dying_takes_the_card_with_it_and_says_so(self):
        """Nothing is left on the screen that a tap cannot answer, and the loss is said once. A
        card that vanishes on its own looks like an answer that was sent."""
        got, ask = self.stopped()
        broker.chat("box").terminate(force=True)
        self.assertTrue(self.until(lambda: {"t": "ask_done", "id": ask["id"]} in got), got)
        notes = [f["text"] for f in got if f["t"] == "note"]
        self.assertIn(broker.LOST_ASK_NOTE, notes)
        self.assertEqual(broker.pending_asks("box"), [])

    # -- the permission mode -----------------------------------------------------

    def test_a_live_engine_takes_a_new_permission_mode_without_restarting(self):
        """This is the point of doing it on the pipe. An agent stopped at a question can be given
        a freer hand and carry on, instead of losing the turn to a restart."""
        self.agent("box", permission_mode="acceptEdits")
        broker.switch("box", "chat")
        got = self.frames("box")
        broker.send("box", "hello")
        engine = broker.chat("box")
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)

        self.assertEqual(broker.set_permission_mode("box", "plan"), "plan")
        self.assertIn({"t": "perm_mode", "mode": "plan"}, got)
        self.assertIs(broker.chat("box"), engine, "the same process, still running")
        self.assertTrue(engine.alive())
        self.assertEqual(broker.permission_mode("box"), "plan")
        self.assertEqual(registry.get("box")["permission_mode"], "plan",
                         "and the next start uses it too")

    def test_with_nothing_running_the_record_is_the_whole_change(self):
        self.agent("box", permission_mode="acceptEdits")
        self.assertEqual(broker.permission_mode("box"), "acceptEdits")
        self.assertEqual(broker.set_permission_mode("box", "bypassPermissions"),
                         "bypassPermissions")
        self.assertEqual(registry.get("box")["permission_mode"], "bypassPermissions")
        self.assertIsNone(broker.chat("box"), "and nothing was started to hear it")

    def test_a_running_terminal_is_told_to_use_its_own_keystroke(self):
        """A pseudo-terminal has no pipe to ask on, and the person is sitting in front of it."""
        self.agent("box", permission_mode="acceptEdits")
        term = broker.start("box")
        self.assertTrue(self.until(term.alive))
        got = self.frames("box")

        self.assertEqual(broker.set_permission_mode("box", "plan"), "plan")
        self.assertIn(broker.TERMINAL_PERM_NOTE, [f["text"] for f in got if f["t"] == "note"])
        self.assertEqual(registry.get("box")["permission_mode"], "plan")
        self.assertTrue(term.alive(), "and the terminal is untouched")

    def test_a_permission_mode_nobody_measured_is_refused(self):
        """manual and auto were never tested against the engine here, and the engine answers
        "manual" as "default", which is already a sign the two words are not one word."""
        self.agent("box")
        for word in ("manual", "auto", "", "default"):
            with self.assertRaises(ValueError):
                broker.set_permission_mode("box", word)

    # -- stopping ----------------------------------------------------------------

    def test_stop_in_chat_mode_halts_the_turn_and_keeps_the_process(self):
        """Slice one ended the engine, which cost ten seconds of loading the conversation again.
        The interrupt halts the turn and the same process takes the next message."""
        got, ask = self.stopped()
        engine = broker.chat("box")

        self.assertTrue(broker.stop("box"))
        self.assertTrue(engine.alive(), "interrupted, not killed")
        self.assertIs(broker.chat("box"), engine)
        self.assertTrue(self.until(lambda: {"t": "settled"} in got), got)
        self.assertIn({"t": "ask_done", "id": ask["id"]}, got,
                      "the turn ended, so the card it was waiting on is dead")

        self.assertEqual(broker.send("box", "hello"), "sent")
        self.assertTrue(self.until(
            lambda: {"t": "live", "phase": "delta", "text": "you said hello"} in got), got)


class Autocompact(Case):
    def test_every_agent_starts_with_the_size_limit(self):
        rec = self.agent("box")
        for engine in ("chat", None):
            argv = broker.argv_for(rec, False, engine=engine) if engine else broker.argv_for(rec, False)
            self.assertIn("--autocompact", argv)
            self.assertEqual(argv[argv.index("--autocompact") + 1], config.AUTOCOMPACT)

    def test_an_agent_can_be_left_out(self):
        """The assistant and Juno, 15 October: off until recall() brings back what compaction drops."""
        rec = registry.update(self.agent("box")["id"], autocompact=False)
        self.assertNotIn("--autocompact", broker.argv_for(rec, False, engine="chat"))
        rec = registry.update(rec["id"], autocompact=True)
        self.assertIn("--autocompact", broker.argv_for(rec, False, engine="chat"))

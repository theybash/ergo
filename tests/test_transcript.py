"""Reading a conversation back out of a session file.

This is the chat view's data source, so it has to hold the shape of a conversation: what was said,
what tools ran, and what they returned, in the order they happened.
"""

from commandcenter import transcript
from tests.harness import Case


class Reading(Case):
    def test_nothing_yet(self):
        rec = self.agent("box")
        self.assertEqual(transcript.recent(rec), [])

    def test_what_was_said_by_whom(self):
        rec = self.agent("box")
        self.transcript_for(rec, [self.said("hello", "user"), self.said("hi back")])
        rows = transcript.recent(rec)
        self.assertEqual([(r["who"], r["text"]) for r in rows],
                         [("operator", "hello"), ("agent", "hi back")])
        self.assertTrue(all(r["kind"] == "text" for r in rows))

    def test_claude_codes_own_notes_are_not_the_operator_speaking(self):
        """After a Read of an image Claude Code writes an isMeta user row about the scaling."""
        rec = self.agent("box")
        note = self.said("[Image: original 2308x1580, displayed at 2000x1369.]", "user")
        note["isMeta"] = True
        self.transcript_for(rec, [self.said("look at this", "user"), note, self.said("seen it")])
        rows = transcript.recent(rec)
        self.assertEqual([(r["who"], r["text"]) for r in rows],
                         [("operator", "look at this"), ("agent", "seen it")])

    def test_the_compact_summary_is_marked_to_fold(self):
        rec = self.agent("box")
        summary = self.said("This session is being continued.", "user")
        summary["isCompactSummary"] = True
        self.transcript_for(rec, [summary, self.said("on it")])
        rows = transcript.page(rec)["messages"]
        self.assertEqual([(r["who"], r.get("compacted", False)) for r in rows],
                         [("operator", True), ("agent", False)])

    def test_a_message_sent_mid_turn_is_his_and_a_task_notice_is_not(self):
        """Sent while the agent works, a message is saved as a queued_command attachment."""
        rec = self.agent("box")
        def queued(text, mode):
            return {"type": "attachment", "timestamp": "2026-10-07T09:22:01.685Z",
                    "attachment": {"type": "queued_command", "commandMode": mode,
                                   "prompt": [{"type": "text", "text": text}]}}
        self.transcript_for(rec, [self.said("start", "user"), queued("did it finish?", "prompt"),
                                  queued("<task-notification>done</task-notification>", "task-notification"),
                                  self.said("yes")])
        rows = transcript.recent(rec)
        self.assertEqual([(r["who"], r["text"]) for r in rows],
                         [("operator", "start"), ("operator", "did it finish?"), ("agent", "yes")])

    def test_a_reply_in_pieces_reads_back_whole(self):
        """A caller wrote HANGUP, used a tool, wrote one more line, and the call missed the HANGUP."""
        rec = self.agent("box")
        self.transcript_for(rec, [self.said("before"), self.said("over to you", "user"),
                                  self.said("HANGUP: done"), self.used_tool("Bash", command="ls"),
                                  self.tool_said("ok"), self.said("one more line")])
        self.assertEqual(transcript.said_since(rec, 1), "HANGUP: done\n\none more line")

    def test_a_tool_call_reads_back_whole(self):
        rec = self.agent("box")
        long_out = "line\n" * 5000
        self.transcript_for(rec, [self.used_tool("Bash", command="ls -la /very/long/path", description="list"),
                                  self.tool_said(long_out)])
        got = transcript.tool_call(rec, "t1")
        self.assertEqual(got["tool"], "Bash")
        self.assertEqual(got["input"], "ls -la /very/long/path")
        self.assertEqual(got["result"], long_out)
        self.assertFalse(got["error"])
        self.assertIsNone(transcript.tool_call(rec, "nope"))

    def test_a_tool_call_is_its_own_row(self):
        rec = self.agent("box")
        self.transcript_for(rec, [
            self.said("let me look", "assistant"),
            self.used_tool("Bash", command="ls -la", description="list"),
            self.tool_said("total 0"),
        ])
        rows = transcript.recent(rec)
        self.assertEqual([r["kind"] for r in rows], ["text", "tool", "result"])
        self.assertEqual(rows[1]["tool"], "Bash")
        self.assertIn("command: ls -la", rows[1]["input"])
        self.assertEqual(rows[2]["text"], "total 0")
        self.assertFalse(rows[2]["error"])

    def test_a_question_keeps_its_options_so_the_console_can_draw_them(self):
        """A phone cannot read the dialog drawn in the terminal, so the options have to travel."""
        rec = self.agent("box")
        self.transcript_for(rec, [{
            "type": "assistant", "timestamp": "t",
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": "q1", "name": "AskUserQuestion",
                "input": {"questions": [{"question": "Which one?", "header": "Pick",
                                         "multiSelect": False,
                                         "options": [{"label": "A", "description": "first"},
                                                     {"label": "B", "description": "second"}]}]}}]}}])
        row = transcript.recent(rec)[0]
        self.assertEqual(row["tool"], "AskUserQuestion")
        import json
        got = json.loads(row["inputRaw"])
        self.assertEqual([o["label"] for o in got["questions"][0]["options"]], ["A", "B"])

    def test_a_refused_card_is_marked_and_his_pick_folds_into_a_good_one(self):
        rec = self.agent("box")
        bad = self.used_tool("mcp__platform__show", kind="choice", data={"options": ["A"]})
        good = self.used_tool("mcp__platform__show", kind="choice",
                              data={"question": "Which?", "options": ["A", "B"]})
        good["message"]["content"][0]["id"] = "t2"
        pick = {"type": "user", "timestamp": "t", "message": {"role": "user", "content": [
            {"type": "text", "text": '[Choice t2] To "Which?", the owner picked: B'}]}}
        self.transcript_for(rec, [bad, self.tool_said("question is missing", error=True),
                                  good, pick])
        rows = transcript.recent(rec)
        self.assertTrue(rows[0]["refused"])
        self.assertEqual([r.get("answer") for r in rows if r["kind"] == "tool"], [None, "B"])
        self.assertFalse(any(r["kind"] == "text" for r in rows))

    def test_an_ordinary_tool_carries_no_raw_input(self):
        rec = self.agent("box")
        self.transcript_for(rec, [self.used_tool("Bash", command="ls")])
        self.assertNotIn("inputRaw", transcript.recent(rec)[0])

    def test_more_is_whether_the_read_reached_the_start(self):
        """Lines that are not rows make a short answer, and a short answer is not the start."""
        rec = self.agent("box")
        noise = [{"type": "system", "timestamp": "t"}] * 60
        self.transcript_for(rec, [self.said("first")] + noise + [self.said("last")])
        got = transcript.page(rec, 5)
        self.assertEqual([r["text"] for r in got["messages"]], ["last"])
        self.assertTrue(got["more"])
        self.assertFalse(transcript.page(rec, 50)["more"])

    def test_a_failed_tool_says_so(self):
        rec = self.agent("box")
        self.transcript_for(rec, [self.used_tool("Bash", command="nope"),
                                  self.tool_said("command not found", error=True)])
        self.assertTrue(transcript.recent(rec)[-1]["error"])

    def test_a_result_and_a_call_share_an_id_so_the_chat_can_pair_them(self):
        rec = self.agent("box")
        self.transcript_for(rec, [self.used_tool("Read", file_path="/tmp/x"),
                                  self.tool_said("contents")])
        rows = transcript.recent(rec)
        self.assertEqual(rows[0]["id"], rows[1]["id"])

    def test_several_blocks_in_one_entry_stay_in_order(self):
        """The agent speaks, then calls a tool, in one message. A chat shows two rows."""
        rec = self.agent("box")
        self.transcript_for(rec, [{
            "type": "assistant", "timestamp": "t",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "id"}}]}}])
        self.assertEqual([r["kind"] for r in transcript.recent(rec)], ["text", "tool"])

    def test_a_huge_tool_result_is_cut_and_says_how_much_was_cut(self):
        """Nearly three quarters of a real session's characters are tool output."""
        rec = self.agent("box")
        self.transcript_for(rec, [self.used_tool("Read", file_path="/big"),
                                  self.tool_said("x" * 5000)])
        row = transcript.recent(rec)[-1]
        self.assertLess(len(row["text"]), 1400)
        self.assertIn("more characters", row["text"])

    def test_a_subagent_conversation_is_not_shown(self):
        rec = self.agent("box")
        side = dict(self.said("private"), isSidechain=True)
        self.transcript_for(rec, [side, self.said("public")])
        self.assertEqual([r["text"] for r in transcript.recent(rec)], ["public"])

    def test_only_the_last_page_is_returned(self):
        rec = self.agent("box")
        self.transcript_for(rec, [self.said(f"line {n}") for n in range(40)])
        rows = transcript.recent(rec, 5)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["text"], "line 39")

    def test_entries_that_are_not_messages_are_skipped(self):
        rec = self.agent("box")
        self.transcript_for(rec, [{"type": "mode", "mode": "acceptEdits"},
                                  {"type": "last-prompt", "text": "x"},
                                  self.said("only this")])
        self.assertEqual([r["text"] for r in transcript.recent(rec)], ["only this"])

"""OpenCode, beside Claude Code: the argv, the config, the turn, and the ledger.

Nothing here runs the real binary. The fake is a small script that prints the event shapes this
version of OpenCode prints, which is enough to check that a turn becomes the same frames the chat
tab already draws and that the session id it makes is written down. The ledger is a SQLite file
built to the columns the real one has.
"""

import json
import sqlite3
import time
from pathlib import Path

from tests.harness import Case

from commandcenter import broker, config, opencode, registry


FAKE = '''#!/usr/bin/env python3
import json, sys

text = sys.stdin.read()
sid = "ses_fake_0001"
events = [
    {"type": "step_start", "sessionID": sid},
    {"type": "text", "sessionID": sid,
     "part": {"type": "text", "text": "the answer: " + text.strip()}},
    {"type": "tool_use", "sessionID": sid,
     "part": {"tool": "read",
              "state": {"status": "completed", "input": {"path": "a.txt"},
                        "output": "A"}}},
    {"type": "step_finish", "sessionID": sid},
]
for event in events:
    print(json.dumps(event), flush=True)
'''


class OpenCodeCase(Case):
    def setUp(self):
        super().setUp()
        self.bin = self.dir / "fake-opencode"
        self.bin.write_text(FAKE)
        self.bin.chmod(0o755)
        config.OPENCODE_BIN = str(self.bin)
        config.OPENCODE_DB = str(self.dir / "opencode.db")
        # The ledger cache is keyed by agent id and lives between tests otherwise.
        opencode.forget("oc")

    def agent(self, name="oc", **kw):
        cwd = self.dir / name
        cwd.mkdir(parents=True, exist_ok=True)
        return registry.create(name=name, cwd=str(cwd), engine="opencode", **kw)


class ConfigTests(OpenCodeCase):
    def test_the_config_is_one_file_per_agent_and_names_the_agent(self):
        rec = self.agent()
        path = opencode.write_config(rec)
        assert path == opencode.config_path(rec)
        assert path.parent.name == "opencode"
        body = json.loads(path.read_text())
        assert body["mcp"]["servers"]["platform"]["environment"]["PLATFORM_AGENT"] == rec["id"]
        assert body["mcp"]["servers"]["platform"]["type"] == "local"
        assert isinstance(body["permissions"], list)
        # V2 dropped the old object; a file that still has it is silently not applying it.
        assert "permission" not in body

    def test_plan_is_read_only_and_accept_edits_is_the_agents_own_folder(self):
        def effect(rules, action, resource="*"):
            found = [r["effect"] for r in rules
                     if r["action"] == action and r["resource"] == resource]
            return found[-1] if found else None

        # The registry never makes an agent in plan mode, so it is set after.
        rec = registry.update(self.agent()["id"], permission_mode="plan")
        plan = opencode._permissions(rec)
        assert effect(plan, "*") == "deny"
        assert effect(plan, "read") == "allow"
        assert effect(plan, "read", "*.env") == "deny"
        assert effect(plan, "edit") is None
        assert effect(plan, "shell") == "deny"

        rec = registry.update(rec["id"], permission_mode="acceptEdits")
        edits = opencode._permissions(rec)
        assert effect(edits, "*") == "allow"
        assert effect(edits, "external_directory") == "deny"
        assert effect(edits, "shell") == "deny"
        assert effect(edits, "read", "*.env") == "deny"
        assert effect(edits, "read", "*.env.example") == "allow"

        rec = registry.update(rec["id"], permission_mode="bypassPermissions")
        assert opencode._permissions(rec) == [
            {"action": "*", "resource": "*", "effect": "allow"}]

    def test_the_argv_carries_the_session_the_model_and_auto(self):
        rec = self.agent(model="openai/gpt-5", permission_mode="bypassPermissions")
        rec = registry.update(rec["id"], opencode_session="ses_from_opencode")
        argv = opencode.argv_for(rec, resume=True)
        assert argv[0] == str(self.bin)
        assert argv[1] == "run"
        assert argv[2] == "--standalone"
        assert argv[3:5] == ["--format", "json"]
        assert argv[argv.index("--session") + 1] == "ses_from_opencode"
        assert argv[argv.index("--model") + 1] == "openai/gpt-5"
        assert "--auto" in argv

    def test_a_new_conversation_has_no_session_flag(self):
        rec = self.agent()
        argv = opencode.argv_for(rec, resume=False)
        assert "--session" not in argv
        assert "--standalone" in argv
        assert "run" in argv


class EngineTests(OpenCodeCase):
    def test_a_turn_becomes_frames_and_the_session_id_is_kept(self):
        rec = self.agent()
        frames = []
        kept = []
        engine = opencode.OpenCodeEngine(
            rec["id"], opencode.argv_for(rec, resume=False), rec["cwd"],
            on_event=frames.append, on_session=kept.append)
        self.addCleanup(engine.terminate, True)
        assert engine.say("what is in the file?") is True

        deadline = time.time() + 10
        while time.time() < deadline and not any(f.get("t") == "settled" for f in frames):
            time.sleep(0.02)

        assert any(f.get("t") == "settled" for f in frames), frames
        tool = [f for f in frames if f.get("t") == "tool"]
        assert tool and tool[0]["name"] == "read"
        said = "".join(f.get("text") or "" for f in frames if f.get("t") == "live")
        assert "what is in the file?" in said
        assert kept and kept[0] == "ses_fake_0001"
        assert engine.session_id == "ses_fake_0001"
        assert engine.alive() is True
        assert engine.working() is False

    def test_the_broker_starts_opencode_and_writes_its_session_id_down(self):
        rec = self.agent()
        engine = broker.start_chat(rec["id"])
        self.addCleanup(engine.terminate, True)
        assert engine.say("hello") is True

        deadline = time.time() + 10
        while time.time() < deadline and not registry.get(rec["id"])["opencode_session"]:
            time.sleep(0.02)

        assert registry.get(rec["id"])["opencode_session"] == "ses_fake_0001"
        assert broker.chat(rec["id"]) is engine


class PermissionTests(OpenCodeCase):
    def test_changing_the_mode_rewrites_the_file_and_the_live_engine(self):
        rec = self.agent(permission_mode="plan")
        engine = broker.start_chat(rec["id"])
        self.addCleanup(engine.terminate, True)
        seen = []
        broker.listen(rec["id"], seen.append)

        now = broker.set_permission_mode(rec["id"], "bypassPermissions")

        assert now == "bypassPermissions"
        body = json.loads(opencode.config_path(rec).read_text())
        assert body["permissions"] == [
            {"action": "*", "resource": "*", "effect": "allow"}]
        assert "permission" not in body
        assert engine.permission_mode == "bypassPermissions"
        assert {"t": "perm_mode", "mode": "bypassPermissions"} in seen


class LedgerTests(OpenCodeCase):
    def setUp(self):
        super().setUp()
        self.rec = self.agent(permission_mode="acceptEdits")
        self.rec = registry.update(self.rec["id"], opencode_session="ses_ledger")
        self.cwd = self.rec["cwd"]
        opencode.forget(self.rec["id"])

        conn = sqlite3.connect(config.OPENCODE_DB)
        conn.execute(
            "create table session_v2 (id text primary key, directory text, title text, cost real, "
            "tokens_input integer, tokens_output integer, tokens_reasoning integer, "
            "tokens_cache_read integer, tokens_cache_write integer, "
            "time_created integer, time_updated integer)")
        conn.execute(
            "create table session_message (session_id text, type text, seq integer, "
            "time_created integer, data text)")
        conn.execute(
            "insert into session_v2 values (?,?,?,?,?,?,?,?,?,?,?)",
            ("ses_ledger", self.cwd, "a title", 0.25, 100, 40, 0, 10, 5, 1000, 3000))
        conn.execute("insert into session_message values (?,?,?,?,?)",
                     ("ses_ledger", "user", 1, 1000,
                      json.dumps({"time": {"created": 1000}, "text": "look at a.txt"})))
        conn.execute("insert into session_message values (?,?,?,?,?)",
                     ("ses_ledger", "assistant", 2, 2000, json.dumps({
                         "time": {"created": 2000, "completed": 2500},
                         "model": {"id": "m", "providerID": "p"},
                         "tokens": {"input": 7, "output": 3,
                                    "cache": {"read": 2, "write": 1}},
                         "content": [
                             {"type": "text", "text": "it says A"},
                             {"type": "tool", "id": "t1", "name": "read",
                              "state": {"status": "completed",
                                        "input": {"path": "a.txt"}, "output": "A"}},
                         ]})))
        conn.commit()
        conn.close()

    def test_summary_reads_like_a_claude_summary(self):
        facts = opencode.summary(self.rec)
        assert facts["exists"] is True
        assert facts["turns"] == 1
        assert facts["tools_used"] == 1
        assert facts["last_message"] == "it says A"
        assert facts["tokens"] == {"input": 100, "output": 40,
                                   "cache_write": 5, "cache_read": 10}
        assert facts["cost_usd"] == 0.25
        assert facts["by_model"]["p/m"]["input"] == 7

    def test_recent_and_tool_call_match_the_transcript_shape(self):
        rows = opencode.recent(self.rec)
        assert [r["kind"] for r in rows] == ["text", "text", "tool", "result"]
        assert rows[0] == {"kind": "text", "who": "operator", "at": rows[0]["at"],
                           "text": "look at a.txt"}
        assert rows[2]["tool"] == "read"
        assert rows[3]["text"] == "A"

        call = opencode.tool_call(self.rec, "t1")
        assert call["tool"] == "read"
        assert call["error"] is False
        assert "a.txt" in call["input"]

    def test_the_chat_page_has_the_paged_shape(self):
        from commandcenter import transcript
        page = transcript.page(self.rec, 2)
        assert [r["kind"] for r in page["messages"]] == ["tool", "result"]
        assert page["more"] is True

    def test_a_session_made_in_a_folder_is_discovered(self):
        found = opencode.discover_session(self.cwd, since=0, cap=1.0)
        assert found == "ses_ledger"

    def test_another_folder_is_not_claimed(self):
        assert opencode.discover_session(str(self.dir / "elsewhere"), since=0, cap=0.1) is None


class SendTests(OpenCodeCase):
    def test_a_message_takes_the_chat_path_even_in_terminal_mode(self):
        from commandcenter import broker
        rec = self.agent()
        broker._set_mode(rec["id"], "terminal")
        got = []
        real = broker._send_chat
        broker._send_chat = lambda aid, text: got.append((aid, text)) or "sent"
        try:
            assert broker.send(rec["id"], "hello") == "sent"
        finally:
            broker._send_chat = real
        assert got == [(rec["id"], "hello")]


class ModelTests(OpenCodeCase):
    def test_free_is_read_from_the_prices_not_the_name(self):
        zero = {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}
        assert opencode._free([zero]) is True
        assert opencode._free([zero, {"input": 0.1, "output": 0}]) is False
        assert opencode._free([]) is False


class InstructionTests(OpenCodeCase):
    def test_the_config_names_the_ergo_note_and_the_folders_claude_md(self):
        rec = self.agent()
        (Path(rec["cwd"]) / "CLAUDE.md").write_text("rules")
        body = json.loads(opencode.write_config(rec).read_text())
        note, claude = body["instructions"]
        assert "set_alarm" in Path(note).read_text()
        assert claude == str(Path(rec["cwd"]) / "CLAUDE.md")

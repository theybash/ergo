"""Recruiting: bringing a hidden agent back, and adopting a conversation that already exists.

Every test here runs over a fake projects folder in a temporary directory. The real one belongs
to Claude Code, it holds the owner's own work, and a test that wrote into it would be writing
into his conversations.

The listing is the part worth testing hard. It reads files somebody else writes, in a folder
that also holds scratch sessions, subagent transcripts and files a byte long, so most of the
rules are about what it refuses to show.
"""

import http.client
import json
import os
import sqlite3
import threading
from http.server import ThreadingHTTPServer

from commandcenter import broker, config, recruits, registry, server
from tests.harness import Case


class RecruitCase(Case):
    """A fake projects folder, and a way to put one session file in it."""

    def setUp(self):
        super().setUp()
        config.PROJECTS.mkdir(parents=True, exist_ok=True)

    def session(self, slug, session_id, rows=(), when=None, pad=True, folder_for=None):
        """Write one session file the way Claude Code writes one.

        Padded past a kilobyte by default, because the listing refuses anything smaller and
        almost every test here wants its file to be listed.
        """
        folder = config.PROJECTS / slug
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{session_id}.jsonl"
        lines = [json.dumps(row) for row in rows]
        if pad:
            lines.append(json.dumps({"type": "padding", "text": "x" * 2000}))
        path.write_text("\n".join(lines) + "\n")
        if when is not None:
            os.utime(path, (when, when))
        if folder_for:
            # The subagent transcripts of that session, in a folder named after it.
            sub = folder / folder_for
            (sub / "subagents").mkdir(parents=True, exist_ok=True)
            (sub / "subagents" / "one.jsonl").write_text(
                json.dumps({"type": "user", "message": {"role": "user", "content": "sub"}}) + "\n")
        return path

    def user(self, text, cwd="/home/dev/thing", at="2026-08-01T09:00:00Z", meta=False):
        row = {"type": "user", "cwd": cwd, "timestamp": at,
               "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        if meta:
            row["isMeta"] = True
        return row

    def titled(self, name):
        return {"type": "custom-title", "customTitle": name}

    def opencode(self, session_id="ses_test", cwd=None, title=None, first="hi",
                 turns=1, when=1_760_000_000_000, parent_id=None, archived=None):
        """Write one OpenCode session the way OpenCode's ledger holds one."""
        conn = sqlite3.connect(config.OPENCODE_DB)
        conn.execute("""
            create table if not exists session_v2 (
                id text primary key, directory text, parent_id text, title text,
                cost real, tokens_input integer, tokens_output integer,
                tokens_reasoning integer, tokens_cache_read integer,
                tokens_cache_write integer, time_created integer,
                time_updated integer, time_archived integer)""")
        conn.execute("""
            create table if not exists session_message (
                id text primary key, session_id text, type text, seq integer,
                time_created integer, time_updated integer, data text)""")
        folder = cwd or str(self.dir / "thing")
        conn.execute(
            "insert into session_v2 (id, directory, parent_id, title, cost, "
            "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, "
            "tokens_cache_write, time_created, time_updated, time_archived) "
            "values (?, ?, ?, ?, 0, 0, 0, 0, 0, 0, ?, ?, ?)",
            (session_id, folder, parent_id, title, when, when + 1000, archived))
        if first is not None:
            conn.execute(
                "insert into session_message values (?, ?, 'user', 1, ?, ?, ?)",
                (session_id + "-u", session_id, when, when,
                 json.dumps({"time": {"created": when}, "text": first})))
        for n in range(turns):
            conn.execute(
                "insert into session_message values (?, ?, 'assistant', ?, ?, ?, ?)",
                (session_id + f"-a{n}", session_id, 2 + n, when + n,
                 when + n, json.dumps({"time": {"created": when + n},
                                       "content": [{"type": "text", "text": "answer"}]})))
        conn.commit()
        conn.close()
        return folder

    def ids(self, rows):
        return [row["session_id"] for row in rows]


# -- the listing -----------------------------------------------------------------

class Listing(RecruitCase):
    def test_a_session_is_described_by_its_name_folder_and_first_words(self):
        self.session("-home-dev-thing", "aaa", [self.titled("night shift"),
                                                self.user("read the log and tell me")])
        row = recruits.sessions()[0]
        self.assertEqual(row["session_id"], "aaa")
        self.assertEqual(row["name"], "night shift")
        self.assertEqual(row["first"], "read the log and tell me")
        self.assertEqual(row["cwd"], "/home/dev/thing")
        self.assertEqual(row["slug"], "-home-dev-thing")
        self.assertEqual(row["started"], "2026-08-01T09:00:00Z")
        self.assertEqual(row["turns"], 1)
        self.assertFalse(row["registered"])

    def test_a_session_with_no_title_row_has_no_name(self):
        self.session("-home-dev-thing", "aaa", [self.user("hello")])
        self.assertIsNone(recruits.sessions()[0]["name"])

    def test_an_agent_name_row_names_it_too(self):
        self.session("-home-dev-thing", "aaa",
                     [{"type": "agent-name", "name": "scraper"}, self.user("go")])
        self.assertEqual(recruits.sessions()[0]["name"], "scraper")

    def test_the_first_message_is_cut_to_one_line(self):
        self.session("-home-dev-thing", "aaa", [self.user("one\ntwo   three")])
        self.assertEqual(recruits.sessions()[0]["first"], "one two three")

    def test_the_first_message_is_cut_to_160_characters(self):
        self.session("-home-dev-thing", "aaa", [self.user("y" * 400)])
        first = recruits.sessions()[0]["first"]
        self.assertEqual(len(first), 160)
        self.assertTrue(first.endswith("…"))

    def test_a_note_claude_code_injected_is_not_the_first_message(self):
        self.session("-home-dev-thing", "aaa",
                     [self.user("machinery", meta=True), self.user("what the owner typed")])
        self.assertEqual(recruits.sessions()[0]["first"], "what the owner typed")

    def test_a_tool_result_row_counts_as_a_turn_but_says_nothing(self):
        result = {"type": "user", "cwd": "/home/dev/thing", "timestamp": "2026-08-01T09:00:01Z",
                  "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}
        self.session("-home-dev-thing", "aaa", [result, self.user("the real question")])
        row = recruits.sessions()[0]
        self.assertEqual(row["first"], "the real question")
        self.assertEqual(row["turns"], 2)

    def test_a_scratch_folder_is_never_offered(self):
        self.session("-tmp-claude-1000-scratchpad", "aaa", [self.user("throwaway")])
        self.session("-home-dev-thing", "bbb", [self.user("real")])
        self.assertEqual(self.ids(recruits.sessions()), ["bbb"])

    def test_a_file_under_a_kilobyte_is_never_offered(self):
        self.session("-home-dev-thing", "small", [self.user("hi")], pad=False)
        self.session("-home-dev-thing", "big", [self.user("hi")])
        self.assertEqual(self.ids(recruits.sessions()), ["big"])

    def test_a_subagent_folder_is_not_a_session(self):
        self.session("-home-dev-thing", "aaa", [self.user("hi")], folder_for="aaa")
        self.assertEqual(self.ids(recruits.sessions()), ["aaa"])

    def test_the_newest_conversation_is_first(self):
        self.session("-home-dev-thing", "old", [self.user("a")], when=1_000_000)
        self.session("-home-dev-thing", "new", [self.user("b")], when=3_000_000)
        self.session("-home-dev-other", "middle", [self.user("c")], when=2_000_000)
        self.assertEqual(self.ids(recruits.sessions()), ["new", "middle", "old"])

    def test_the_last_time_is_the_file_time(self):
        self.session("-home-dev-thing", "aaa", [self.user("a")], when=1_760_000_000)
        self.assertEqual(recruits.sessions()[0]["last"], "2025-10-09T08:53:20Z")

    def test_the_list_stops_at_two_hundred(self):
        for n in range(recruits.MAX_SESSIONS + 15):
            self.session("-home-dev-thing", f"s{n:04d}", [self.user("a")], when=1_000_000 + n)
        rows = recruits.sessions()
        self.assertEqual(len(rows), recruits.MAX_SESSIONS)
        # The newest survive the cut, so the row the owner is looking for is the one that stays.
        self.assertEqual(rows[0]["session_id"], "s0214")

    def test_turn_counting_stops_at_the_cap(self):
        rows = [self.user(f"turn {n}") for n in range(recruits.MAX_TURNS + 40)]
        self.session("-home-dev-thing", "aaa", rows)
        self.assertEqual(recruits.sessions()[0]["turns"], recruits.MAX_TURNS)

    def test_a_session_the_console_already_has_says_so(self):
        rec = registry.create(name="known", cwd=str(self.dir), session_id="aaa")
        self.session("-home-dev-thing", "aaa", [self.user("hi")])
        self.assertTrue(recruits.sessions()[0]["registered"])
        registry.hide(rec["id"])
        self.assertTrue(recruits.sessions()[0]["registered"],
                        "hiding an agent does not free its conversation")

    def test_a_projects_folder_that_is_not_there_is_an_empty_list(self):
        config.PROJECTS = self.dir / "nothing-here"
        self.assertEqual(recruits.sessions(), [])

    def test_a_broken_row_does_not_lose_the_file(self):
        path = self.session("-home-dev-thing", "aaa", [self.user("good")])
        path.write_text("{not json at all\n" + path.read_text())
        self.assertEqual(recruits.sessions()[0]["first"], "good")

    def test_one_session_is_found_by_id_even_past_the_cap(self):
        for n in range(recruits.MAX_SESSIONS + 5):
            self.session("-home-dev-thing", f"s{n:04d}", [self.user("a")], when=1_000_000 + n)
        found = recruits.find("s0000")
        self.assertIsNotNone(found)
        self.assertEqual(found["cwd"], "/home/dev/thing")

    def test_an_id_with_a_path_in_it_finds_nothing(self):
        self.session("-home-dev-thing", "aaa", [self.user("hi")])
        self.assertIsNone(recruits.find("../-home-dev-thing/aaa"))

    def test_the_name_falls_back_to_the_last_part_of_the_folder(self):
        row = {"cwd": "/home/dev/night shift", "name": None, "session_id": "aaa"}
        self.assertEqual(recruits.default_name(row), "night shift")
        self.assertEqual(recruits.default_name(row, "typed"), "typed")
        self.assertEqual(recruits.default_name({**row, "name": "titled"}), "titled")


class OpenCodeListing(RecruitCase):
    def test_an_opencode_session_is_described_like_a_claude_one(self):
        folder = "/Users/dev/opencode thing"
        self.opencode("ses_one", cwd=folder, title="night shift",
                      first="read the log", turns=2)
        row = next(r for r in recruits.sessions() if r["session_id"] == "ses_one")
        self.assertEqual(row["name"], "night shift")
        self.assertEqual(row["first"], "read the log")
        self.assertEqual(row["cwd"], folder)
        self.assertEqual(row["turns"], 2)
        self.assertEqual(row["engine"], "opencode")
        self.assertIsNone(row["size"])
        self.assertFalse(row["registered"])

    def test_an_opencode_scratch_folder_is_never_offered(self):
        self.opencode("ses_scratch", cwd="/tmp/cc-scratch")
        self.opencode("ses_real", cwd="/Users/dev/real")
        self.assertEqual(self.ids(recruits.sessions()), ["ses_real"])

    def test_an_opencode_session_with_no_messages_is_never_offered(self):
        self.opencode("ses_empty", cwd="/Users/dev/empty", first=None, turns=0)
        self.opencode("ses_real", cwd="/Users/dev/real", first="hello")
        self.assertEqual(self.ids(recruits.sessions()), ["ses_real"])

    def test_a_child_session_is_not_a_conversation(self):
        self.opencode("ses_child", cwd="/Users/dev/child", parent_id="ses_parent")
        self.opencode("ses_parent", cwd="/Users/dev/parent")
        self.assertEqual(self.ids(recruits.sessions()), ["ses_parent"])

    def test_an_opencode_session_the_console_has_says_registered(self):
        folder = "/Users/dev/known"
        self.opencode("ses_one", cwd=folder)
        rec = registry.create(name="known", cwd=folder, engine="opencode",
                              opencode_session="ses_one")
        self.assertTrue(next(r for r in recruits.sessions()
                             if r["session_id"] == "ses_one")["registered"])
        registry.hide(rec["id"])
        self.assertTrue(next(r for r in recruits.sessions()
                             if r["session_id"] == "ses_one")["registered"])

    def test_claude_and_opencode_sessions_are_mixed_newest_first(self):
        self.session("-home-dev-thing", "old", [self.user("a")], when=1_000_000)
        self.opencode("ses_new", cwd="/Users/dev/new",
                      when=3_000_000_000, first="later")
        self.assertEqual(self.ids(recruits.sessions()), ["ses_new", "old"])

    def test_one_opencode_session_is_found_by_id_even_past_the_cut(self):
        for n in range(recruits.MAX_SESSIONS + 5):
            self.opencode(f"ses_{n:04d}", cwd=str(self.dir / f"p{n}"),
                          when=1_000_000_000 + n)
        found = recruits.find("ses_0000", engine="opencode")
        self.assertIsNotNone(found)
        self.assertEqual(found["engine"], "opencode")

    def test_an_opencode_id_with_a_path_in_it_finds_nothing(self):
        self.assertIsNone(recruits.find("../ses_one", engine="opencode"))


# -- the routes ------------------------------------------------------------------

class Routes(RecruitCase):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]
        # Nothing may start a process. Every test in here asserts on the list of agents, and one
        # of them asserts on this list staying empty.
        self.started = []
        real = broker.start
        broker.start = lambda agent_id: self.started.append(agent_id)
        self.addCleanup(lambda: setattr(broker, "start", real))

    def hit(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body or {}) if method == "POST" else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_the_listing_route_carries_both_halves(self):
        rec = self.agent("boxer")
        registry.hide(rec["id"])
        self.session("-home-dev-thing", "aaa", [self.user("hello")])
        code, got = self.hit("GET", "/api/recruits")
        self.assertEqual(code, 200)
        self.assertEqual([r["id"] for r in got["hidden"]], ["boxer"])
        self.assertEqual(self.ids(got["sessions"]), ["aaa"])

    def test_a_visible_agent_is_not_a_recruit(self):
        self.agent("boxer")
        self.assertEqual(self.hit("GET", "/api/recruits")[1]["hidden"], [])

    def test_show_brings_a_hidden_agent_back(self):
        rec = self.agent("boxer")
        registry.hide(rec["id"])
        code, got = self.hit("POST", "/api/recruits/show", {"id": "boxer"})
        self.assertEqual(code, 200)
        self.assertEqual(got["id"], "boxer")
        self.assertFalse(registry.get("boxer")["hidden"])

    def test_show_says_404_for_an_agent_that_does_not_exist(self):
        self.assertEqual(self.hit("POST", "/api/recruits/show", {"id": "ghost"})[0], 404)

    def test_adopt_makes_an_agent_out_of_a_session(self):
        folder = self.dir / "thing"
        folder.mkdir()
        self.session("-home-dev-thing", "aaa",
                     [self.titled("night shift"), self.user("hi", cwd=str(folder))])
        code, got = self.hit("POST", "/api/recruits/adopt", {"session_id": "aaa"})
        self.assertEqual(code, 201)
        self.assertEqual(got["name"], "night shift")
        self.assertEqual(got["session_id"], "aaa")
        self.assertEqual(got["cwd"], str(folder))
        self.assertEqual(registry.get(got["id"])["session_id"], "aaa")

    def test_adopt_never_starts_an_engine(self):
        folder = self.dir / "thing"
        folder.mkdir()
        self.session("-home-dev-thing", "aaa", [self.user("hi", cwd=str(folder))])
        self.hit("POST", "/api/recruits/adopt", {"session_id": "aaa"})
        self.assertEqual(self.started, [], "the owner wakes it from the card, like any agent")

    def test_adopt_makes_an_agent_out_of_an_opencode_session(self):
        folder = self.dir / "opencode thing"
        folder.mkdir()
        self.opencode("ses_oc", cwd=str(folder), title="oc session", first="hi")
        code, got = self.hit("POST", "/api/recruits/adopt",
                             {"session_id": "ses_oc", "engine": "opencode"})
        self.assertEqual(code, 201)
        self.assertEqual(got["name"], "oc session")
        self.assertEqual(got["engine"], "opencode")
        self.assertEqual(got["opencode_session"], "ses_oc")
        self.assertEqual(got["cwd"], str(folder))
        self.assertNotEqual(got["session_id"], "ses_oc",
                            "OpenCode's id lives in opencode_session, not Claude's field")
        self.assertEqual(registry.get(got["id"])["opencode_session"], "ses_oc")
        self.assertEqual(self.started, [])

    def test_adopt_infers_opencode_when_the_client_omits_engine(self):
        folder = self.dir / "opencode thing"
        folder.mkdir()
        self.opencode("ses_oc", cwd=str(folder))
        code, got = self.hit("POST", "/api/recruits/adopt", {"session_id": "ses_oc"})
        self.assertEqual(code, 201)
        self.assertEqual(got["engine"], "opencode")
        self.assertEqual(got["opencode_session"], "ses_oc")

    def test_adopting_an_opencode_session_twice_hands_back_the_same_agent(self):
        folder = self.dir / "opencode thing"
        folder.mkdir()
        self.opencode("ses_oc", cwd=str(folder))
        _, first = self.hit("POST", "/api/recruits/adopt",
                            {"session_id": "ses_oc", "engine": "opencode"})
        code, again = self.hit("POST", "/api/recruits/adopt",
                               {"session_id": "ses_oc", "engine": "opencode"})
        self.assertEqual(code, 200)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(registry.all(include_hidden=True)), 1)

    def test_adopting_an_opencode_session_whose_agent_is_hidden_shows_it_again(self):
        folder = self.dir / "opencode thing"
        folder.mkdir()
        rec = registry.create(name="known", cwd=str(folder), engine="opencode",
                              opencode_session="ses_oc")
        registry.hide(rec["id"])
        self.opencode("ses_oc", cwd=str(folder))
        code, got = self.hit("POST", "/api/recruits/adopt",
                             {"session_id": "ses_oc", "engine": "opencode"})
        self.assertEqual(code, 200)
        self.assertEqual(got["id"], "known")
        self.assertFalse(registry.get("known")["hidden"])

    def test_an_opencode_folder_that_has_gone_is_a_question_and_not_an_error(self):
        gone = str(self.dir / "opencode-moved")
        self.opencode("ses_oc", cwd=gone)
        code, got = self.hit("POST", "/api/recruits/adopt",
                             {"session_id": "ses_oc", "engine": "opencode"})
        self.assertEqual(code, 200)
        self.assertTrue(got["needs_dir"])
        self.assertEqual(got["path"], gone)
        self.assertEqual(registry.all(include_hidden=True), [])

    def test_the_typed_name_wins_over_the_one_in_the_file(self):
        folder = self.dir / "thing"
        folder.mkdir()
        self.session("-home-dev-thing", "aaa",
                     [self.titled("night shift"), self.user("hi", cwd=str(folder))])
        _, got = self.hit("POST", "/api/recruits/adopt",
                          {"session_id": "aaa", "name": "watcher"})
        self.assertEqual(got["name"], "watcher")
        self.assertEqual(got["id"], "watcher")

    def test_adopting_twice_hands_back_the_same_agent(self):
        folder = self.dir / "thing"
        folder.mkdir()
        self.session("-home-dev-thing", "aaa", [self.user("hi", cwd=str(folder))])
        _, first = self.hit("POST", "/api/recruits/adopt", {"session_id": "aaa"})
        code, again = self.hit("POST", "/api/recruits/adopt", {"session_id": "aaa"})
        self.assertEqual(code, 200)
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(registry.all(include_hidden=True)), 1)

    def test_adopting_a_session_whose_agent_is_hidden_shows_it_again(self):
        rec = registry.create(name="known", cwd=str(self.dir), session_id="aaa")
        registry.hide(rec["id"])
        self.session("-home-dev-thing", "aaa", [self.user("hi")])
        code, got = self.hit("POST", "/api/recruits/adopt", {"session_id": "aaa"})
        self.assertEqual(code, 200)
        self.assertEqual(got["id"], "known")
        self.assertFalse(registry.get("known")["hidden"])

    def test_a_folder_that_has_gone_is_a_question_and_not_an_error(self):
        gone = str(self.dir / "moved")
        self.session("-home-dev-moved", "aaa", [self.user("hi", cwd=gone)])
        code, got = self.hit("POST", "/api/recruits/adopt", {"session_id": "aaa"})
        self.assertEqual(code, 200)
        self.assertTrue(got["needs_dir"])
        self.assertEqual(got["path"], gone)
        self.assertEqual(registry.all(include_hidden=True), [])

    def test_the_same_question_is_answered_by_making_the_folder(self):
        gone = str(self.dir / "moved")
        self.session("-home-dev-moved", "aaa", [self.user("hi", cwd=gone)])
        code, got = self.hit("POST", "/api/recruits/adopt",
                             {"session_id": "aaa", "create_dir": True})
        self.assertEqual(code, 201)
        self.assertTrue(os.path.isdir(gone))
        self.assertEqual(got["cwd"], gone)

    def test_the_same_question_is_answered_by_naming_another_folder(self):
        gone = str(self.dir / "moved")
        instead = self.dir / "elsewhere"
        instead.mkdir()
        self.session("-home-dev-moved", "aaa", [self.user("hi", cwd=gone)])
        code, got = self.hit("POST", "/api/recruits/adopt",
                             {"session_id": "aaa", "cwd": str(instead)})
        self.assertEqual(code, 201)
        self.assertEqual(got["cwd"], str(instead))

    def test_a_relative_folder_is_refused(self):
        folder = self.dir / "thing"
        folder.mkdir()
        self.session("-home-dev-thing", "aaa", [self.user("hi", cwd=str(folder))])
        code, got = self.hit("POST", "/api/recruits/adopt",
                             {"session_id": "aaa", "cwd": "work/thing"})
        self.assertEqual(code, 400)
        self.assertIn("full path", got["error"])

    def test_adopt_says_404_for_a_session_that_is_not_there(self):
        self.assertEqual(self.hit("POST", "/api/recruits/adopt",
                                  {"session_id": "nope"})[0], 404)

    def test_adopt_needs_a_session_id(self):
        self.assertEqual(self.hit("POST", "/api/recruits/adopt", {})[0], 400)

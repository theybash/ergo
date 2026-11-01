"""The tools an agent is given, and the protocol they arrive over.

The server is driven the way Claude Code drives it: a line of JSON-RPC in, a line out. That is the
whole contract, so testing it needs no MCP library, which is fortunate because there is no pip.
"""

import json
import os
import subprocess
import sys

from commandcenter import broker, config, registry
from tests.harness import Case, json_route

TOOLS = str(broker.TOOLS)


def speak(messages, role="agent", agent="box", api="http://127.0.0.1:9"):
    """Send messages to a fresh tool server and read the answers back."""
    env = dict(os.environ, PLATFORM_ROLE=role, PLATFORM_AGENT=agent, PLATFORM_API=api)
    out = subprocess.run([sys.executable, TOOLS], input="".join(
        json.dumps(m) + "\n" for m in messages), capture_output=True, text=True, env=env,
        timeout=60)
    return [json.loads(line) for line in out.stdout.splitlines() if line.strip()]


def ask(name, arguments=None, **kw):
    got = speak([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments or {}}}], **kw)
    return got[0]


def payload(answer):
    return json.loads(answer["result"]["content"][0]["text"])


class Protocol(Case):
    def test_it_answers_the_handshake_with_the_version_that_was_asked_for(self):
        got = speak([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2024-11-05"}}])
        self.assertEqual(got[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", got[0]["result"]["capabilities"])

    def test_a_notification_gets_no_answer(self):
        """Replying to a notification is a protocol error: the client has nowhere to put it."""
        got = speak([{"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
        self.assertEqual([m["id"] for m in got], [2])

    def test_nonsense_does_not_kill_the_server(self):
        """This process is an agent's whole toolset; a crash takes the tools away for good."""
        got = speak([{"jsonrpc": "2.0", "id": 1, "method": "no/such/method"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
        self.assertIn("error", got[0])
        self.assertIn("tools", got[1]["result"])

    def test_a_line_that_is_not_json_is_survived(self):
        env = dict(os.environ, PLATFORM_ROLE="agent", PLATFORM_AGENT="box")
        out = subprocess.run([sys.executable, TOOLS],
                             input='not json\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n',
                             capture_output=True, text=True, env=env, timeout=60)
        answers = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
        self.assertEqual(answers[-1]["id"], 2)


class WhichTools(Case):
    def test_an_ordinary_agent_gets_the_cords_the_schedule_and_its_browser_and_nothing_else(self):
        got = speak([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
        names = [t["name"] for t in got[0]["result"]["tools"]]
        self.assertEqual(names, ["list_agents", "transfer_to", "call_agent",
                                 "list_jobs", "add_job", "remove_job", "run_job",
                                 "call_transcript", "call_owner", "rename_me", "set_profile_picture", "set_alarm", "cancel_alarm", "float_me", "calm_mode", "quiet_for", "offer_task",
                                 "task_plan", "task_done", "task_list", "shift_request", "feed_post", "owner_task_add", "task_nag", "owed_add", "owner_task_done", "task_move", "task_drop", "whatsapp_chats", "whatsapp_read", "reply_outside", "whatsapp_find", "whatsapp_follow", "whatsapp_draft", "email_draft", "show", "browser_open", "browser_snapshot", "browser_act",
                                 "browser_screenshot", "browser_login", "browser_logins",
                                 "browser_handoff", "browser_do", "make_delegate",
                                 "list_delegates", "tell_delegate", "archive_delegate",
                                 "revive_delegate"])

    def test_the_master_also_gets_the_fleet(self):
        got = speak([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], role="master")
        names = [t["name"] for t in got[0]["result"]["tools"]]
        for expected in ("wake", "sleep", "stop", "message", "read_agent", "agents_health"):
            self.assertIn(expected, names)

    def test_a_master_only_tool_is_not_there_for_an_agent(self):
        self.assertIn("no tool called wake", ask("wake", {"agent": "x"})["error"]["message"])

    def test_every_tool_describes_itself_and_its_arguments(self):
        got = speak([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], role="master")
        for tool in got[0]["result"]["tools"]:
            self.assertGreater(len(tool["description"]), 30, tool["name"])
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])


class Failing(Case):
    def test_a_tool_that_fails_says_so_as_a_result_not_a_protocol_error(self):
        """The agent should read what went wrong and try something else."""
        answer = ask("list_agents")
        self.assertTrue(answer["result"]["isError"])
        self.assertIn("not answering", answer["result"]["content"][0]["text"])

    def test_a_missing_argument_is_reported_as_a_bad_call(self):
        answer = ask("transfer_to", {"agent": "other"})
        self.assertEqual(answer["error"]["code"], -32602)

    def test_a_tool_with_no_agent_of_its_own_refuses_to_pretend(self):
        answer = ask("transfer_to", {"agent": "other", "instruction": "do it"}, agent="")
        self.assertIn("which agent it belongs to", answer["result"]["content"][0]["text"])


class Show(Case):
    def test_a_card_is_checked_by_its_kind_and_a_wrong_one_says_why(self):
        good = payload(ask("show", {"kind": "choice", "data": {
            "question": "Dinner on Friday?", "options": ["Yes", {"label": "No", "description": "busy"}]}}))
        self.assertEqual(good["shown"], "choice")
        bad = ask("show", {"kind": "choice", "data": {"question": "Dinner?", "options": ["Yes"]}})
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("options is a list of 2 to 5", bad["result"]["content"][0]["text"])

    def test_a_report_may_link_to_ergo_itself(self):
        good = payload(ask("show", {"kind": "report", "data": {
            "title": "Done", "sections": [{"head": "A", "body": "a"}, {"head": "B", "body": "b"}],
            "link": {"label": "Ergo", "url": "/"}}}))
        self.assertEqual(good["shown"], "report")


class ProfilePicture(Case):
    def test_a_wide_picture_is_cut_square_into_the_outbox(self):
        from PIL import Image
        folder = self.dir / "boxhome"
        folder.mkdir()
        Image.new("RGB", (300, 200), "red").save(folder / "me.jpg")
        srv = self.http({"/api/state": json_route({"agents": [{"id": "box", "cwd": str(folder)}]})})
        got = payload(ask("set_profile_picture", {"file": "me.jpg"}, api=srv.base))
        self.assertEqual(got["saved"], "avatar.png")
        with Image.open(folder / ".platform" / "outbox" / "box" / "avatar.png") as img:
            self.assertEqual(img.size, (256, 256))


class Against(Case):
    """The tools talking to a stand-in for the daemon, so the requests they make are checked."""

    def setUp(self):
        super().setUp()
        self.srv = self.http({
            "/api/state": json_route({"agents": [
                {"id": "box", "name": "box", "state": "green", "working": False, "role": None,
                 "cwd": "/tmp", "turns": 3, "waiting": 0, "server": {"up": True},
                 "last_message": "all done"}],
                "health": {"memory": {"available_gb": 7.5}}}),
            "/api/agents/box/wake": json_route({"ok": True, "pid": 123}),
            "/api/agents/box/message": json_route({"ok": True, "outcome": "held", "waiting": 1}),
            "/api/agents/box/messages": json_route({"messages": [
                {"who": "agent", "at": "t", "text": "the last thing"}]}),
            "/api/agents/box": json_route({"id": "box", "name": "box", "state": "green",
                                           "working": False, "cwd": "/tmp", "turns": 3,
                                           "last_message": "all done"}),
            "/api/cords": json_route({"id": "c1", "state": "completed",
                                      "messages": [{"role": "payload", "text": "carried text"}]}),
            "/api/health": json_route({"memory": {"available_gb": 7.5},
                                       "battery": {"percent": 99},
                                       "pressure": {"cpu": 0.1},
                                       "disks": [{"mount": "/", "free_gb": 90}]}),
        })

    def test_list_agents_says_who_exists_and_how_much_memory_is_left(self):
        got = payload(ask("list_agents", api=self.srv.base))
        self.assertEqual(got["agents"][0]["id"], "box")
        self.assertEqual(got["memory_free_gb"], 7.5)

    def test_waking_reaches_the_daemon(self):
        payload(ask("wake", {"agent": "box"}, role="master", api=self.srv.base))
        self.assertTrue(any(h["path"] == "/api/agents/box/wake" and h["method"] == "POST"
                            for h in self.srv.hits))

    def test_a_message_is_held_by_default_and_says_which(self):
        got = payload(ask("message", {"agent": "box", "text": "hello"}, role="master",
                          api=self.srv.base))
        self.assertEqual(got["outcome"], "held")
        sent = json.loads(next(h["body"] for h in self.srv.hits
                              if h["path"] == "/api/agents/box/message"))
        self.assertFalse(sent["wake"], "waking costs memory, so it is never the default")

    def test_reading_a_conversation_brings_back_what_was_said(self):
        got = payload(ask("read_agent", {"agent": "box"}, role="master", api=self.srv.base))
        self.assertEqual(got["messages"][0]["text"], "the last thing")
        self.assertEqual(got["agent"]["id"], "box")

    def test_a_transfer_names_itself_as_the_sender(self):
        payload(ask("transfer_to", {"agent": "other", "instruction": "use this"},
                    agent="me", api=self.srv.base))
        sent = json.loads(next(h["body"] for h in self.srv.hits if h["path"] == "/api/cords"))
        self.assertEqual(sent, {"kind": "transfer", "from": "me", "to": "other",
                                "instruction": "use this"})

    def test_a_call_passes_its_budget_only_when_given_one(self):
        payload(ask("call_agent", {"agent": "other", "instruction": "ask them"},
                    agent="me", api=self.srv.base))
        self.assertNotIn("budget", json.loads(self.srv.hits[-1]["body"]))
        payload(ask("call_agent", {"agent": "other", "instruction": "ask", "budget": 4},
                    agent="me", api=self.srv.base))
        self.assertEqual(json.loads(self.srv.hits[-1]["body"])["budget"], 4)


class Hiring(Case):
    """create_agent, which is the one tool that differs from the console on purpose."""

    def setUp(self):
        super().setUp()
        self.made = []
        outer = self

        def create(handler, body):
            outer.made.append(json.loads(body))
            sent = outer.made[-1]
            return 201, "application/json", {
                "id": "research", "name": sent["name"], "cwd": sent["cwd"], "state": "green"}

        self.srv = self.http({"/api/agents": create})

    def test_it_is_master_only(self):
        answer = ask("create_agent", {"name": "research", "folder": "/tmp/x"})
        self.assertIn("no tool called create_agent", answer["error"]["message"])

    def test_it_creates_a_folder_that_is_missing_and_says_so(self):
        folder = str(self.dir / "brand-new")
        got = payload(ask("create_agent", {"name": "research", "folder": folder},
                          role="master", api=self.srv.base))
        self.assertTrue(got["created_folder"], "a tool that refused would just mean mkdir and retry")
        self.assertIn("did not exist", got["note"])
        import os
        self.assertTrue(os.path.isdir(folder))

    def test_it_says_when_the_folder_was_already_there(self):
        got = payload(ask("create_agent", {"name": "research", "folder": str(self.dir)},
                          role="master", api=self.srv.base))
        self.assertFalse(got["created_folder"])

    def test_it_passes_the_name_and_folder_through(self):
        folder = str(self.dir / "work")
        payload(ask("create_agent", {"name": "Research Desk", "folder": folder, "model": "sonnet"},
                    role="master", api=self.srv.base))
        self.assertEqual(self.made[-1], {"name": "Research Desk", "cwd": folder,
                                         "model": "sonnet", "start": True})

    def test_a_relative_path_is_refused_rather_than_guessed(self):
        answer = ask("create_agent", {"name": "research", "folder": "somewhere"},
                     role="master", api=self.srv.base)
        self.assertIn("full path", answer["result"]["content"][0]["text"])
        self.assertEqual(self.made, [], "and nothing was created")

    def test_it_can_be_asked_not_to_start_the_agent(self):
        payload(ask("create_agent", {"name": "research", "folder": str(self.dir), "start": False},
                    role="master", api=self.srv.base))
        self.assertFalse(self.made[-1]["start"])


class Configured(Case):
    def test_an_agent_is_told_its_own_name_and_role(self):
        rec = self.agent("chief", role="master")
        path = broker.write_tools_config(rec)
        env = json.loads(path.read_text())["mcpServers"]["platform"]["env"]
        self.assertEqual(env["PLATFORM_AGENT"], "chief")
        self.assertEqual(env["PLATFORM_ROLE"], "master")

    def test_an_ordinary_agent_is_not_the_master(self):
        path = broker.write_tools_config(self.agent("box"))
        env = json.loads(path.read_text())["mcpServers"]["platform"]["env"]
        self.assertEqual(env["PLATFORM_ROLE"], "agent")

    def test_the_address_is_always_loopback(self):
        config.BIND = "0.0.0.0"
        path = broker.write_tools_config(self.agent("box"))
        env = json.loads(path.read_text())["mcpServers"]["platform"]["env"]
        self.assertTrue(env["PLATFORM_API"].startswith("http://127.0.0.1:"))

    def test_the_command_line_carries_the_config_and_still_names_the_conversation(self):
        rec = self.agent("box")
        path = broker.write_tools_config(rec)
        argv = broker.argv_for(rec, False, path)
        self.assertIn("--mcp-config", argv)
        self.assertIn(str(path), argv)
        self.assertIn("--session-id", argv)
        self.assertNotIn("--strict-mcp-config", argv,
                         "that would take away every MCP server the operator configured")

    def test_remote_control_is_off_unless_asked_for(self):
        """Every agent appearing in the operator's phone was not wanted."""
        argv = broker.argv_for(self.agent("box"), False)
        self.assertNotIn("--remote-control", argv)

    def test_when_turned_on_each_agent_registers_under_its_own_name(self):
        config.REMOTE_CONTROL = True
        rec = self.agent("box")
        argv = broker.argv_for(rec, False)
        self.assertEqual(argv[argv.index("--remote-control") + 1], "box")

    def test_the_platforms_own_tools_do_not_stop_to_ask(self):
        """Found by watching a real agent stall at a permission dialog on list_agents."""
        rec = self.agent("box")
        argv = broker.argv_for(rec, False, broker.write_tools_config(rec))
        self.assertIn("--allowedTools", argv)
        self.assertIn("mcp__platform", argv)
        self.assertIn("--permission-mode", argv, "everything else still asks")

    def test_nothing_else_is_pre_approved(self):
        rec = self.agent("box")
        argv = broker.argv_for(rec, False, broker.write_tools_config(rec))
        allowed = argv[argv.index("--allowedTools") + 1:]
        self.assertEqual([a for a in allowed if not a.startswith("--")][:1], ["mcp__platform"])

    def test_waking_rewrites_the_config_so_a_promotion_takes_effect(self):
        rec = self.agent("box")
        broker.write_tools_config(rec)
        registry.update("box", role="master")
        path = broker.write_tools_config(registry.get("box"))
        self.assertEqual(json.loads(path.read_text())["mcpServers"]["platform"]["env"]
                         ["PLATFORM_ROLE"], "master")


class Schedule(Case):
    """The schedule tools, against a stand-in for the daemon.

    The interesting half is the conversion: an agent is given minutes because that is how the work
    is described, and the daemon is given seconds because that is what it stores.
    """

    def setUp(self):
        super().setUp()

        def made(handler, body):
            asked = json.loads(body or b"{}")
            return (201, "application/json", {
                "id": "j1", "agent": asked.get("agent"), "task": asked.get("task"),
                "at": asked.get("at"), "every": asked.get("every"), "enabled": True,
                "quiet": asked.get("quiet", False), "next": 1787130000.0,
                "runs": 0, "fails": 0, "last_outcome": None})

        self.srv = self.http({
            "/api/jobs": made,
            "/api/jobs/j1": json_route({"ok": True}),
            "/api/jobs/nope": json_route({"ok": False}),
            "/api/jobs/j1/run": json_route({"ok": True, "said": "running j1 on box now"}),
        })

    def listing(self, jobs):
        self.srv.routes["/api/jobs"] = json_route({"jobs": jobs})

    def sent(self, path, method="POST"):
        for hit in self.srv.hits:
            if hit["path"] == path and hit["method"] == method:
                return json.loads(hit["body"] or b"{}")
        raise AssertionError(f"nothing was {method}ed to {path}: {self.srv.hits}")

    def test_minutes_reach_the_daemon_as_seconds(self):
        payload(ask("add_job", {"task": "look at the drafts", "every_minutes": 30},
                    api=self.srv.base))
        self.assertEqual(self.sent("/api/jobs")["every"], 1800)

    def test_a_time_of_day_is_passed_through_untouched(self):
        payload(ask("add_job", {"task": "the morning check", "at": "07:00"}, api=self.srv.base))
        body = self.sent("/api/jobs")
        self.assertEqual(body["at"], "07:00")
        self.assertNotIn("every", body)

    def test_work_is_yours_unless_you_name_someone(self):
        payload(ask("add_job", {"task": "mine", "at": "07:00"}, agent="box", api=self.srv.base))
        self.assertEqual(self.sent("/api/jobs")["agent"], "box")

    def test_you_can_schedule_work_on_another_agent(self):
        """No walls between agents, by the decision of 18 August."""
        payload(ask("add_job", {"task": "theirs", "at": "07:00", "agent": "other"},
                    agent="box", api=self.srv.base))
        self.assertEqual(self.sent("/api/jobs")["agent"], "other")

    def test_a_job_with_no_schedule_is_refused_before_the_daemon_hears_of_it(self):
        answer = ask("add_job", {"task": "when though"}, api=self.srv.base)
        self.assertTrue(answer["result"]["isError"])
        self.assertEqual(self.srv.hits, [])

    def test_both_a_time_and_a_repeat_is_refused(self):
        answer = ask("add_job", {"task": "both", "at": "07:00", "every_minutes": 30},
                     api=self.srv.base)
        self.assertTrue(answer["result"]["isError"])
        self.assertEqual(self.srv.hits, [])

    def test_a_server_that_does_not_know_its_agent_refuses_to_guess(self):
        answer = ask("add_job", {"task": "whose is this", "at": "07:00"}, agent="",
                     api=self.srv.base)
        self.assertIn("name the agent", answer["result"]["content"][0]["text"])

    def test_the_answer_says_when_it_next_runs_and_what_happens_then(self):
        got = payload(ask("add_job", {"task": "the morning check", "at": "07:00"},
                          api=self.srv.base))
        self.assertEqual(got["runs"], "every day at 07:00")
        self.assertTrue(got["next_run"])
        self.assertIn("task succeeded", got["note"])

    def test_a_repeat_is_read_back_in_words(self):
        got = payload(ask("add_job", {"task": "hourly", "every_minutes": 60}, api=self.srv.base))
        self.assertEqual(got["runs"], "every hour")

    def test_your_own_work_is_marked_in_the_list(self):
        self.listing([
            {"id": "a", "agent": "box", "task": "mine", "at": "07:00", "enabled": True,
             "next": 1787130000.0},
            {"id": "b", "agent": "other", "task": "theirs", "every": 7200, "enabled": True,
             "next": 1787130000.0}])
        got = payload(ask("list_jobs", api=self.srv.base))["jobs"]
        self.assertEqual([j["mine"] for j in got], [True, False])
        self.assertEqual(got[1]["runs"], "every 2 hours")

    def test_removing_work_that_is_not_there_says_so_rather_than_claiming_success(self):
        got = payload(ask("remove_job", {"job": "nope"}, api=self.srv.base))
        self.assertFalse(got["removed"])
        self.assertIn("no job called nope", got["note"])

    def test_running_work_now_reaches_the_daemon_and_does_not_wait(self):
        got = payload(ask("run_job", {"job": "j1"}, api=self.srv.base))
        self.assertIn("running j1", got["said"])
        self.assertIn("background", got["note"])


class RenameMe(Case):
    def test_the_ring_carries_the_name_he_gave_the_agent(self):
        from commandcenter import registry, server
        self.agent("portfolio-manager")
        registry.update("portfolio-manager", name="Juno")
        got = server._ring_named({"id": "x", "agent": "portfolio-manager", "text": "hi"})
        self.assertEqual(got["agent"], "Juno")
        self.assertEqual(got["agent_id"], "portfolio-manager", "Prompt opens this agent's chat")
        self.assertIsNone(server._ring_named(None))

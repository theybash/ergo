"""Outside agents: the owner's agents on other machines, handing work to Ergo's agents over MCP.

No agent runs. `broker.send` is replaced and what it would have sent is read back."""

import base64
import threading
import time

from commandcenter import broker, outside, registry
from tests.harness import Case


class OutsideCase(Case):
    def setUp(self):
        super().setUp()
        self.sent = []
        real = broker.send
        broker.send = lambda agent, text, wake=False: self.sent.append((agent, text, wake)) or "sent"
        self.addCleanup(lambda: setattr(broker, "send", real))
        self.assistant = self.agent("assistant")
        (self.dir / "research").mkdir(parents=True)
        self.other = registry.create(name="research", cwd=str(self.dir / "research"))
        made = outside.add("MacBook Claude")
        self.token = made.pop("token")
        self.me = outside.who(self.token)

    def call(self, tool, /, **args):
        return outside.call_tool(self.me, tool, args)

    def test_a_token_is_shown_once_and_only_its_hash_is_kept(self):
        self.assertEqual(self.me["id"], "macbook-claude")
        self.assertNotIn(self.token, (outside._file()).read_text())
        self.assertIsNone(outside.who("ergo_wrong"))
        self.assertNotIn("token", outside.listing()[0])

    def test_it_sees_every_agent_that_is_not_hidden(self):
        names = [a["id"] for a in self.call("list_agents")["agents"]]
        self.assertIn(self.assistant["id"], names)
        self.assertIn(self.other["id"], names)
        registry.update(self.other["id"], hidden=True)
        with self.assertRaises(ValueError):
            self.call("send_message", agent=self.other["id"], text="hi")

    def test_a_message_is_signed_and_says_how_to_answer(self):
        self.call("send_message", agent=self.assistant["id"], text="Find the lease PDF")
        agent, text, wake = self.sent[-1]
        self.assertEqual(agent, self.assistant["id"])
        self.assertIn("[From MacBook Claude", text)
        self.assertIn('to="macbook-claude"', text)
        self.assertTrue(wake)

    def test_files_go_in_and_come_out_but_never_outside_the_folder(self):
        got = self.call("send_file", agent=self.assistant["id"], name="notes.txt", content="hello")
        self.assertEqual(got["bytes"], 5)
        paths = [f["path"] for f in self.call("list_files", agent=self.assistant["id"])["files"]]
        self.assertTrue(any(p.endswith("notes.txt") for p in paths))
        path = next(p for p in paths if p.endswith("notes.txt"))
        self.assertEqual(self.call("get_file", agent=self.assistant["id"], path=path)["text"], "hello")
        with self.assertRaises(ValueError):
            self.call("get_file", agent=self.assistant["id"], path="../../etc/passwd")

    def test_a_binary_file_comes_back_as_base64(self):
        (self.dir / "assistant" / "pic.bin").write_bytes(b"\xff\x00\xfe")
        got = self.call("get_file", agent=self.assistant["id"], path="pic.bin")
        self.assertEqual(base64.b64decode(got["content_base64"]), b"\xff\x00\xfe")

    def test_a_handoff_answer_comes_back_through_replies(self):
        hid = self.call("handoff", agent=self.assistant["id"], task="Book the table",
                        files=[{"name": "menu.txt", "content": "dal"}])["handoff"]
        self.assertIn(f"HANDOFF {hid}", self.sent[-1][1])
        (self.dir / "assistant" / "done.txt").write_text("booked")
        outside.deliver("macbook-claude", self.assistant["id"], f"{hid} done", file="done.txt")
        got = self.call("replies", wait_seconds=0)
        self.assertEqual(got["next"], 1)
        row = got["replies"][0]
        self.assertTrue(row["text"].startswith(hid))
        self.assertEqual(row["file"]["get_file"], {"agent": self.assistant["id"], "path": "done.txt"})
        self.assertEqual(self.call("replies", after=1, wait_seconds=0)["replies"], [])

    def test_replies_waits_for_the_first_answer(self):
        def later():
            time.sleep(0.3)
            outside.deliver("macbook-claude", self.assistant["id"], "here")
        threading.Thread(target=later).start()
        start = time.time()
        got = self.call("replies", wait_seconds=5)
        self.assertEqual(got["replies"][0]["text"], "here")
        self.assertLess(time.time() - start, 3)

    def test_a_reply_file_must_be_the_senders_own(self):
        with self.assertRaises(ValueError):
            outside.deliver("macbook-claude", self.assistant["id"], "x", file="/etc/passwd")

    def test_the_protocol_lists_and_calls_tools(self):
        init = outside.rpc(self.me, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                     "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "ergo")
        names = [t["name"] for t in outside.rpc(self.me, {"jsonrpc": "2.0", "id": 2,
                                                          "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(set(names), {"list_agents", "send_message", "send_file", "list_files",
                                      "get_file", "handoff", "replies"})
        self.assertIsNone(outside.rpc(self.me, {"jsonrpc": "2.0", "method": "notifications/initialized"}))
        bad = outside.rpc(self.me, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "get_file", "arguments": {"agent": "nobody", "path": "x"}}})
        self.assertTrue(bad["result"]["isError"])

    def test_removed_means_locked_out(self):
        outside.remove("macbook-claude")
        self.assertIsNone(outside.who(self.token))


class Sessions(OutsideCase):
    def test_messages_say_who_and_which_client(self):
        me = outside.attach(self.me, None)
        outside.rpc(me, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"clientInfo": {"name": "codex-mcp-client", "version": "1"}}})
        outside.call_tool(me, "send_message", {"agent": self.assistant["id"], "text": "hi"})
        self.assertIn("[From MacBook Claude, Codex,", self.sent[-1][1])

    def test_over_http_the_session_id_comes_back(self):
        import http.client
        import json
        from http.server import ThreadingHTTPServer
        from commandcenter import server
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        token = outside.add("studio")["token"]

        def post(body, sid=None):
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            if sid:
                headers["Mcp-Session-Id"] = sid
            conn.request("POST", "/mcp", json.dumps(body), headers)
            resp = conn.getresponse()
            try:
                return resp.getheader("Mcp-Session-Id"), json.loads(resp.read() or b"null")
            finally:
                conn.close()

        sid, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"clientInfo": {"name": "claude-code"}}})
        self.assertTrue(sid)
        _, got = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "list_agents", "arguments": {}}}, sid)
        self.assertFalse(got["result"].get("isError"))
        self.assertIn(self.assistant["id"], got["result"]["content"][0]["text"])

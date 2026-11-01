"""Messages waiting for a sleeping agent. Held, never queued, and never lost."""

from commandcenter import mailbox
from tests.harness import Case


class Holding(Case):
    def test_a_held_message_survives_a_restart(self):
        mailbox.hold("box", "the first note")
        mailbox.reset()
        self.assertEqual(mailbox.waiting("box"), 1)

    def test_several_arrive_as_one_prompt_with_their_ages(self):
        mailbox.hold("box", "ALMOND")
        mailbox.hold("box", "BASALT")
        merged = mailbox.merged(mailbox.take("box"))
        self.assertIn("ALMOND", merged)
        self.assertIn("BASALT", merged)
        self.assertEqual(mailbox.waiting("box"), 0)

    def test_one_message_is_delivered_as_itself(self):
        mailbox.hold("box", "just the one")
        self.assertEqual(mailbox.merged(mailbox.take("box")).strip(), "just the one")

    def test_taking_empties_the_box(self):
        mailbox.hold("box", "a note")
        self.assertEqual(len(mailbox.take("box")), 1)
        self.assertEqual(mailbox.take("box"), [])

    def test_every_message_has_an_id_that_survives_a_restart(self):
        mailbox.hold("box", "one")
        mailbox.hold("box", "two")
        ids = [m["id"] for m in mailbox.waiting_list("box")]
        mailbox.reset()
        self.assertEqual([m["id"] for m in mailbox.waiting_list("box")], ids)

    def test_deleting_one_leaves_the_others(self):
        for word in ("one", "two", "three"):
            mailbox.hold("box", word)
        middle = mailbox.waiting_list("box")[1]["id"]
        self.assertTrue(mailbox.drop_one("box", middle))
        left = [m["text"] for m in mailbox.waiting_list("box")]
        self.assertEqual(left, ["one", "three"])

    def test_deleting_an_id_that_is_not_there_says_so(self):
        mailbox.hold("box", "one")
        self.assertFalse(mailbox.drop_one("box", "nosuchid"))
        self.assertEqual(mailbox.waiting("box"), 1)

    def test_deleting_the_lot(self):
        for word in ("one", "two"):
            mailbox.hold("box", word)
        self.assertEqual(mailbox.drop("box"), 2)
        self.assertEqual(mailbox.waiting("box"), 0)

    def test_two_agents_do_not_share_a_box(self):
        mailbox.hold("one", "for one")
        mailbox.hold("two", "for two")
        self.assertEqual(mailbox.waiting("one"), 1)
        self.assertEqual([m["text"] for m in mailbox.waiting_list("two")], ["for two"])

    def test_taking_one_to_send_leaves_the_others(self):
        for word in ("one", "two"):
            mailbox.hold("box", word)
        first = mailbox.waiting_list("box")[0]["id"]
        self.assertEqual(mailbox.take_one("box", first)["text"], "one")
        self.assertEqual([m["text"] for m in mailbox.waiting_list("box")], ["two"])
        self.assertIsNone(mailbox.take_one("box", first))


class Quiet(Case):
    """A fresh job's result waits for the agent but never shows on his card (18 Oct 2026)."""

    def test_a_quiet_message_is_not_counted_or_listed_but_still_delivered(self):
        mailbox.hold("box", "his note")
        mailbox.hold("box", "[Your scheduled job x, a fresh run: succeeded]", quiet=True)
        self.assertEqual(mailbox.waiting("box"), 1)
        self.assertEqual(mailbox.waiting("box", everything=True), 2)
        self.assertEqual([m["text"] for m in mailbox.waiting_list("box")], ["his note"])
        self.assertEqual(mailbox.counts(), {"box": 1})
        self.assertEqual(len(mailbox.take("box")), 2)


class SendingHeld(Case):
    """15 October: a message held because the terminal was busy waited for a start that never
    came, since the agent was already up. Send on the held list delivers it now."""

    def setUp(self):
        super().setUp()
        import threading
        from http.server import ThreadingHTTPServer
        from commandcenter import broker, server
        self.agent("box")
        self.sent, self.outcome = [], "sent"
        real = broker.send
        broker.send = lambda agent, text, wake=False: (self.sent.append((agent, text, wake))
                                                      or self.outcome)
        self.addCleanup(setattr, broker, "send", real)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def post(self, body):
        import http.client
        import json
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/agents/box/sendheld", json.dumps(body),
                     {"Content-Type": "application/json", "X-Forwarded-For": "100.64.0.2"})
        resp = conn.getresponse()
        try:
            return resp.status, json.loads(resp.read() or b"null")
        finally:
            conn.close()

    def test_send_delivers_it_now_and_wakes_the_agent(self):
        mailbox.hold("box", "Tell me more")
        mid = mailbox.waiting_list("box")[0]["id"]
        status, got = self.post({"id": mid})
        self.assertEqual(status, 200)
        self.assertTrue(got["ok"])
        self.assertEqual(self.sent, [("box", "Tell me more", True)])
        self.assertEqual(mailbox.waiting("box"), 0)

    def test_a_failed_send_puts_it_back(self):
        self.outcome = None
        mailbox.hold("box", "keep me")
        status, got = self.post({"id": mailbox.waiting_list("box")[0]["id"]})
        self.assertFalse(got["ok"])
        self.assertEqual([m["text"] for m in mailbox.waiting_list("box")], ["keep me"])

    def test_a_message_already_gone_says_so(self):
        status, _ = self.post({"id": "nothere"})
        self.assertEqual(status, 404)

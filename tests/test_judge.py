"""The referee: Jev reads a line against the card, or reads the voice's line for a claim.

TypeSafe is never called. `memory.post_jev` is one module function, the judge looks it up on the
module each call, and a test replaces it and reads the request that would have gone out.

The point of every failure test is the same: a fault answers the safe way, "unrelated" for a reply
and "not claimed" for a claim, so the browser never confirms or corrects anything on a fault.
"""

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

from commandcenter import config, judge, memory, registry, server, voice
from tests.harness import Case

KEY = "ts-not-a-real-key-0123456789"


class JudgeCase(Case):
    def setUp(self):
        super().setUp()
        registry.create(name="acme", cwd=str(self.dir))
        config.TYPESAFE_KEY = KEY
        self.sent = []
        self.answer = None
        real = memory.post_jev

        def fake(payload, key, timeout=None):
            self.sent.append({"payload": payload, "key": key, "timeout": timeout})
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer if self.answer is not None else {"answers": {}}

        memory.post_jev = fake
        self.addCleanup(lambda: setattr(memory, "post_jev", real))
        self.made = voice.propose("acme", "send the deck to Arjun")

    def choice(self, pick, confidence=0.93):
        self.answer = {"answers": {"reply": {"type": "choice", "choice": pick,
                                             "probabilities": {pick: 0.95},
                                             "confidence": confidence}}}

    def noul(self, p):
        self.answer = {"answers": {"claim": {"type": "noul", "noul": p}}}


class Reply(JudgeCase):
    def test_each_decision_maps_through(self):
        for pick in judge.DECISIONS:
            self.choice(pick, 0.91)
            got = judge.reply(self.made["id"], "Yeah")
            self.assertEqual(got, {"decision": pick, "confidence": 0.91})

    def test_the_request_is_one_choice_with_the_proposal_in_the_state(self):
        self.choice("confirmed")
        judge.reply(self.made["id"], "yes but add initech")
        self.assertEqual(len(self.sent), 1)
        payload = self.sent[0]["payload"]
        self.assertEqual(payload["model"], memory.JEV_MODEL)
        self.assertEqual(list(payload["questions"]), ["reply"])
        q = payload["questions"]["reply"]
        self.assertEqual(q["type"], "choice")
        self.assertEqual(set(q["criteria"]), {"confirmed", "modified", "cancelled", "unrelated"})
        for option in q["criteria"].values():
            self.assertTrue(option["meaning"])
            self.assertTrue(option["examples"])
        # The owner's own phrasings are the examples.
        self.assertIn("yes but add initech", q["criteria"]["modified"]["examples"])
        self.assertIn("Yeah", q["criteria"]["confirmed"]["examples"])
        state = payload["state"]
        self.assertEqual(state["owner_said"], "yes but add initech")
        self.assertEqual(state["proposal"]["readback"], self.made["readback"])
        self.assertEqual(state["proposal"]["task"], "send the deck to Arjun")
        self.assertEqual(state["proposal"]["agent"], "acme")
        # The key goes to the transport and nowhere in the body.
        self.assertEqual(self.sent[0]["key"], KEY)
        self.assertNotIn(KEY, json.dumps(payload))
        self.assertLessEqual(self.sent[0]["timeout"], judge.TIMEOUT)

    def test_a_stop_proposal_carries_its_readback(self):
        stop = voice.propose("acme", action="stop")
        self.choice("confirmed")
        judge.reply(stop["id"], "yes")
        state = self.sent[-1]["payload"]["state"]["proposal"]
        self.assertEqual(state["action"], "stop")
        self.assertEqual(state["readback"], stop["readback"])

    def test_judging_does_not_use_up_the_proposal(self):
        self.choice("confirmed")
        judge.reply(self.made["id"], "Yeah")
        self.assertIsNotNone(voice.pending_proposal(self.made["id"]))

    def test_an_unknown_proposal_is_unrelated_and_asks_nothing(self):
        got = judge.reply("nope", "Yeah")
        self.assertEqual(got["decision"], "unrelated")
        self.assertEqual(self.sent, [])

    def test_a_confirmed_proposal_is_gone_and_so_is_the_judgement(self):
        voice.cancel(self.made["id"])
        self.assertEqual(judge.reply(self.made["id"], "Yeah")["decision"], "unrelated")
        self.assertEqual(self.sent, [])

    def test_no_key_is_unrelated_and_asks_nothing(self):
        config.TYPESAFE_KEY = ""
        got = judge.reply(self.made["id"], "Yeah")
        self.assertEqual(got, {"decision": "unrelated", "confidence": 0.0})
        self.assertEqual(self.sent, [])

    def test_a_jev_failure_is_unrelated(self):
        self.answer = RuntimeError("TypeSafe could not be reached: timed out")
        self.assertEqual(judge.reply(self.made["id"], "Yeah")["decision"], "unrelated")

    def test_an_answer_that_is_not_an_option_is_unrelated(self):
        self.choice("maybe")
        self.assertEqual(judge.reply(self.made["id"], "Yeah")["decision"], "unrelated")
        self.answer = {"nothing": True}
        self.assertEqual(judge.reply(self.made["id"], "Yeah")["decision"], "unrelated")

    def test_an_empty_line_asks_nothing(self):
        self.assertEqual(judge.reply(self.made["id"], "   ")["decision"], "unrelated")
        self.assertEqual(self.sent, [])


class Claim(JudgeCase):
    def test_the_claim_is_one_noul_on_her_line(self):
        self.noul(0.97)
        got = judge.claim("I did, Acme has it.")
        self.assertEqual(got, {"claimed": True, "p": 0.97})
        payload = self.sent[0]["payload"]
        self.assertEqual(list(payload["questions"]), ["claim"])
        q = payload["questions"]["claim"]
        self.assertEqual(q["type"], "noul")
        self.assertEqual(set(q["criteria"]), {"true", "false"})
        self.assertEqual(payload["state"], {"desk_said": "I did, Acme has it."})

    def test_a_low_p_is_not_claimed(self):
        self.noul(0.12)
        self.assertEqual(judge.claim("Shall I send it?"), {"claimed": False, "p": 0.12})

    def test_no_key_or_a_failure_is_not_claimed(self):
        config.TYPESAFE_KEY = ""
        self.assertEqual(judge.claim("Sent."), {"claimed": False, "p": 0.0})
        self.assertEqual(self.sent, [])
        config.TYPESAFE_KEY = KEY
        self.answer = RuntimeError("TypeSafe refused the request (529): overloaded")
        self.assertEqual(judge.claim("Sent."), {"claimed": False, "p": 0.0})
        self.answer = {"answers": {"claim": {"noul": True}}}
        self.assertFalse(judge.claim("Sent.")["claimed"])


class Logging(JudgeCase):
    def test_one_line_per_judgement_with_the_line_cut_to_40(self):
        self.choice("confirmed", 0.88)
        long = "yes and please also make sure the whole team at initech gets it"
        with self.assertLogs("cc.judge", level="INFO") as seen:
            judge.reply(self.made["id"], long)
        self.assertEqual(len(seen.output), 1)
        line = seen.output[0]
        self.assertIn("reply", line)
        self.assertIn("confirmed 0.88", line)
        self.assertIn(" ms", line)
        self.assertNotIn(long, line)
        self.assertIn(long[:30], line)
        self.assertNotIn("initech gets it", line)

    def test_the_key_never_reaches_the_log(self):
        self.answer = RuntimeError(f"TypeSafe refused the request (401): bad key {KEY}")
        with self.assertLogs("cc.judge", level="INFO") as seen:
            judge.reply(self.made["id"], "Yeah")
            judge.claim("Sent.")
        self.assertEqual(len(seen.output), 2)
        for line in seen.output:
            self.assertNotIn(KEY, line)


class Route(JudgeCase):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def hit(self, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/api/voice/judge", body=json.dumps(body),
                     headers={"Content-Type": "application/json"})
        reply = conn.getresponse()
        got = json.loads(reply.read() or b"{}")
        conn.close()
        return reply.status, got

    def test_the_route_judges_a_reply(self):
        self.choice("modified", 0.9)
        code, got = self.hit({"kind": "reply", "proposal_id": self.made["id"],
                              "line": "yes but add initech"})
        self.assertEqual(code, 200)
        self.assertEqual(got, {"decision": "modified", "confidence": 0.9})

    def test_the_route_judges_a_claim(self):
        self.noul(0.9)
        code, got = self.hit({"kind": "claim", "line": "Sent."})
        self.assertEqual(code, 200)
        self.assertEqual(got, {"claimed": True, "p": 0.9})

    def test_an_unknown_kind_is_400(self):
        code, got = self.hit({"kind": "guess", "line": "Sent."})
        self.assertEqual(code, 400)
        self.assertIn("kind", got["error"])

    def test_the_route_never_shows_the_key(self):
        self.answer = RuntimeError(f"bad {KEY}")
        code, got = self.hit({"kind": "reply", "proposal_id": self.made["id"], "line": "Yeah"})
        self.assertEqual(code, 200)
        self.assertEqual(got["decision"], "unrelated")
        self.assertNotIn(KEY, json.dumps(got))

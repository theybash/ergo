"""GET /api/usage: the plan, voice minutes and each agent's spend."""

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from tests.harness import NINE_AM, Case

from commandcenter import clock, config, server, usage, voice


def cost_state(total, out=0, model="claude-opus-5", start=1788433551082):
    return {"type": "cost-state", "sessionId": "s", "totalCostUSD": total, "startTime": start,
            "modelUsage": {model: {"inputTokens": 2, "outputTokens": out,
                                   "cacheReadInputTokens": 10, "cacheCreationInputTokens": 5,
                                   "costUSD": total}}}


def assistant(model):
    return {"type": "assistant", "timestamp": "2026-08-20T10:00:00Z",
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "hi"}]}}


def reply(mid, model, inp=0, read=0, out=0, hour=0):
    return {"type": "assistant", "timestamp": "2026-08-20T10:00:00Z",
            "message": {"id": mid, "role": "assistant", "model": model,
                        "content": [{"type": "text", "text": "hi"}],
                        "usage": {"input_tokens": inp, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": hour, "output_tokens": out,
                                  "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                     "ephemeral_1h_input_tokens": hour}}}}


class Agents(Case):
    def test_the_last_cost_state_wins(self):
        rec = self.agent("box")
        self.transcript_for(rec, [cost_state(1.0, 10), assistant("claude-sonnet-5"),
                                  cost_state(2.5, 40), assistant("claude-opus-5")])
        row = usage.agents()[0]
        self.assertEqual(row["cost_usd"], 2.5)
        self.assertEqual(row["tokens_out"], 40)
        self.assertEqual(row["model"], "claude-opus-5")
        self.assertAlmostEqual(row["session_started"], 1788433551.082)

    def test_output_tokens_add_up_across_models(self):
        rec = self.agent("box")
        row = cost_state(3.0, 10)
        row["modelUsage"]["claude-haiku-5"] = {"outputTokens": 5, "costUSD": 0.1}
        self.transcript_for(rec, [row, assistant("claude-opus-5")])
        self.assertEqual(usage.agents()[0]["tokens_out"], 15)

    def test_replies_after_the_last_cost_state_are_added(self):
        # Claude Code writes cost-state rows only now and then. The replies after the last one
        # carry their own token counts, and they are priced and added on top.
        rec = self.agent("box")
        self.transcript_for(rec, [cost_state(2.0, 40),
                                  reply("m1", "claude-opus-5-5", inp=1000, read=1_000_000, out=500),
                                  reply("m2", "claude-opus-5-5", out=100, hour=10_000)])
        row = usage.agents()[0]
        # m1: 1000*4 + 1M*0.20 + 500*20 = 214000 / 1e6. m2: 100*20 + 10000*4*2 = 82000 / 1e6.
        self.assertEqual(row["cost_usd"], round(2.0 + 0.214 + 0.082, 2))
        self.assertEqual(row["tokens_out"], 40 + 500 + 100)
        # Input: the cost-state's 2 + 10 + 5, then 1000 new, a million read, 10,000 written.
        self.assertEqual(row["tokens_in"], 17 + 1000 + 1_000_000 + 10_000)

    def test_one_reply_written_as_several_rows_counts_once(self):
        rec = self.agent("box")
        self.transcript_for(rec, [cost_state(1.0, 0),
                                  reply("m1", "claude-opus-5-5", out=1000),
                                  reply("m1", "claude-opus-5-5", out=1000)])
        self.assertEqual(usage.agents()[0]["tokens_out"], 1000)

    def test_replies_before_the_cost_state_are_already_in_it(self):
        rec = self.agent("box")
        self.transcript_for(rec, [reply("m0", "claude-opus-5-5", out=9999), cost_state(1.0, 7)])
        row = usage.agents()[0]
        self.assertEqual(row["cost_usd"], 1.0)
        self.assertEqual(row["tokens_out"], 7)

    def test_a_growing_file_adds_only_its_new_replies(self):
        rec = self.agent("box")
        path = self.transcript_for(rec, [cost_state(1.0, 0), reply("m1", "claude-opus-5-5", out=10)])
        self.assertEqual(usage.agents()[0]["tokens_out"], 10)
        with open(path, "a") as fh:
            fh.write(json.dumps(reply("m2", "claude-opus-5-5", out=5)) + "\n")
        usage._list["agents"] = None  # past the minute the list is kept
        self.assertEqual(usage.agents()[0]["tokens_out"], 15)

    def test_an_unknown_model_adds_tokens_but_no_dollars(self):
        rec = self.agent("box")
        self.transcript_for(rec, [cost_state(1.0, 0), reply("m1", "some-other-model", out=50, inp=10**6)])
        row = usage.agents()[0]
        self.assertEqual(row["cost_usd"], 1.0)
        self.assertEqual(row["tokens_out"], 50)

    def test_a_cost_state_with_no_reply_is_not_counted(self):
        # A copied conversation was deleted, and Claude Code wrote its settings rows back with the
        # copy's cost-state. The file holds no reply, so the agent spent nothing here.
        rec = self.agent("box")
        self.transcript_for(rec, [{"type": "custom-title", "customTitle": "box"},
                                  cost_state(1213.76, 5000)])
        row = usage.agents()[0]
        self.assertIsNone(row["cost_usd"])
        self.assertIsNone(row["tokens_in"])

    def test_a_dated_model_name_is_priced(self):
        self.assertAlmostEqual(usage.price("claude-haiku-4-5-20251001", {"output_tokens": 1_000_000}), 5.0)

    def test_a_missing_file_gives_nulls(self):
        self.agent("box")
        row = usage.agents()[0]
        self.assertEqual(row["name"], "box")
        self.assertIsNone(row["cost_usd"])
        self.assertIsNone(row["tokens_out"])
        self.assertIsNone(row["model"])

    def test_no_cost_state_keeps_the_model(self):
        rec = self.agent("box")
        self.transcript_for(rec, [assistant("claude-opus-5"), self.said("done")])
        row = usage.agents()[0]
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["model"], "claude-opus-5")

    def test_a_synthetic_message_is_not_the_model(self):
        rec = self.agent("box")
        self.transcript_for(rec, [assistant("claude-opus-5"), assistant("<synthetic>")])
        self.assertEqual(usage.agents()[0]["model"], "claude-opus-5")

    def test_a_big_file_is_read_from_the_end(self):
        rec = self.agent("box")
        path = self.transcript_for(rec, [cost_state(1.0, 1)])
        filler = json.dumps(self.said("x" * 2000)) + "\n"
        with open(path, "a") as fh:
            # About three megabytes, more than ten blocks, then the row that should win, then a
            # line longer than one block so the boundary handling is exercised too.
            fh.write(filler * 1500)
            fh.write(json.dumps(cost_state(9.0, 90)) + "\n")
            fh.write(json.dumps(self.said("y" * (usage.BLOCK + 10))) + "\n")
            fh.write(json.dumps(assistant("claude-opus-5")) + "\n")
        cost, model = usage.scan(path)
        self.assertEqual(cost["totalCostUSD"], 9.0)
        self.assertEqual(model, "claude-opus-5")

    def test_the_scan_stops_before_the_top(self):
        rec = self.agent("box")
        path = self.transcript_for(rec, [{"type": "cost-state", "broken": True}])
        with open(path, "a") as fh:
            fh.write((json.dumps(self.said("x" * 2000)) + "\n") * 1500)
            fh.write(json.dumps(assistant("m")) + "\n" + json.dumps(cost_state(4.0)) + "\n")
        reads = []
        real = open

        class Counting:
            def __init__(self, fh):
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.fh.close()

            def seek(self, *a):
                return self.fh.seek(*a)

            def tell(self):
                return self.fh.tell()

            def read(self, n):
                reads.append(n)
                return self.fh.read(n)

        usage.open = lambda p, mode: Counting(real(p, mode))
        self.addCleanup(lambda: delattr(usage, "open"))
        cost, _ = usage.scan(path)
        self.assertEqual(cost["totalCostUSD"], 4.0)
        self.assertEqual(len(reads), 1)

    def test_most_spent_first_and_no_record_last(self):
        a, b, c = self.agent("a"), self.agent("b"), self.agent("c")
        self.transcript_for(a, [assistant("claude-opus-5"), cost_state(1.0)])
        self.transcript_for(c, [assistant("claude-opus-5"), cost_state(5.0)])
        self.assertEqual([r["name"] for r in usage.agents()], ["c", "a", "b"])

    def test_the_list_is_kept_for_a_minute(self):
        rec = self.agent("box")
        path = self.transcript_for(rec, [assistant("claude-opus-5"), cost_state(1.0)])
        self.assertEqual(usage.agents()[0]["cost_usd"], 1.0)
        with open(path, "a") as fh:
            fh.write(json.dumps(cost_state(2.0)) + "\n")
        self.assertEqual(usage.agents()[0]["cost_usd"], 1.0)
        clock.advance(usage.CACHE_SECONDS + 1)
        self.assertEqual(usage.agents()[0]["cost_usd"], 2.0)


class Budgets(Case):
    def test_voice_counts_today(self):
        voice.spend(600, 1)
        got = usage.snapshot()["voice"]
        self.assertEqual(got, {"used_minutes": 10.0, "cap_minutes": config.VOICE_DAILY_MINUTES})

    def test_voice_from_yesterday_is_nought_today(self):
        voice.spend(600, 1)
        clock.advance(60 * 60 * 26)
        self.assertEqual(usage.snapshot()["voice"]["used_minutes"], 0.0)

    def write(self, text):
        config.VAR.mkdir(parents=True, exist_ok=True)
        (config.VAR / "plan-usage.json").write_text(text)

    def test_the_file_is_read(self):
        self.write(json.dumps({"at": NINE_AM - 60,
                               "five_hour": {"used_percentage": 37, "resets_at": NINE_AM + 3600},
                               "seven_day": {"used_percentage": 41.5, "resets_at": None}}))
        got = usage.plan()
        self.assertEqual(got["five_hour"], {"used_percentage": 37, "resets_at": NINE_AM + 3600})
        self.assertEqual(got["seven_day"], {"used_percentage": 41.5, "resets_at": None})
        self.assertEqual(got["at"], NINE_AM - 60)
        self.assertFalse(got["stale"])

    def test_the_file_lives_under_var(self):
        self.assertTrue(str(config.VAR).startswith(str(self.dir)))

    def test_a_missing_file_is_null(self):
        self.assertIsNone(usage.plan())
        self.assertIsNone(usage.snapshot()["plan"])

    def test_broken_json_is_null(self):
        self.write("{not json")
        self.assertIsNone(usage.plan())
        self.write("[1, 2]")
        self.assertIsNone(usage.plan())

    def test_an_old_file_is_stale(self):
        self.write(json.dumps({"at": NINE_AM - usage.PLAN_STALE_SECONDS - 1,
                               "five_hour": {"used_percentage": 5, "resets_at": None}}))
        got = usage.plan()
        self.assertTrue(got["stale"])
        self.assertIsNone(got["seven_day"])

    def test_the_limits_are_asked_live_and_kept_when_the_ask_fails(self):
        """30 October 2026: "yours is always stale". Claude and Codex are asked as claude.ai and
        Codex ask, at most once a minute; a failed ask keeps the last answer, stale once old."""
        from commandcenter import live_usage
        config.CLAUDE_CREDS.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "t", "expiresAt": (NINE_AM + 3600) * 1000}}))
        config.CODEX_HOME.mkdir(parents=True, exist_ok=True)
        (config.CODEX_HOME / "auth.json").write_text(json.dumps({"tokens": {"access_token": "c"}}))
        asked = []

        def fake(url, headers):
            asked.append(url)
            if "anthropic" in url:
                return {"five_hour": {"utilization": 12.0, "resets_at": "2026-10-30T13:20:00+00:00"},
                        "seven_day": {"utilization": 48.0, "resets_at": None}}
            return {"rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 604800,
                                                      "reset_at": NINE_AM + 9999}, "secondary_window": None}}
        real = live_usage._get
        live_usage._get = fake
        self.addCleanup(setattr, live_usage, "_get", real)
        got = usage.snapshot()
        self.assertEqual(got["plan"]["five_hour"]["used_percentage"], 12.0)
        self.assertEqual(got["codex"]["seven_day"], {"used_percentage": 1.0, "resets_at": NINE_AM + 9999})
        self.assertIsNone(got["codex"]["five_hour"], "a business plan has only the week")
        usage.snapshot()
        self.assertEqual(len(asked), 2, "a second look within the minute asks nothing")

        def down(url, headers):
            raise OSError("down")
        live_usage._get = down
        clock.advance(live_usage.STALE_SECONDS + 1)
        got = usage.snapshot()
        self.assertEqual(got["codex"]["seven_day"]["used_percentage"], 1.0)
        self.assertTrue(got["codex"]["stale"])


class Route(Case):
    def setUp(self):
        super().setUp()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        self.port = httpd.server_address[1]

    def test_the_shape(self):
        rec = self.agent("box")
        self.transcript_for(rec, [assistant("claude-opus-5"), cost_state(1.25, 300)])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/usage")
        reply = conn.getresponse()
        got = json.loads(reply.read())
        conn.close()
        self.assertEqual(reply.status, 200)
        self.assertEqual(set(got), {"plan", "codex", "voice", "openai", "agents"})
        self.assertIsNone(got["plan"])
        self.assertEqual(set(got["voice"]), {"used_minutes", "cap_minutes"})
        self.assertEqual(got["agents"], [{"id": rec["id"], "name": "box", "cost_usd": 1.25,
                                          "tokens_out": 300, "tokens_in": 17, "model": "claude-opus-5",
                                          "session_started": 1788433551.082}])


if __name__ == "__main__":
    unittest.main()

"""Fresh runs: a scheduled job in a new, short claude -p session, and what the scheduler keeps.

The claude here is a fake, a few lines of Python that print the same stream-json events. It
writes down what it was given, so a test can check the command line, the folder and the prompt.
"""

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

from commandcenter import config, cron, fresh
from tests.harness import Case

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
prompt = sys.stdin.read()
with open(os.environ["FAKE_SAW"], "w") as fh:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt}, fh)
mode = os.environ.get("FAKE_MODE", "ok")
if mode == "hang":
    time.sleep(60)
if mode == "crash":
    sys.stderr.write("boom: no credentials\n")
    sys.exit(3)
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "checked it\ntask succeeded", "num_turns": 2,
                  "total_cost_usd": 0.0042,
                  "usage": {"input_tokens": 12, "cache_creation_input_tokens": 3000,
                            "cache_read_input_tokens": 9000, "output_tokens": 80}}), flush=True)
'''


class Runner(Case):
    def setUp(self):
        super().setUp()
        bin_path = self.dir / "claude"
        bin_path.write_text(FAKE)
        bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC)
        config.CLAUDE_BIN = str(bin_path)
        self.saw = self.dir / "saw.json"
        for name, value in (("FAKE_SAW", str(self.saw)), ("FAKE_MODE", "ok")):
            old = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(lambda n=name, o=old: os.environ.pop(n, None) if o is None
                            else os.environ.__setitem__(n, o))
        self.rec = self.agent("box", permission_mode="acceptEdits")

    def job(self, **kw):
        return cron.add("box", "read the site", at="07:00", **kw)

    def test_a_fresh_run_reads_the_result_and_keeps_the_stream(self):
        got = fresh.run(self.job(), "the prompt", timeout=20)
        self.assertEqual(got["reply"], "checked it\ntask succeeded")
        self.assertEqual(got["cost_usd"], 0.0042)
        self.assertEqual(got["tokens"], {"in": 12, "cache_write": 3000, "cache_read": 9000,
                                         "out": 80})
        lines = Path(got["file"]).read_text().splitlines()
        self.assertEqual(json.loads(lines[-1])["type"], "result")
        self.assertTrue(got["file"].startswith(str(config.VAR / "job-runs")))

    def test_it_runs_in_the_agents_folder_with_its_tools_and_never_asks(self):
        from commandcenter import broker
        written, real = [], broker.write_tools_config
        def keep(rec, run=None):
            path = real(rec, run)
            written.append(json.loads(path.read_text()))
            return path
        with mock.patch.object(broker, "write_tools_config", keep):
            got = fresh.run(self.job(model="sonnet"), "the prompt", timeout=20)
        saw = json.loads(self.saw.read_text())
        argv = saw["argv"]
        self.assertEqual(os.path.realpath(saw["cwd"]), os.path.realpath(self.rec["cwd"]))
        self.assertEqual(saw["prompt"], "the prompt")
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("--max-budget-usd", argv, "no dollar cap unless one is set")
        env = written[0]["mcpServers"]["platform"]["env"]
        self.assertEqual(env["PLATFORM_AGENT"], "box",
                         "the run's tools know which agent it is")
        self.assertEqual(env["PLATFORM_TRANSCRIPT"], got["file"], "a post names the run")
        self.assertFalse(Path(argv[argv.index("--mcp-config") + 1]).exists(), "removed after")
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--session-id", argv)

    def test_a_watch_run_may_read_the_watched_folder_and_only_read_it(self):
        house = os.path.realpath(self.dir) + "/ergo"
        with mock.patch.object(config, "HOME", house):
            argv = fresh.argv_for(self.rec, None, "tools.json",
                                  {"watch": house + "/inbox/*.txt"})
            platform = fresh.read_rules(self.rec, {"watch": "/home/user/platform/var/*.env"})
        tools = argv[argv.index("--allowedTools") + 1:argv.index("--permission-mode")]
        self.assertEqual(tools, ["mcp__platform", f"Read(/{house}/inbox)",
                                 f"Read(/{house}/inbox/**)"])
        self.assertNotIn("--add-dir", argv, "an added folder would take acceptEdits too")
        self.assertEqual(platform, [], "the platform var folder holds the keys")

    def test_a_run_that_hangs_is_killed_at_its_timeout(self):
        os.environ["FAKE_MODE"] = "hang"
        began = time.time()
        got = fresh.run(self.job(), "x", timeout=0.3)
        self.assertLess(time.time() - began, 15)
        self.assertIsNone(got["reply"])
        self.assertIn("timed out", got["error"])

    def test_a_crash_says_why(self):
        os.environ["FAKE_MODE"] = "crash"
        got = fresh.run(self.job(), "x", timeout=20)
        self.assertIsNone(got["reply"])
        self.assertIn("exited 3", got["error"])
        self.assertIn("no credentials", got["error"])

    def test_the_scheduler_records_cost_and_does_not_wake_the_agent(self):
        fleet = self.fleet()
        # The fleet fake stands in for fresh.run. Put the real one back, so the fake claude runs;
        # the fleet's own cleanup restores it after.
        fresh.run = Runner._real_run
        made = self.job()
        cron._run(made["id"])
        job = cron.get(made["id"])
        self.assertEqual(job["last_outcome"], "succeeded")
        self.assertEqual(job["last_cost"], 0.0042)
        self.assertEqual(fleet.woken, [], "a fresh run never wakes the main conversation")
        row = json.loads(config.JOB_LOG.read_text().splitlines()[-1])
        self.assertEqual(row["kind"], "fresh")
        self.assertEqual(row["model"], "claude-haiku-5-5")
        self.assertEqual(row["cost_usd"], 0.0042)
        prompt = json.loads(self.saw.read_text())["prompt"]
        self.assertIn("not your main conversation", prompt)
        self.assertIn("read the site", prompt)


    def test_the_whole_reply_waits_for_the_agent_and_does_not_wake_it(self):
        """The owner, 17 Oct 2026: the agent gets what its job found, in full, at no extra wake."""
        from commandcenter import mailbox
        fleet = self.fleet()
        fresh.run = Runner._real_run
        made = self.job()
        cron._run(made["id"])
        self.assertEqual(fleet.woken, [])
        held = mailbox.take("box")
        self.assertEqual(len(held), 1)
        self.assertIn(made["id"], held[0]["text"])
        self.assertIn("read the site", held[0]["text"])
        self.assertIn("checked it\ntask succeeded", held[0]["text"])


Runner._real_run = staticmethod(fresh.run)


class Kinds(Case):
    def setUp(self):
        super().setUp()
        self.agent("box")

    def test_a_repeating_job_is_fresh_on_haiku_by_default(self):
        job = cron.add("box", "a thing", at="07:00")
        self.assertEqual((job["kind"], job["model"]), ("fresh", "claude-haiku-5-5"))

    def test_a_one_off_is_continuity_by_default(self):
        job = cron.add("box", "has he answered?", in_minutes=2)
        self.assertEqual((job["kind"], job["model"]), ("continuity", None))

    def test_a_repeating_continuity_job_needs_a_reason(self):
        with self.assertRaises(ValueError):
            cron.add("box", "a thing", at="07:00", kind="continuity")
        job = cron.add("box", "a thing", at="07:00", kind="continuity",
                       reason="reads the day's meeting notes from the chat")
        self.assertEqual(job["kind"], "continuity")

    def test_an_old_job_without_a_kind_stays_continuity(self):
        self.assertEqual(cron._blank()["kind"], "continuity")

    def test_a_fresh_job_runs_through_the_fresh_runner(self):
        fleet = self.fleet()
        made = cron.add("box", "a thing", at="07:00")
        cron._run(made["id"])
        self.assertEqual(fleet.fresh, ["box"])
        self.assertEqual(fleet.woken, [])

    def test_kind_and_model_can_be_changed(self):
        made = cron.add("box", "a thing", at="07:00")
        job = cron.update(made["id"], model="opus")
        self.assertEqual(job["model"], "claude-opus-5-5")
        with self.assertRaises(ValueError):
            cron.update(made["id"], kind="continuity")
        job = cron.update(made["id"], kind="continuity", reason="reads the chat")
        self.assertEqual((job["kind"], job["model"]), ("continuity", None))

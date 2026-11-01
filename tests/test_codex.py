"""The Codex engine: its events, turned into the frames the console already reads."""

from pathlib import Path

from commandcenter import codex
from tests.harness import Case

# One real turn of `codex exec --json`, recorded on this machine on 19 October 2026. Codex was
# asked to run ls in a folder holding a.txt and to say what it found.
RECORDED = Path(__file__).resolve().parent / "codex_shell.jsonl"


class Frames(Case):
    def test_a_recorded_turn_becomes_the_chat_frames(self):
        out, threads = [], []
        stream = codex.CodexStream(out.append, on_thread=threads.append)
        for line in RECORDED.read_text().splitlines():
            stream.feed(line)
        self.assertEqual(threads, ["01a1507d-9ff6-7fa3-b848-15e5bc59c598"])
        self.assertEqual(out, [
            {"t": "state", "state": "thinking"},
            {"t": "live", "phase": "start"},
            {"t": "live", "phase": "delta", "text": "I’ll list the folder’s contents."},
            {"t": "tool", "name": "Bash", "summary": "ls"},
            {"t": "live", "phase": "start"},
            {"t": "live", "phase": "delta", "text": "The folder contains one file: `a.txt`."},
            {"t": "state", "state": "idle"},
            {"t": "live", "phase": "end"},
            {"t": "settled"},
        ])


class Env(Case):
    def test_a_codex_run_never_sees_the_openai_key(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test", "CODEX_API_KEY": "x"}):
            env = codex.run_env("a1")
            argv = codex.terminal_argv({"id": "a1", "cwd": "/tmp"})
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(env["PLATFORM_AGENT_ID"], "a1")
        self.assertIn("--unset=OPENAI_API_KEY", argv)


class Instructions(Case):
    def test_a_codex_agent_reads_the_claude_md_chain(self):
        # Codex reads AGENTS.md; the houses keep CLAUDE.md (27 October 2026).
        flags = codex.common_flags({"id": "x", "permission_mode": "plan"}, "never")
        self.assertIn('project_doc_fallback_filenames=["CLAUDE.md"]', flags)
        self.assertIn(f'project_root_markers=["{codex.ROOT_MARK}"]', flags)

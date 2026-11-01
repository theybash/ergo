"""When he last read each chat, so unread agents can be marked."""

from commandcenter import clock, reads, registry
from tests import test_voice


class Reads(test_voice.Routes):
    def test_an_agent_never_opened_counts_as_read(self):
        self.agent("scout")
        code, got = self.hit("GET", "/api/agents/scout")
        self.assertIsNone(got.get("read_at"))

    def test_opening_a_chat_marks_it_read(self):
        self.agent("scout")
        code, got = self.hit("POST", "/api/agents/scout/read", {})
        self.assertEqual(code, 200)
        self.assertEqual(got["read_at"], clock.now())
        self.assertEqual(reads.read_at("scout"), clock.now())
        code, got = self.hit("GET", "/api/agents/scout")
        self.assertEqual(got["read_at"], clock.now())

    def test_an_unknown_agent_is_refused(self):
        code, _ = self.hit("POST", "/api/agents/nobody/read", {})
        self.assertEqual(code, 404)

"""Night shift requests: an agent adds a block to the shift's requests.md."""

from commandcenter import config
from tests import test_voice


class ShiftRequest(test_voice.Routes):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, config, "SHIFT_REQUESTS", config.SHIFT_REQUESTS)
        config.SHIFT_REQUESTS = self.dir / "night-shift" / "requests.md"

    def test_a_request_is_one_block_in_the_files_own_format(self):
        rec = self.agent("box")
        code, got = self.hit("POST", "/api/shift/request", {
            "agent": rec["id"], "title": "Read PDFs", "what": "a pdf tool", "why": "it cannot",
            "urgency": "urgent"})
        self.assertEqual(code, 200)
        text = config.SHIFT_REQUESTS.read_text()
        self.assertIn("## Read PDFs\n- from: box\n", text)
        self.assertIn("- urgency: urgent\n- what: a pdf tool\n- why: it cannot\n- status: open", text)

    def test_his_own_request_says_so(self):
        rec = self.agent("box")
        self.hit("POST", "/api/shift/request", {"agent": rec["id"], "title": "T", "what": "w",
                                                "why": "y", "requested_by_owner": True})
        self.assertIn("- from: owner, claimed by box", config.SHIFT_REQUESTS.read_text())

    def test_missing_fields_and_unknown_agents_are_refused(self):
        rec = self.agent("box")
        self.assertEqual(self.hit("POST", "/api/shift/request",
                                  {"agent": rec["id"], "title": "T"})[0], 400)
        self.assertEqual(self.hit("POST", "/api/shift/request",
                                  {"agent": "nobody", "title": "T", "what": "w", "why": "y"})[0], 404)

"""A call made in a view keeps to it.

The console's view hides cards from someone looking over his shoulder. A call made in the demo
view was still built from the whole fleet, so the front desk said "Globex pulled Sam's transcript" a
breath after agreeing to say nothing sensitive. These tests hold the fix: an agent outside the
view is absent from both prompts, from every tool, from the inbox, and from past calls.
"""

from pathlib import Path

from commandcenter import config, inbox, registry, voice
from tests import test_voice


class InAView(test_voice.VoiceCase):
    def setUp(self):
        super().setUp()
        self.shown = self.agent("scout")
        registry.update(self.shown["id"], views=["demo"])
        self.hidden = self.agent("ledger")
        registry.update(self.hidden["id"], views=["work"])
        self.transcript_for(self.hidden, [self.said("Sam sent the Teams transcript.")])
        self.transcript_for(self.shown, [self.said("The Now page is live.")])

    def test_the_prompts_name_only_the_agents_in_the_view(self):
        with voice.scoped("demo"):
            self.open_call()
        live = self.payload()["instructions"]
        back = self.backend()["instructions"]
        for prompt in (live, back):
            self.assertIn("scout", prompt)
            self.assertNotIn("ledger", prompt)
            self.assertIn("demo view", prompt)

    def test_with_no_view_the_whole_fleet_is_there(self):
        self.open_call()
        self.assertIn("ledger", self.payload()["instructions"])
        self.assertNotIn("demo view", self.payload()["instructions"])

    def test_all_is_the_whole_fleet(self):
        with voice.scoped("all"):
            self.open_call()
        self.assertIn("ledger", self.payload()["instructions"])

    def test_an_agent_with_no_tags_shows_in_every_view(self):
        self.agent("drifter")
        with voice.scoped("demo"):
            self.open_call()
        self.assertIn("drifter", self.payload()["instructions"])

    def test_fleet_status_leaves_out_the_hidden_agent(self):
        with voice.scoped("demo"):
            said = voice.call("fleet_status")
        self.assertIn("scout", said)
        self.assertNotIn("ledger", said)
        self.assertNotIn("Sam", said)

    def test_a_hidden_agent_does_not_exist_to_the_tools(self):
        with voice.scoped("demo"):
            got = voice.call("agent_history", {"agent": "ledger"})
            self.assertIn("There is no agent", got)
            with self.assertRaises(KeyError):
                voice.propose("ledger", "read the mail")
        self.assertEqual(voice.call("agent_history", {"agent": "ledger"})["resolved_to"],
                         "ledger")

    def test_past_calls_are_off_in_the_demo_view(self):
        voice.say_down("voice", "Globex found the Sam transcript.")
        with voice.scoped("demo"):
            self.open_call()
            for name in ("calls", "recall", "turns"):
                self.assertIn("Past calls are off", voice.call(name, {}))
        self.assertNotIn("Sam", self.backend()["instructions"])

    def test_the_inbox_says_only_items_from_the_view(self):
        inbox.add("ledger", "Sam's transcript is in.")
        inbox.add("scout", "The Now page is live.")
        with voice.scoped("demo"):
            got = self.open_call()
        told = [it["agent"] for it in got["brief"]]
        self.assertEqual(told, ["scout"])
        self.assertNotIn("Sam", self.payload()["instructions"])

    def test_a_file_in_a_hidden_agent_folder_cannot_be_read(self):
        config.VOICE_READ_ROOTS = (str(Path(self.dir).resolve()),)
        secret = Path(self.hidden["cwd"]) / "notes.md"
        secret.write_text("Sam\n")
        with voice.scoped("demo"):
            self.assertEqual(voice.call("read_file", {"path": str(secret)}), voice.NOT_ALLOWED)
        self.assertEqual(voice.call("read_file", {"path": str(secret)})["total_lines"], 1)

    def test_memory_is_free_in_the_other_views(self):
        voice.say_down("you", "Compare the cast of Son of Sardaar with Prem Ratan Dhan Payo.")
        for view in ("work", "personal"):
            with voice.scoped(view):
                self.open_call()
                self.assertNotIn("Past calls are off", str(voice.call("calls", {"n": 5})))
            self.assertIn("Son of Sardaar", self.backend()["instructions"])

    def test_an_unknown_view_is_the_whole_fleet(self):
        with voice.scoped("boardroom"):
            self.assertIsNone(voice.current_view())


class ViewOverTheRoutes(test_voice.Routes):
    def setUp(self):
        super().setUp()
        hidden = self.agent("ledger")
        registry.update(hidden["id"], views=["work"])
        registry.update(self.agent("scout")["id"], views=["demo"])

    def test_the_tool_route_keeps_to_the_view_it_is_sent(self):
        code, got = self.hit("POST", "/api/voice/tool",
                             {"name": "fleet_status", "args": {}, "view": "demo"})
        self.assertEqual(code, 200)
        self.assertNotIn("ledger", got["result"])
        code, got = self.hit("POST", "/api/voice/tool", {"name": "fleet_status", "args": {}})
        self.assertIn("ledger", got["result"])

    def test_the_offer_route_keeps_to_the_view_it_is_sent(self):
        code, _ = self.hit("POST", "/api/voice/offer", {"sdp": "v=0 offer", "view": "demo"})
        self.assertEqual(code, 200)
        self.assertNotIn("ledger", self.payload()["instructions"])
        self.assertIsNone(voice.current_view(), "the view must not outlive its request")


class PersonaFile(test_voice.Routes):
    """Config edits the front desk's persona. The real file is never touched here."""

    def setUp(self):
        super().setUp()
        self.file = Path(self.dir) / "voice_persona.md"
        self.file.write_text("Be dry.\n")
        real = voice.PERSONA
        voice.PERSONA = self.file
        self.addCleanup(lambda: setattr(voice, "PERSONA", real))

    def test_the_page_reads_the_file(self):
        code, got = self.hit("GET", "/api/voice/persona")
        self.assertEqual(code, 200)
        self.assertEqual(got["text"], "Be dry.\n")
        self.assertFalse(got["previous"])

    def test_a_save_reaches_the_next_call_and_keeps_the_old_one(self):
        code, got = self.hit("POST", "/api/voice/persona", {"text": "Be brief."})
        self.assertEqual(code, 200)
        self.assertTrue(got["previous"])
        self.assertEqual(self.file.read_text(), "Be brief.\n")
        voice.open_call("v=0 offer")
        self.assertIn("Be brief.", self.payload()["instructions"])

    def test_put_back_swaps_the_two(self):
        self.hit("POST", "/api/voice/persona", {"text": "Be brief."})
        code, got = self.hit("POST", "/api/voice/persona/previous", {})
        self.assertEqual(code, 200)
        self.assertEqual(self.file.read_text(), "Be dry.\n")
        self.hit("POST", "/api/voice/persona/previous", {})
        self.assertEqual(self.file.read_text(), "Be brief.\n")

    def test_an_empty_prompt_is_refused_and_the_file_stays(self):
        code, got = self.hit("POST", "/api/voice/persona", {"text": "   "})
        self.assertEqual(code, 400)
        self.assertIn("cannot be empty", got["error"])
        self.assertEqual(self.file.read_text(), "Be dry.\n")

    def test_nothing_to_put_back_says_so(self):
        code, got = self.hit("POST", "/api/voice/persona/previous", {})
        self.assertEqual(code, 400)

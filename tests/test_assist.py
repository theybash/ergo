"""Assistant mode: the bar's message is routed by Jev and lands in the agent's chat.

TypeSafe and the agents are never reached: `memory.post_jev` and `broker.send` are stand ins.
"""

import base64
import json

from commandcenter import assist, broker, config, memory
from tests import test_voice


class Assist(test_voice.Routes):
    def setUp(self):
        super().setUp()
        self.agent("personal-assistant")
        self.agent("darwin")
        config.TYPESAFE_KEY = "ts-not-a-real-key"
        self.sent = []
        real_jev, real_send, real_switch = memory.post_jev, broker.send, broker.switch
        memory.post_jev = lambda payload, key, timeout=None: {"answers": {"agent": {
            "type": "choice", "choice": "darwin", "confidence": 0.2}}}
        broker.send = lambda agent_id, text, wake=False: self.sent.append(
            {"agent": agent_id, "text": text, "wake": wake}) or "sent"
        broker.switch = lambda agent_id, want: want
        self.addCleanup(lambda: setattr(memory, "post_jev", real_jev))
        self.addCleanup(lambda: setattr(broker, "send", real_send))
        self.addCleanup(lambda: setattr(broker, "switch", real_switch))

    def test_an_unsure_pick_goes_to_the_home_agent_with_the_screen_and_is_logged(self):
        shot = base64.b64encode(b"\xff\xd8" + b"\x00" * 2000).decode()
        code, got = self.hit("POST", "/api/assist/send",
                             {"text": "what is this", "screenshot": shot, "app": "Chrome"})
        self.assertEqual(code, 200)
        self.assertEqual(got["agent"], "personal-assistant")
        self.assertEqual(self.sent[0]["agent"], "personal-assistant")
        self.assertTrue(self.sent[0]["wake"])
        self.assertIn("[From the assistant bar] what is this", self.sent[0]["text"])
        self.assertIn("[Attached: ", self.sent[0]["text"])
        log = [json.loads(line) for line in
               (config.VAR / "assist" / "routes.jsonl").read_text().splitlines()]
        self.assertEqual((log[-1]["jev"], log[-1]["agent"], log[-1]["how"]),
                         ("darwin", "personal-assistant", "routed"))

    def test_jev_reads_the_screen_he_added(self):
        seen = []
        memory.post_jev = lambda payload, key, timeout=None: seen.append(payload) or {
            "answers": {"agent": {"type": "choice", "choice": "darwin", "confidence": 0.9}}}
        code, got = self.hit("POST", "/api/assist/send", {
            "text": "Explain this", "app": "Chrome", "url": "https://example.org/crispr",
            "screen_text": "How CRISPR edits genes " * 200})
        self.assertEqual((code, got["agent"]), (200, "darwin"))
        screen = seen[0]["state"]["screen"]
        self.assertEqual(screen["link"], "https://example.org/crispr")
        self.assertTrue(screen["text"].startswith("How CRISPR edits genes"))
        self.assertLessEqual(len(screen["text"]), assist.JEV_SCREEN_CHARS)

    def test_a_name_the_owner_calls_wins_over_jev_and_over_the_face_the_phone_showed(self):
        self.agent("nova")
        self.agent("scout")
        for text, want in (("Nova, do this", "nova"), ("hey scout, any roles?", "scout"),
                           ("ask Darwin why the sky is blue", "darwin"),
                           ("I walked five scout", "personal-assistant")):
            code, got = self.hit("POST", "/api/assist/send", {"text": text, "agent": "darwin"})
            self.assertEqual((code, got["agent"]), (200, want), text)

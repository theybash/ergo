"""Voice notes from the wheel: the audio goes to OpenAI, the text goes to the agent.

OpenAI is never called: `voicenote.post_audio` is one function for that reason, and every test
puts a stand in there.
"""

import base64

from commandcenter import broker, config, voicenote
from tests import test_voice

AUDIO = base64.b64encode(b"\x00" * 4000).decode()


class VoiceNote(test_voice.Routes):
    def setUp(self):
        super().setUp()
        self.pa = self.agent("personal-assistant")
        self.calls = []

        def fake(data, filename, mime, key, english=False):
            self.calls.append({"bytes": len(data), "filename": filename, "mime": mime, "key": key, "english": english})
            return {"text": "  remind me to call   Sam  "}

        real_post, real_send = voicenote.post_audio, broker.send
        voicenote.post_audio = fake
        self.sent = []
        broker.send = lambda agent_id, text, wake=False: self.sent.append(
            {"agent": agent_id, "text": text, "wake": wake}) or "sent"
        self.addCleanup(lambda: setattr(voicenote, "post_audio", real_post))
        self.addCleanup(lambda: setattr(broker, "send", real_send))

    def test_a_note_is_written_down_and_sent_awake(self):
        code, got = self.hit("POST", "/api/voice/note",
                             {"agent": "personal-assistant", "mime": "audio/mp4", "audio": AUDIO})
        self.assertEqual(code, 200)
        self.assertEqual(got["text"], "remind me to call Sam")
        self.assertEqual(got["said"], "Sent to personal-assistant.")
        self.assertEqual(self.calls[0]["filename"], "note.m4a")
        self.assertEqual(self.calls[0]["key"], config.OPENAI_KEY)
        self.assertEqual(self.sent[0]["agent"], "personal-assistant")
        self.assertTrue(self.sent[0]["wake"])
        self.assertIn("[Voice note from the owner, transcribed] remind me", self.sent[0]["text"])

    def test_a_data_url_is_taken_too(self):
        code, _ = self.hit("POST", "/api/voice/note",
                           {"mime": "audio/webm", "audio": "data:audio/webm;base64," + AUDIO})
        self.assertEqual(code, 200)
        self.assertEqual(self.calls[0]["filename"], "note.webm")

    def test_no_key_says_so_and_calls_nothing(self):
        config.OPENAI_KEY = ""
        code, got = self.hit("POST", "/api/voice/note", {"audio": AUDIO})
        self.assertEqual(code, 503)
        self.assertEqual(self.calls, [])

    def test_a_broken_or_tiny_recording_is_refused(self):
        code, _ = self.hit("POST", "/api/voice/note", {"audio": "not base64!!"})
        self.assertEqual(code, 400)
        code, got = self.hit("POST", "/api/voice/note", {"audio": base64.b64encode(b"x").decode()})
        self.assertEqual(code, 400)
        self.assertIn("too short", got["error"])
        self.assertEqual(self.calls, [])

    def test_an_unknown_agent_is_refused(self):
        code, _ = self.hit("POST", "/api/voice/note", {"agent": "nobody", "audio": AUDIO})
        self.assertEqual(code, 400)

    def test_silence_is_not_sent(self):
        voicenote.post_audio = lambda *a, **k: {"text": "   "}
        code, got = self.hit("POST", "/api/voice/note", {"audio": AUDIO})
        self.assertEqual(code, 400)
        self.assertEqual(self.sent, [])

    def test_words_in_another_script_are_silence_not_a_note(self):
        for invented in ("謝謝。", "পৃথিবী"):
            voicenote.post_audio = lambda *a, t=invented, **k: {"text": t}
            code, got = self.hit("POST", "/api/voice/note", {"audio": AUDIO})
            self.assertEqual(code, 400)
            self.assertIn("Nothing heard", got["error"])
        self.assertEqual(self.sent, [])

    def test_hindi_in_devanagari_is_a_note(self):
        voicenote.post_audio = lambda *a, **k: {"text": "कल मीटिंग है"}
        code, _ = self.hit("POST", "/api/voice/note", {"audio": AUDIO})
        self.assertEqual(code, 200)
        self.assertEqual(len(self.sent), 1)

    def test_send_false_writes_it_down_and_sends_nothing(self):
        code, got = self.hit("POST", "/api/voice/note", {"audio": AUDIO, "send": False})
        self.assertEqual(code, 200)
        self.assertEqual(got["text"], "remind me to call Sam")
        self.assertNotIn("said", got)
        self.assertEqual(self.sent, [])

    def test_checked_words_go_through_send(self):
        code, got = self.hit("POST", "/api/voice/note/send",
                             {"agent": "personal-assistant", "text": "call Sam at five"})
        self.assertEqual(code, 200)
        self.assertEqual(got["said"], "Sent to personal-assistant.")
        self.assertIn("call Sam at five", self.sent[0]["text"])
        self.assertEqual(self.calls, [], "sending checked words must not transcribe again")

    def test_empty_checked_words_are_refused(self):
        code, _ = self.hit("POST", "/api/voice/note/send", {"text": "   "})
        self.assertEqual(code, 400)
        self.assertEqual(self.sent, [])


class Greeting(test_voice.Routes):
    WAV = base64.b64encode(b"RIFF" + b"\x00" * 4000).decode()

    def test_no_greeting_until_one_is_recorded(self):
        conn_code, _ = self.hit("GET", "/api/voice/greeting")
        self.assertEqual(conn_code, 404)

    def test_a_recorded_greeting_is_served_as_wav(self):
        code, got = self.hit("POST", "/api/voice/greeting", {"audio": self.WAV})
        self.assertEqual(code, 200)
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/voice/greeting")
        reply = conn.getresponse()
        body = reply.read()
        conn.close()
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.getheader("Content-Type"), "audio/wav")
        self.assertTrue(body.startswith(b"RIFF"))
        self.hit("POST", "/api/voice/greeting/reset", {})
        self.assertEqual(self.hit("GET", "/api/voice/greeting")[0], 404)

    def test_not_a_wav_is_refused(self):
        code, _ = self.hit("POST", "/api/voice/greeting", {"audio": AUDIO})
        self.assertEqual(code, 400)


class Heard(VoiceNote):
    def test_what_he_said_while_connecting_is_written_down_not_sent(self):
        code, got = self.hit("POST", "/api/voice/heard", {"audio": AUDIO, "mime": "audio/webm"})
        self.assertEqual(code, 200)
        self.assertEqual(got["text"], "remind me to call Sam")
        self.assertEqual(self.sent, [])

    def test_silence_while_connecting_is_empty_not_an_error(self):
        voicenote.post_audio = lambda *a, **k: {"text": "謝謝"}
        code, got = self.hit("POST", "/api/voice/heard", {"audio": AUDIO})
        self.assertEqual(code, 200)
        self.assertEqual(got["text"], "")

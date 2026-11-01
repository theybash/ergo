"""Dictation: the engine setting, and the short lived keys a browser dictates with."""

import json

from commandcenter import config, dictation
from tests import test_voice


class Dictation(test_voice.Routes):
    def setUp(self):
        super().setUp()
        self.posted = []

        def fake(url, payload, headers):
            self.posted.append((url, payload, headers))
            if "openai" in url:
                return {"value": "ek_test", "expires_at": 1}
            return {"name": "auth_tokens/test"}
        self.real_post = dictation._post
        dictation._post = fake
        self.addCleanup(setattr, dictation, "_post", self.real_post)
        config.OPENAI_KEY = "sk-test"
        self.addCleanup(setattr, config, "GEMINI_ENV", config.GEMINI_ENV)
        path = config.VAR / "gemini.env"
        path.write_text("GEMINI_API_KEY=g-test\n")
        config.GEMINI_ENV = str(path)

    def test_the_default_engine_is_openai_live(self):
        code, got = self.hit("GET", "/api/dictate")
        self.assertEqual(code, 200)
        self.assertEqual(got["engine"], "openai")

    def test_the_engine_is_saved_and_a_bad_one_refused(self):
        code, got = self.hit("POST", "/api/dictate", {"engine": "gemini"})
        self.assertEqual((code, got["engine"]), (200, "gemini"))
        self.assertEqual(self.hit("GET", "/api/dictate")[1]["engine"], "gemini")
        code, _ = self.hit("POST", "/api/dictate", {"engine": "shout"})
        self.assertEqual(code, 400)

    def test_an_openai_session_hands_out_a_short_key_never_the_real_one(self):
        code, got = self.hit("POST", "/api/dictate/session", {"engine": "openai"})
        self.assertEqual(code, 200)
        self.assertIn("openai-insecure-api-key.ek_test", got["protocols"])
        self.assertNotIn("sk-test", json.dumps(got))
        url, payload, headers = self.posted[-1]
        self.assertEqual(payload["session"]["type"], "transcription")
        self.assertIsNone(payload["session"]["audio"]["input"]["turn_detection"])
        self.assertLessEqual(payload["expires_after"]["seconds"], 60)
        # English only, his choice: the mixed hint put Hindi words into English dictation.
        self.assertEqual(payload["session"]["audio"]["input"]["transcription"]["languages"], ["en"])

    def test_a_gemini_session_hands_out_a_one_use_token_never_the_key(self):
        code, got = self.hit("POST", "/api/dictate/session", {"engine": "gemini"})
        self.assertEqual(code, 200)
        self.assertTrue(got["url"].endswith("access_token=auth_tokens/test"))
        self.assertNotIn("g-test", json.dumps(got))
        self.assertEqual(self.posted[-1][1]["uses"], 1)

    def test_the_session_follows_the_saved_engine(self):
        self.hit("POST", "/api/dictate", {"engine": "record"})
        code, got = self.hit("POST", "/api/dictate/session")
        self.assertEqual((code, got["engine"]), (200, "record"))
        self.assertEqual(self.posted, [])

    def test_no_key_says_so(self):
        config.OPENAI_KEY = ""
        code, got = self.hit("POST", "/api/dictate/session", {"engine": "openai"})
        self.assertEqual(code, 503)
        self.assertIn("OpenAI", got["error"])

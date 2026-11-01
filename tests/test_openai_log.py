"""The OpenAI usage log: one line per use, priced, kept for every day, summed for Config."""

from commandcenter import clock, openai_log, voice, voicenote
from tests import test_voice


class OpenAILog(test_voice.Routes):
    def test_a_call_is_priced_by_the_minute(self):
        voice.end_session("s1", 120)
        got = openai_log.summary()
        self.assertAlmostEqual(got["by_use"]["calls"]["today"], 0.10)
        self.assertEqual(got["by_use"]["calls"]["minutes"], 2.0)

    def test_the_same_session_ended_twice_is_logged_once(self):
        voice.end_session("s1", 60)
        voice.end_session("s1", 60)
        self.assertEqual(openai_log.summary()["by_use"]["calls"]["count"], 1)

    def test_dictation_minutes_come_from_the_browser(self):
        code, _ = self.hit("POST", "/api/openai/used", {"seconds": 30})
        self.assertEqual(code, 200)
        self.assertAlmostEqual(openai_log.summary()["by_use"]["dictation"]["today"], 0.0085)

    def test_tokens_are_priced_per_million(self):
        openai_log.record("vision", "gpt-6-luna", tokens_in=1_000_000, tokens_out=1_000_000)
        self.assertAlmostEqual(openai_log.summary()["today"], 0.60)

    def test_a_transcription_reports_its_seconds(self):
        self.assertEqual(openai_log.seconds_of({"usage": {"type": "duration", "seconds": 9}}), 9)
        voicenote._logged({"text": "hi", "usage": {"type": "duration", "seconds": 60}})
        self.assertAlmostEqual(openai_log.summary()["by_use"]["voice note"]["today"], 0.0045)

    def test_an_unknown_model_is_kept_unpriced(self):
        openai_log.record("vision", "gpt-new", tokens_in=10)
        self.assertEqual(openai_log.summary()["unpriced"], 1)

    def test_every_day_is_kept_and_the_week_sums_seven(self):
        openai_log.record("calls", "gpt-live-1", seconds=60)
        clock.freeze(clock.now() + 86400 * 8)
        openai_log.record("calls", "gpt-live-1", seconds=60)
        got = openai_log.summary()
        self.assertAlmostEqual(got["week"], 0.05)
        self.assertEqual(len(openai_log._file().read_text().splitlines()), 2)

    def test_config_reads_it_in_usage(self):
        voice.end_session("s1", 60)
        code, got = self.hit("GET", "/api/usage")
        self.assertAlmostEqual(got["openai"]["today"], 0.05)

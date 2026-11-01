"""The Integrations screen shows only what the daemon measured."""

from commandcenter import config, integrations, mail
from tests.harness import Case


class Integrations(Case):
    def test_a_service_is_connected_only_when_its_setting_is_there(self):
        real = mail.TOKEN
        mail.TOKEN = str(self.dir / "no-token.json")
        self.addCleanup(setattr, mail, "TOKEN", real)
        self.addCleanup(setattr, config, "OPENAI_KEY", config.OPENAI_KEY)
        config.OPENAI_KEY = ""
        got = integrations.snapshot()
        self.assertEqual({k: v["state"] for k, v in got.items()}, {
            "telegram": "not set up", "gmail": "not set up",
            "openai-voice": "not set up", "codex": "not installed"})
        self.assertIsNone(got["gmail"]["scope"])
        mail.token_file().write_text("{}")
        self.addCleanup(setattr, mail, "FROM", mail.FROM)
        mail.FROM = "owner@example.com"
        config.TELEGRAM_TOKEN, config.TELEGRAM_OPERATOR = "t", "1"
        got = integrations.snapshot()
        self.assertEqual((got["gmail"]["state"], got["gmail"]["scope"]), ("connected", "owner@example.com"))
        self.assertEqual(got["telegram"]["state"], "connected")

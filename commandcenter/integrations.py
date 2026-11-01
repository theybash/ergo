"""What the Integrations screen may say about each service, measured on this laptop.

The daemon says what it can check without the network, and the screen shows only that, so no
install shows a service as connected when it is not:

    telegram      the bot token and the operator id are set
    gmail         the token file mail.py sends with exists; the scope is mail.FROM
    openai-voice  the OpenAI key is set (the key itself never leaves the daemon)
    codex         the Codex CLI is on the PATH

A service missing here is not measured, and the screen says so. Nothing here reads a token: the
Gmail check is a file's existence, and the key checks are whether the string is empty.
"""

import shutil

from . import config, mail


def _row(ok, scope=None, off="not set up", on="connected"):
    if ok:
        return {"state": on, "badge": "ok", "scope": scope}
    return {"state": off, "badge": "needs", "scope": None}


def snapshot():
    return {
        "telegram": _row(bool(config.TELEGRAM_TOKEN and config.TELEGRAM_OPERATOR), "operator chat"),
        "gmail": _row(mail.token_file().exists(), mail.FROM or None),
        "openai-voice": _row(bool(config.OPENAI_KEY), "voice, dictation"),
        "codex": _row(bool(shutil.which(config.CODEX_BIN)), None, "not installed", "installed"),
    }

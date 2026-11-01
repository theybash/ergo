"""The logins: a KeePassXC vault, and the one door through which a password reaches a page.

    one vault    var/vault/personal.kdbx. "personal" is the one side every agent is on. The name
                 is kept so a vault made by an older build still opens
    the key      each vault opens with a random key file, and that key is sealed in the laptop's
                 TPM chip. The sealed copy is useless off this laptop; the open key lives only in
                 this process's memory, and touches disk only for the instant a vault is read, in
                 a private folder under /run/user, which is memory, not disk
    per agent    entries live in a group named after the agent: job-search/LinkedIn. An agent can
                 use only its own group
    never shown  no tool returns a password, a user name or a code. login() types them into the
                 page itself, and only after checking that the page is the site the entry was saved
                 for, by its address as Chrome reports it, never by what the agent says

What it cannot stop is the agent misusing an account once it is signed in. That is for the action
rules and the reviewer, not the vault.
"""

import base64
import hashlib
import hmac
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from . import clock, config, logs, registry

log = logs.get(__name__)

# One house, one side. Records and files still carry a side, and it is always this one.
SIDE = "personal"
SIDES = (SIDE,)
_lock = threading.Lock()
_keys = {}


def side_of(rec):
    """The side an agent is on. There is one, so it is always the same."""
    return SIDE


def _dir():
    d = config.VAR / "vault"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _db(side):
    return _dir() / f"{side}.kdbx"


# -- the TPM ---------------------------------------------------------------------

def _tpm(script, cwd):
    """Run tpm2 commands. Through `sg tss` until this login session has the tss group itself."""
    if os.access("/dev/tpmrm0", os.R_OK | os.W_OK):
        cmd = ["bash", "-c", script]
    else:
        cmd = ["sg", "tss", "-c", script]
    got = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=30)
    if got.returncode != 0:
        raise RuntimeError("the TPM refused: " + got.stderr.decode(errors="replace").strip()[-200:])


_private_said = False


@contextmanager
def _private():
    """A private folder for the open key. Memory on Linux, a private temp folder elsewhere.

    The usual home is /run/user/<uid>, which is tmpfs on Linux. macOS has no /run/user and no
    tmpfs at a known path, so the fallback is the user's own temporary directory. That is weaker,
    and it is said once rather than silently: the key file still exists for a moment either way,
    and the sealed copy in the TPM is what keeps the vault shut off this laptop.
    """
    global _private_said
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()) / "ergo-vault"
    try:
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        d = tempfile.mkdtemp(dir=base)
    except OSError:
        if not _private_said:
            _private_said = True
            log.info("vault: no /run/user here (macOS); the key folder falls back to temp")
        d = tempfile.mkdtemp(prefix="ergo-vault-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


PRIMARY = "tpm2_createprimary -Q -C o -g sha256 -G ecc -c primary.ctx"


def _seal(side, key):
    with _private() as d:
        (d / "key.bin").write_bytes(key)
        _tpm(f"{PRIMARY} && tpm2_create -Q -C primary.ctx -i key.bin -u seal.pub -r seal.priv", d)
        for part in ("pub", "priv"):
            shutil.copy(d / f"seal.{part}", _dir() / f"{side}.seal.{part}")


def _unseal(side):
    with _private() as d:
        for part in ("pub", "priv"):
            shutil.copy(_dir() / f"{side}.seal.{part}", d / f"seal.{part}")
        _tpm(f"{PRIMARY} && tpm2_load -Q -C primary.ctx -u seal.pub -r seal.priv -c seal.ctx "
             f"&& tpm2_unseal -c seal.ctx -o key.bin", d)
        return (d / "key.bin").read_bytes()


def _key(side):
    with _lock:
        if side not in _keys:
            _keys[side] = _unseal(side)
        return _keys[side]


def ensure(side):
    """The vault for one side, made on first use: a fresh key, sealed, and an empty database."""
    if side not in SIDES:
        raise ValueError("side must be personal")
    if _db(side).exists():
        return
    key = os.urandom(64)
    _seal(side, key)
    with _lock:
        _keys[side] = key
    with _keyfile(side) as k:
        _cli(["db-create", "--set-key-file", str(k), str(_db(side))], key_args=False)
    log.info("vault: the %s vault was made, its key sealed in the TPM", side)


@contextmanager
def _keyfile(side):
    with _private() as d:
        path = d / "k"
        path.write_bytes(_key(side))
        os.chmod(path, 0o600)
        yield path


def _cli(args, side=None, stdin=None, key_args=True):
    binary = shutil.which("keepassxc-cli")
    if not binary:
        raise RuntimeError("keepassxc-cli is not installed")
    if key_args:
        with _keyfile(side) as k:
            cmd = [binary, args[0], "--no-password", "--key-file", str(k)] + args[1:]
            got = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=30)
    else:
        got = subprocess.run([binary] + args, input=stdin, capture_output=True, text=True, timeout=30)
    if got.returncode != 0:
        # The error text never carries a secret: secrets only ever travel on stdin.
        raise RuntimeError(got.stderr.strip().splitlines()[-1] if got.stderr.strip() else "the vault said no")
    return got.stdout


# -- entries ---------------------------------------------------------------------

def _check_name(text, what):
    text = (text or "").strip()
    if not text or "/" in text or len(text) > 80:
        raise ValueError(f"{what} must be a short name with no slash")
    return text


def listing(side):
    """Every entry in one vault: agent, title and the site it is for. Never a user name or secret."""
    if not _db(side).exists():
        return []
    rows = []
    for line in _cli(["ls", "-R", "-f", str(_db(side))], side).splitlines():
        line = line.strip()
        if not line or line.endswith("/") or "/" not in line:
            continue
        agent, title = line.split("/", 1)
        try:
            url = _cli(["show", "-a", "URL", str(_db(side)), line], side).strip()
        except RuntimeError:
            url = ""
        rows.append({"agent": agent, "title": title, "url": url})
    return rows


def add(body):
    """Save a login for one agent. The password and the two factor secret go in on stdin only."""
    body = body or {}
    agent = _check_name(body.get("agent"), "agent")
    rec = registry.get(agent)
    if not rec:
        raise ValueError(f"no agent called {agent}")
    title = _check_name(body.get("title"), "title")
    url = (body.get("url") or "").strip()
    if not urllib.parse.urlsplit(url).netloc:
        raise ValueError("url must be the site's login address, like https://www.linkedin.com/login")
    password = body.get("password") or ""
    if not password:
        raise ValueError("a login needs a password")
    side = side_of(rec)
    ensure(side)
    db = str(_db(side))
    groups = _cli(["ls", db], side).split()
    if f"{agent}/" not in groups:
        _cli(["mkdir", db, agent], side)
    notes = ""
    totp = (body.get("totp") or "").replace(" ", "").upper()
    if totp:
        base64.b32decode(totp + "=" * (-len(totp) % 8))
        notes = "totp:" + totp
    args = ["add", "-u", (body.get("username") or "").strip(), "--url", url, "-p"]
    if notes:
        args += ["--notes", notes]
    # The notes line carries the TOTP secret on the command line for the instant the CLI runs.
    # Seen only by processes of this user, which can already read the daemon's memory.
    _cli(args + [db, f"{agent}/{title}"], side, stdin=password + "\n")
    log.info("vault: a login %s was saved for %s (%s side)", title, agent, side)
    return {"saved": f"{agent}/{title}", "side": side}


def remove(body):
    agent = _check_name((body or {}).get("agent"), "agent")
    title = _check_name((body or {}).get("title"), "title")
    side = side_of(registry.get(agent))
    _cli(["rm", str(_db(side)), f"{agent}/{title}"], side)
    log.info("vault: the login %s for %s was removed", title, agent)
    return {"removed": f"{agent}/{title}"}


def _secret(agent, title):
    rec = registry.get(agent)
    if not rec:
        raise ValueError(f"no agent called {agent}")
    side = side_of(rec)
    if not _db(side).exists():
        raise RuntimeError(f"there is no {side} vault yet. Add a login in Config first.")
    title = _check_name(title, "login")
    out = _cli(["show", "-s", "-a", "URL", "-a", "UserName", "-a", "Password", "-a", "Notes",
                str(_db(side)), f"{agent}/{title}"], side).split("\n")
    url, user, password = (out + ["", "", ""])[:3]
    notes = "\n".join(out[3:])
    totp = next((l[5:].strip() for l in notes.splitlines() if l.startswith("totp:")), "")
    return {"url": url.strip(), "user": user, "password": password, "totp": totp}


def code(secret, at=None, digits=6, step=30):
    """The six digit code an authenticator app would show now (RFC 6238)."""
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((at if at is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(n).zfill(digits)


def entries_for(agent):
    """The logins one agent may use, by title and site."""
    side = side_of(registry.get(agent))
    return [r for r in listing(side) if r["agent"] == agent]


# -- typing a login into a page -------------------------------------------------------

def _origin(url):
    p = urllib.parse.urlsplit(url or "")
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port)


FIELDS = """(() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && !e.disabled }
  const all = [...document.querySelectorAll('input')].filter(vis)
  const pw = all.find(e => e.type === 'password')
  const before = pw ? all.slice(0, all.indexOf(pw)) : all
  const user = before.reverse().find(e => /^(email|text|tel)$/.test(e.type) && !e.value)
    || all.find(e => e.autocomplete === 'username')
    || (!pw && all.find(e => /^(email|text|tel)$/.test(e.type) && /user|mail|login|phone|id/i.test(e.name + e.id + e.placeholder + e.autocomplete)))
  const otp = all.find(e => e.autocomplete === 'one-time-code' || /otp|totp|2fa|mfa|verification|code/i.test(e.name + e.id + e.placeholder))
  if (pw) pw.setAttribute('data-ergo-secret', '1')
  return { pw: !!pw, user: !!user, otp: !!otp && !pw,
           marks: [user && (user.setAttribute('data-ergo-user', '1'), 1), otp && (otp.setAttribute('data-ergo-otp', '1'), 1)] }
})()"""


def _fill(b, selector, text):
    node = b.page("Runtime.evaluate", {"expression": f"document.querySelector('{selector}')"})["result"]
    if not node.get("objectId"):
        return False
    b.page("DOM.focus", {"objectId": node["objectId"]})
    b.type_text(text)
    return True


def login(agent, title):
    """Sign the agent's browser in with one of its saved logins. Returns where the page went, and
    never anything that was typed."""
    from . import browsers
    s = _secret(agent, title)
    if not s["url"]:
        raise RuntimeError("that login has no site address saved, so it is never typed anywhere")
    b = browsers.get(agent)
    if b.control:
        raise RuntimeError("The owner has control of the browser right now.")
    b.used = clock.now()

    def here():
        return b.page("Runtime.evaluate", {"expression": "location.href", "returnByValue": True})["result"].get("value") or ""

    # Not on the site yet: go to the saved login page. Anywhere else, never type.
    if _origin(here()) != _origin(s["url"]):
        since = time.monotonic()
        b.page("Page.navigate", {"url": s["url"]})
        b.wait_load(since)
        time.sleep(0.8)
    steps = []
    for _ in range(3):
        url = here()
        if _origin(url) != _origin(s["url"]):
            raise RuntimeError(f"the page is now {urllib.parse.urlsplit(url).hostname}, not the site this "
                               "login was saved for, so nothing was typed. Ask the owner to take over.")
        f = b.page("Runtime.evaluate", {"expression": FIELDS, "returnByValue": True})["result"].get("value") or {}
        since = time.monotonic()
        if f.get("pw"):
            if f.get("user") and s["user"]:
                _fill(b, "[data-ergo-user]", s["user"])
            _fill(b, "[data-ergo-secret]", s["password"])
            steps.append("password")
        elif f.get("otp") and s["totp"]:
            _fill(b, "[data-ergo-otp]", code(s["totp"]))
            steps.append("code")
        elif f.get("user") and s["user"] and "user" not in steps:
            _fill(b, "[data-ergo-user]", s["user"])
            steps.append("user")
        else:
            break
        b.key("enter")
        b.wait_load(since, timeout=8)
        time.sleep(1.2)
    b.refs, b.ref_page = {}, None
    after = b.page("Runtime.evaluate", {"expression": "[location.href, document.title, "
                                         "!!document.querySelector('input[type=password]')]",
                                         "returnByValue": True})["result"].get("value") or ["", "", False]
    log.info("vault: %s used the login %s (%s)", agent, title, ", ".join(steps) or "nothing to fill")
    return {"filled": steps, "url": after[0], "title": after[1],
            "still_asking_for_a_password": bool(after[2]),
            "note": "Take a snapshot to see where you are. If the site asks for anything else, "
                    "use browser_handoff and the owner will finish it."}

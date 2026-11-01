"""A browser for every agent: its own windows in a real Chrome, on a virtual screen, driven only by
the daemon.

    one Chrome      one shared Chrome, with its profile folder in var/browsers/side-personal (the
                    one side there is). Every agent works in that Chrome, in windows of its own, so
                    a sign-in made once serves every agent. One Chrome per agent would mean one
                    Google sign-in, and one phone approval, per agent
    a real window   Chrome runs headful on one shared virtual screen (Xvfb), never headless:
                    sites treat a headless browser as a bot
    only while used it starts on the agent's first action and closes after IDLE_SECONDS with
                    nothing done and nobody watching; the profile on disk keeps the logins, so the
                    next action just starts it again
    a private pipe  the daemon talks to each Chrome over --remote-debugging-pipe, not a port. No
                    other program on the laptop can reach it, the agent included. That is what makes
                    the password rules enforceable: an agent with raw access could read any field

How an agent drives it, through the daemon's tools: `snapshot` lists the page's buttons, links and
fields from the accessibility tree, each with a short ref like e12; `act` clicks or types on a ref;
a ref dies when the page changes, so an agent never clicks something that has gone. Screenshots and
clicks by position are there for what the tree cannot see.

While the owner watches or has taken over, the browser stays open. While the owner has control,
the agent's actions wait: the page is the owner's until it is handed back.

Standard library only, like the rest of the daemon. The DevTools protocol over a pipe is JSON
messages separated by a NUL byte.
"""

import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse

from . import clock, config, logs, untrusted

log = logs.get(__name__)

IDLE_SECONDS = 10 * 60
MAX_RUNNING = 5
MAX_TABS = 4
DISPLAY = ":91"
SCREEN = (1366, 900)
STEP_TIMEOUT = 30.0
LOAD_TIMEOUT = 20.0
# How long the pauses between typed keys are, as a share of a hand's pace. The tests set 0.
TYPING_PAUSE = 1.0
# How long a click waits to see whether it started a page load. A click that stays on the page
# waits all of it.
CLICK_LOAD_WAIT = 3.0

# Roles worth naming in a snapshot. Everything else on a page is structure an agent does not act on.
ACTIONABLE = {"button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
              "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "option", "switch", "slider",
              "spinbutton", "listbox", "treeitem"}
CONTEXT = {"heading", "alert", "dialog", "alertdialog", "status"}

_lock = threading.RLock()
_browsers = {}          # agent id -> Browser, its view into its side's Chrome
_chromes = {}           # side -> Chrome
_xvfb = None
_reaper = None


def chrome_binary():
    return shutil.which("google-chrome") or shutil.which("chromium") or "google-chrome"


def _side(agent_id):
    from . import registry, vault
    return vault.side_of(registry.get(agent_id))


def _profile(name):
    path = config.VAR / "browsers" / name
    path.mkdir(parents=True, exist_ok=True)
    prefs = path / "Default" / "Preferences"
    if not prefs.exists():
        # Chrome's own password manager stays off: a fake login form on a page cannot then make
        # Chrome fill anything. Logins come from the vault, through the daemon, or from the owner.
        prefs.parent.mkdir(parents=True, exist_ok=True)
        prefs.write_text(json.dumps({
            "credentials_enable_service": False,
            "profile": {"password_manager_enabled": False, "default_content_setting_values": {
                "notifications": 2}},
            "autofill": {"profile_enabled": False, "credit_card_enabled": False},
        }))
    return path


def _ensure_display():
    """One virtual screen for every agent's window. Each window is drawn and captured on its own,
    so they may overlap on it freely."""
    global _xvfb
    with _lock:
        if _xvfb and _xvfb.poll() is None:
            return DISPLAY
        # A screen left from an earlier daemon is reused while it lives; a dead one's lock is cleared.
        num = DISPLAY.lstrip(":")
        lock = f"/tmp/.X{num}-lock"
        try:
            with open(lock) as fh:
                held = int(fh.read().strip())
        except (OSError, ValueError):
            held = None
        if held:
            try:
                os.kill(held, 0)
                return DISPLAY
            except ProcessLookupError:
                for stale in (lock, f"/tmp/.X11-unix/X{num}"):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            except PermissionError:
                raise RuntimeError(f"Another user holds the virtual screen {DISPLAY}.")
        binary = shutil.which("Xvfb")
        if not binary:
            raise RuntimeError("Xvfb is not installed, so there is no screen for a browser.")
        _xvfb = subprocess.Popen(
            [binary, DISPLAY, "-screen", "0", f"{SCREEN[0]}x{SCREEN[1]}x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        if _xvfb.poll() is not None:
            raise RuntimeError("The virtual screen did not start.")
        log.info("browsers: virtual screen up on %s", DISPLAY)
        return DISPLAY


class Gone(RuntimeError):
    """The browser closed while a step waited on it."""


class Chrome:
    """One side's Chrome and the pipe to it. Agents are Browsers, views into it."""

    def __init__(self, side):
        self.side = side
        self.proc = None
        self.wfd = None
        self.lock = threading.RLock()
        self.next_id = 0
        self.waiting = {}
        self.events = []
        self.owners = {}        # targetId -> agent id, for every window and tab an agent has
        self.agents = {}        # agent id -> Browser
        self.alive = False
        self.started = None
        self.used = clock.now()

    def start(self):
        display = _ensure_display()
        profile = _profile(f"side-{self.side}")
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        env = dict(os.environ, DISPLAY=display)
        args = [chrome_binary(), "--remote-debugging-pipe", f"--user-data-dir={profile}",
                "--password-store=basic", "--no-first-run", "--no-default-browser-check",
                "--disable-dev-shm-usage", "--disable-session-crashed-bubble",
                f"--window-size={SCREEN[0]},{SCREEN[1]}", "--window-position=0,0",
                "--disable-features=Translate,MediaRouter", "--lang=en-US", "about:blank"]
        # Chrome reads commands on fd 3 and writes answers on fd 4. A shell seats the two pipe ends
        # there and execs Chrome, so no Python runs between fork and exec, and nothing else of the
        # daemon's, like its listening socket, is handed to Chrome.
        # Moved to numbers of 10 and above first, so the seating can never close what it just seated.
        r = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD, 10)
        w = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD, 10)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        seat = f'exec "$@" 3<&{r} 4>&{w} {r}<&- {w}>&-'
        self.proc = subprocess.Popen(["/bin/bash", "-c", seat, "chrome"] + args, env=env,
                                     pass_fds=(r, w), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r)
        os.close(w)
        self.wfd = to_chrome_w
        self.alive = True
        self.started = clock.now()
        threading.Thread(target=self._read, args=(from_chrome_r,), name=f"cdp-{self.side}",
                         daemon=True).start()
        self.send("Target.setDiscoverTargets", {"discover": True})
        log.info("browsers: the %s side's Chrome started (pid %s)", self.side, self.proc.pid)

    def _read(self, fd):
        buf = b""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    raw, buf = buf.split(b"\0", 1)
                    try:
                        self._got(json.loads(raw))
                    except Exception:
                        log.exception("browsers: a message from the %s side's Chrome went wrong", self.side)
        except OSError:
            pass
        finally:
            os.close(fd)
            self.alive = False
            with self.lock:
                for box in self.waiting.values():
                    box["error"] = "the browser closed"
                    box["done"].set()
                self.waiting.clear()
            for b in list(self.agents.values()):
                b.alive = False

    def _got(self, msg):
        if "id" in msg:
            with self.lock:
                box = self.waiting.pop(msg["id"], None)
            if box:
                box["result"], box["error"] = msg.get("result"), (msg.get("error") or {}).get("message")
                box["done"].set()
            return
        method = msg.get("method", "")
        params = msg.get("params") or {}
        sid = msg.get("sessionId")
        self.events.append((time.monotonic(), method, sid, params))
        del self.events[:-400]
        if sid:
            owner = next((b for b in self.agents.values() if sid in b.sessions.values()), None)
            if owner:
                owner._event(method, params, sid)
            return
        if method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") != "page":
                return
            # A tab a page opens belongs to the agent whose page opened it.
            agent = self.owners.get(info.get("openerId"))
            if agent:
                self.owners[info["targetId"]] = agent
                if sum(1 for a in self.owners.values() if a == agent) > MAX_TABS:
                    self.send_nowait("Target.closeTarget", {"targetId": info["targetId"]})
                    return
                # The new window becomes the one in use, as in any browser: a sign-in a page
                # opens in a new window was invisible while the view stayed on the old one.
                b = self.agents.get(agent)
                if b:
                    threading.Thread(target=b.follow, args=(info["targetId"],), daemon=True).start()
        elif method == "Target.targetDestroyed":
            tid = params.get("targetId")
            agent = self.owners.pop(tid, None)
            b = self.agents.get(agent)
            if b:
                b.sessions.pop(tid, None)
                if tid == b.active:
                    b.active = next(iter(b.sessions), None)

    def send_nowait(self, method, params=None, session=None):
        with self.lock:
            self.next_id += 1
            msg = {"id": self.next_id, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            try:
                os.write(self.wfd, json.dumps(msg).encode() + b"\0")
            except OSError:
                self.alive = False
            return self.next_id

    def send(self, method, params=None, session=None, timeout=STEP_TIMEOUT):
        if not self.alive:
            raise Gone("the browser closed")
        box = {"done": threading.Event(), "result": None, "error": None}
        with self.lock:
            self.next_id += 1
            n = self.next_id
            self.waiting[n] = box
            msg = {"id": n, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            os.write(self.wfd, json.dumps(msg).encode() + b"\0")
        if not box["done"].wait(timeout):
            with self.lock:
                self.waiting.pop(n, None)
            raise RuntimeError(f"the browser did not answer {method} in {timeout:.0f} s")
        if box["error"]:
            raise RuntimeError(box["error"])
        return box["result"] or {}

    def close(self):
        self.alive = False
        try:
            if self.wfd is not None:
                os.write(self.wfd, json.dumps({"id": 0, "method": "Browser.close"}).encode() + b"\0")
        except OSError:
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
        try:
            if self.wfd is not None:
                os.close(self.wfd)
        except OSError:
            pass
        self.wfd = None
        for b in list(self.agents.values()):
            b.alive = False
        log.info("browsers: the %s side's Chrome closed", self.side)


class Browser:
    """One agent's part of its side's Chrome: its own windows, and the conversation about them."""

    def __init__(self, agent_id, chrome=None):
        self.agent = agent_id
        self.chrome = chrome
        self.lock = threading.RLock()
        self.sessions = {}      # targetId -> sessionId, for the pages it has open
        self.active = None      # the targetId the agent is working in
        self.refs = {}          # ref -> backendDOMNodeId, valid until the page changes
        self.ref_page = None
        self.used = clock.now()
        self.started = clock.now()
        self.watchers = 0
        self.control = False    # True while the owner has taken over
        self.frame_sinks = set()
        self.event_sinks = set()  # the live view, told when a login can be saved
        self.offer = None         # a login the owner just typed, waiting for Save or Not now
        self.typed_user = {}      # site -> (user name, when): the first page of a two step login
        self.capture_script = None
        self.alive = False
        self.fitted = False

    @property
    def events(self):
        return self.chrome.events if self.chrome else []

    def send(self, method, params=None, session=None, timeout=STEP_TIMEOUT):
        if not self.alive:
            raise Gone("the browser closed")
        return self.chrome.send(method, params, session=session, timeout=timeout)

    def send_nowait(self, method, params=None, session=None):
        return self.chrome.send_nowait(method, params, session=session)

    def open_window(self, url="about:blank"):
        """A new window of its own. A window, not a tab beside another agent's: a tab behind
        another draws nothing, so its live view would stay blank."""
        tid = self.chrome.send("Target.createTarget", {"url": url, "newWindow": True})["targetId"]
        self.chrome.owners[tid] = self.agent
        self.alive = True
        self.focus(tid)
        return tid

    def _event(self, method, params, sid):
        if method == "Page.screencastFrame":
            self.send_nowait("Page.screencastFrameAck", {"sessionId": params.get("sessionId")}, session=sid)
            for sink in list(self.frame_sinks):
                try:
                    sink(params.get("data"), params.get("metadata") or {})
                except Exception:
                    self.frame_sinks.discard(sink)
            return
        if method == "Runtime.bindingCalled" and params.get("name") == CAPTURE_BINDING:
            # Only his own hands count. A form the agent fills is never offered, so a page cannot
            # get something saved by talking an agent into typing it.
            if self.control:
                try:
                    _captured(self, json.loads(params.get("payload") or "{}"))
                except Exception:
                    log.exception("browsers: a login capture on %s's browser went wrong", self.agent)
            return
        if method == "Page.frameNavigated" and not (params.get("frame") or {}).get("parentId"):
            # The page changed under the refs, so they all go.
            if self.sessions.get(self.active) == sid:
                self.refs, self.ref_page = {}, None

    def page(self, method, params=None, timeout=STEP_TIMEOUT):
        """A command to the tab the agent is working in."""
        return self.send(method, params, session=self.sessions.get(self.active), timeout=timeout)

    def focus(self, target_id):
        if target_id not in self.sessions:
            sid = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
            self.sessions[target_id] = sid
            self.chrome.owners[target_id] = self.agent
            for m in ("Page.enable", "DOM.enable", "Accessibility.enable", "Runtime.enable"):
                self.send(m, session=sid)
            # Password fields are blurred in every screenshot and in the live view.
            self.send("Page.addScriptToEvaluateOnNewDocument", {"source": BLUR}, session=sid)
            self.send("Runtime.evaluate", {"expression": BLUR}, session=sid)
        self.active = target_id
        self.refs, self.ref_page = {}, None
        self.send("Target.activateTarget", {"targetId": target_id})

    def follow(self, target_id):
        """Move to a window its page just opened, and move the live view with it. Off the reader
        thread, since every step here waits on an answer that thread must read."""
        try:
            old = self.sessions.get(self.active)
            self.focus(target_id)
            if self.watchers:
                if old:
                    self.send_nowait("Page.stopScreencast", session=old)
                self.page("Page.bringToFront")
                self.page("Page.startScreencast", {"format": "jpeg", "quality": 55, "maxWidth": 1000,
                                                   "maxHeight": 1000, "everyNthFrame": 1})
                if self.control:
                    _listen(self, True)
                shot = self.screenshot()
                for sink in list(self.frame_sinks):
                    sink(shot, {"deviceWidth": SCREEN[0], "deviceHeight": SCREEN[1]})
        except Exception:
            log.exception("browsers: %s could not move to its new window", self.agent)

    def mine(self):
        """The pages that are this agent's, as Chrome lists them."""
        return [t for t in self.send("Target.getTargets")["targetInfos"]
                if t.get("type") == "page" and self.chrome.owners.get(t["targetId"]) == self.agent]

    def close(self):
        """Its windows close; the side's Chrome stays for the other agents."""
        for tid in list(self.sessions) + [t for t, a in list(self.chrome.owners.items()) if a == self.agent]:
            try:
                self.chrome.send_nowait("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass
            self.chrome.owners.pop(tid, None)
        self.sessions, self.active, self.alive = {}, None, False
        self.chrome.agents.pop(self.agent, None)
        log.info("browsers: %s's windows closed", self.agent)

    # -- what the agent can do -------------------------------------------------------

    def wait_load(self, since, timeout=LOAD_TIMEOUT):
        sid = self.sessions.get(self.active)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if any(t >= since and m == "Page.loadEventFired" and s == sid for t, m, s, _ in self.events):
                return True
            time.sleep(0.1)
        return False

    def snapshot(self, limit=250):
        """The page as a list of things to act on, each with a ref, plus where it is and its title."""
        nodes = self.page("Accessibility.getFullAXTree").get("nodes") or []
        info = self.page("Runtime.evaluate", {"expression": "[location.href, document.title]",
                                               "returnByValue": True})["result"].get("value") or ["", ""]
        refs, lines = {}, []
        for node in nodes:
            if node.get("ignored"):
                continue
            role = ((node.get("role") or {}).get("value") or "").lower()
            name = ((node.get("name") or {}).get("value") or "").strip()
            if role not in ACTIONABLE and role not in CONTEXT:
                continue
            if role in CONTEXT and not name:
                continue
            props = {p.get("name"): (p.get("value") or {}).get("value") for p in node.get("properties") or []}
            backend = node.get("backendDOMNodeId")
            if backend is None:
                continue
            ref = f"e{len(refs) + 1}"
            refs[ref] = backend
            value = (node.get("value") or {}).get("value")
            if props.get("protected") or "password" in name.lower():
                value = "(hidden)" if value else value
            bits = [ref, role, json.dumps(name[:80])]
            if value not in (None, ""):
                bits.append("value=" + json.dumps(str(value)[:60]))
            for flag in ("checked", "disabled", "expanded", "selected", "required"):
                if props.get(flag) not in (None, False, "false"):
                    bits.append(flag)
            lines.append(" ".join(bits))
            if len(lines) >= limit:
                lines.append(f"... and more; scroll or use text to see the rest")
                break
        self.refs, self.ref_page = refs, info[0]
        return {"url": info[0], "title": info[1], "elements": "\n".join(lines),
                "note": untrusted.note("a web page") + " Act on an element by its ref. Refs "
                        "last until the page changes."}

    def _box(self, ref):
        backend = self.refs.get(ref)
        if backend is None:
            raise RuntimeError(f"{ref} is not on the page any more. Take a new snapshot.")
        try:
            self.page("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
        except RuntimeError:
            pass
        quad = self.page("DOM.getBoxModel", {"backendNodeId": backend})["model"]["content"]
        xs, ys = quad[0::2], quad[1::2]
        return backend, sum(xs) / 4, sum(ys) / 4

    def click_at(self, x, y):
        for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.page("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left",
                                                   "clickCount": 1 if kind != "mouseMoved" else 0})

    def act(self, ref, action="click", text=None):
        backend, x, y = self._box(ref)
        since = time.monotonic()
        if action == "click":
            self.click_at(x, y)
        elif action == "type":
            self.page("DOM.focus", {"backendNodeId": backend})
            self.type_text(text or "")
        elif action == "clear_and_type":
            self.page("DOM.focus", {"backendNodeId": backend})
            self.key("ctrl+a")
            self.key("Backspace")
            self.type_text(text or "")
        elif action == "select":
            obj = self.page("DOM.resolveNode", {"backendNodeId": backend})["object"]["objectId"]
            self.page("Runtime.callFunctionOn", {"objectId": obj, "arguments": [{"value": text}],
                "functionDeclaration": "function(v){const o=[...this.options].find(o=>o.value===v||o.text.trim()===v);"
                                       "if(!o)throw new Error('no option '+v);this.value=o.value;"
                                       "this.dispatchEvent(new Event('input',{bubbles:true}));"
                                       "this.dispatchEvent(new Event('change',{bubbles:true}))}"})
        elif action == "hover":
            self.page("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        else:
            raise ValueError("action must be click, type, clear_and_type, select or hover")
        time.sleep(0.4)
        loaded = self.wait_load(since, timeout=CLICK_LOAD_WAIT) if action == "click" else False
        return {"done": action, "ref": ref, "page_changed": bool(loaded) or self.ref_page is None}

    KEYS = {"enter": ("Enter", 13), "tab": ("Tab", 9), "escape": ("Escape", 27),
            "backspace": ("Backspace", 8), "arrowdown": ("ArrowDown", 40), "arrowup": ("ArrowUp", 38),
            "arrowleft": ("ArrowLeft", 37), "arrowright": ("ArrowRight", 39), "pagedown": ("PageDown", 34),
            "pageup": ("PageUp", 33), "home": ("Home", 36), "end": ("End", 35), "space": (" ", 32),
            "delete": ("Delete", 46)}

    def key(self, combo):
        parts = combo.lower().split("+")
        mods = 0
        for m in parts[:-1]:
            mods |= {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "shift": 8}.get(m, 0)
        last = parts[-1]
        key, code = self.KEYS.get(last, (last.upper() if len(last) == 1 else last, ord(last.upper()) if len(last) == 1 else 0))
        # Enter and printable keys go as keyDown with their text, which is what submits a form or
        # types a character; the rest as rawKeyDown, which only presses.
        text = "\r" if key == "Enter" else (key if len(key) == 1 and not mods & 6 else "")
        down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": key if len(key) > 1 else "",
                "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code, "modifiers": mods}
        if text:
            down["text"] = down["unmodifiedText"] = text
        self.page("Input.dispatchKeyEvent", down)
        self.page("Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "windowsVirtualKeyCode": code,
                                             "nativeVirtualKeyCode": code, "modifiers": mods})
        return {"pressed": combo}

    def type_text(self, text):
        """Type like a person: a key down and a key up for every character, a short uneven pause
        between them. Input.insertText put the whole text in at once with no key presses, which a
        site reading its own keyboard events sees as a paste by a program, not typing."""
        import random
        for ch in str(text or ""):
            if ch == "\n":
                self.key("enter")
            else:
                down = {"type": "keyDown", "key": ch, "text": ch, "unmodifiedText": ch}
                if len(ch) == 1 and ch.isascii() and ch.isalnum():
                    code = ("Key" + ch.upper()) if ch.isalpha() else ("Digit" + ch)
                    down.update(code=code, windowsVirtualKeyCode=ord(ch.upper()))
                self.page("Input.dispatchKeyEvent", down)
                self.page("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch,
                                                     "code": down.get("code", "")})
            # Between 40 and 150 ms, now and then a longer pause, as a hand types.
            time.sleep(TYPING_PAUSE * (random.uniform(0.04, 0.15)
                                       + (random.random() < 0.08) * random.uniform(0.2, 0.5)))

    def screenshot(self):
        shot = self.page("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
        return shot["data"]

    def text(self, limit=12000):
        got = self.page("Runtime.evaluate", {"expression": "document.body ? document.body.innerText : ''",
                                              "returnByValue": True})["result"].get("value") or ""
        return got[:limit]


# Blurs password fields, and any field once typed as one, in what is captured.
BLUR = ("(()=>{const s=document.createElement('style');s.textContent="
        "'input[type=password],input[data-ergo-secret]{filter:blur(6px)!important}';"
        "(document.head||document.documentElement).appendChild(s)})()")


# -- the fleet of browsers -----------------------------------------------------------------

def get(agent_id, start=True):
    """The agent's browser: its window in its side's Chrome, opened if it has none."""
    with _lock:
        b = _browsers.get(agent_id)
        if b and b.alive and b.chrome and b.chrome.alive:
            return b
        if not start:
            return None
        side = _side(agent_id)
        c = _chromes.get(side)
        if not c or not c.alive:
            # The last Chrome of this profile must be gone first. Started while the old one was
            # still shutting down, a new Chrome finds the profile locked, hands over to the old one
            # and exits: four starts in ten seconds on 7 October.
            old = c.proc if c else None
            if old is not None and old.poll() is None:
                try:
                    old.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    old.kill()
                    old.wait(timeout=5)
            c = Chrome(side)
            _chromes[side] = c
            c.start()
        b = Browser(agent_id, c)
        c.agents[agent_id] = b
        _browsers[agent_id] = b
        b.open_window()
        c.used = clock.now()
        _start_reaper()
        return b


def status():
    with _lock:
        now = clock.now()
        return {aid: {"open": b.alive, "watchers": b.watchers, "control": b.control,
                      "idle_seconds": round(now - b.used), "closes_in": max(0, round(IDLE_SECONDS - (now - b.used)))
                      if not (b.watchers or b.control) else None}
                for aid, b in _browsers.items() if b.alive}


def close(agent_id):
    with _lock:
        b = _browsers.pop(agent_id, None)
    if b and b.chrome:
        b.close()


def close_all():
    with _lock:
        _browsers.clear()
        chromes = list(_chromes.values())
        _chromes.clear()
    for c in chromes:
        c.close()


def _start_reaper():
    global _reaper
    if _reaper and _reaper.is_alive():
        return
    _reaper = threading.Thread(target=_reap, name="browser-reaper", daemon=True)
    _reaper.start()


def _reap():
    while True:
        time.sleep(30)
        with _lock:
            now = clock.now()
            for aid, b in list(_browsers.items()):
                if not b.alive:
                    _browsers.pop(aid, None)
                    continue
                if b.watchers or b.control:
                    b.used = now
                    continue
                if now - b.used > IDLE_SECONDS:
                    log.info("browsers: %s idle %d s, closing its windows", aid, now - b.used)
                    _browsers.pop(aid, None)
                    threading.Thread(target=b.close, daemon=True).start()
            # A side's Chrome goes once none of its agents has had a window for the idle time.
            for side, c in list(_chromes.items()):
                if any(x.chrome is c and x.alive for x in _browsers.values()):
                    c.used = now
                elif now - c.used > IDLE_SECONDS:
                    _chromes.pop(side, None)
                    threading.Thread(target=c.close, daemon=True).start()


# -- one entry for every action, as the tools call it ----------------------------------------

def run(agent_id, action, args):
    """Carry out one browser action for an agent. Returns a plain result; a screenshot comes back as
    {"image": base64 jpeg}."""
    args = args if isinstance(args, dict) else {}
    if action == "close":
        close(agent_id)
        return {"closed": True}
    b = get(agent_id)
    if b.control:
        raise RuntimeError("The owner has control of your browser right now. Wait for him to hand it back.")
    b.used = clock.now()
    try:
        if action == "open":
            url = str(args.get("url") or "").strip()
            if not url:
                raise ValueError("open needs a url")
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
                url = "https://" + url
            if args.get("new_tab"):
                if len(b.mine()) >= MAX_TABS:
                    raise RuntimeError(f"You have {MAX_TABS} tabs open. Close one or reuse it.")
                b.open_window()
            since = time.monotonic()
            got = b.page("Page.navigate", {"url": url})
            if got.get("errorText"):
                raise RuntimeError(f"could not open {url}: {got['errorText']}")
            b.wait_load(since)
            return b.snapshot()
        if action == "snapshot":
            return b.snapshot()
        if action == "act":
            return b.act(str(args.get("ref") or ""), args.get("action") or "click", args.get("text"))
        if action == "click_xy":
            b.click_at(float(args["x"]), float(args["y"]))
            return {"clicked": [args["x"], args["y"]]}
        if action == "type":
            b.type_text(str(args.get("text") or ""))
            return {"typed": len(str(args.get("text") or ""))}
        if action == "key":
            return b.key(str(args.get("key") or "enter"))
        if action == "scroll":
            dy = float(args.get("dy") or 600)
            b.page("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": SCREEN[0] / 2, "y": SCREEN[1] / 2,
                                                "deltaX": 0, "deltaY": dy})
            time.sleep(0.3)
            b.refs, b.ref_page = {}, None
            return {"scrolled": dy, "note": "Refs were cleared. Take a new snapshot."}
        if action == "screenshot":
            return {"image": b.screenshot()}
        if action == "text":
            return {"warning": untrusted.note("a web page"), "text": b.text()}
        if action == "back":
            since = time.monotonic()
            b.page("Runtime.evaluate", {"expression": "history.back()"})
            b.wait_load(since, timeout=8)
            return b.snapshot()
        if action == "tabs":
            tabs = b.mine()
            return {"tabs": [{"id": t["targetId"][:8], "url": t.get("url"), "title": t.get("title"),
                              "active": t["targetId"] == b.active} for t in tabs]}
        if action == "switch_tab":
            want = str(args.get("id") or "")
            tabs = b.mine()
            hit = next((t for t in tabs if t["targetId"].startswith(want)), None)
            if not hit:
                raise ValueError(f"no tab {want}")
            b.focus(hit["targetId"])
            return b.snapshot()
        if action == "wait":
            time.sleep(min(float(args.get("seconds") or 2), 15))
            return b.snapshot()
        raise ValueError(f"no browser action called {action}")
    finally:
        b.used = clock.now()


# -- the live view and takeover -----------------------------------------------------------

def watch(agent_id, sink):
    """Stream the agent's page to `sink(data_b64, metadata)` until unwatch. Starts the browser if it is
    closed, so opening the view is enough to look at it. While anybody watches, it never idles shut."""
    b = get(agent_id)
    with b.lock:
        b.watchers += 1
        b.frame_sinks.add(sink)
        first = b.watchers == 1
    if first:
        # The streamed tab must be the one Chrome shows: a tab behind another draws nothing, and
        # a screencast of it sends no frames at all.
        try:
            b.page("Page.bringToFront")
        except Exception:
            pass
        b.page("Page.startScreencast", {"format": "jpeg", "quality": 55, "maxWidth": 1000,
                                        "maxHeight": 1000, "everyNthFrame": 1})
    # Every viewer gets a picture now. A screencast sends a frame only when the page changes,
    # so a still page left a new viewer on "Opening the browser" for as long as it stayed still.
    try:
        sink(b.screenshot(), {"deviceWidth": SCREEN[0], "deviceHeight": SCREEN[1]})
    except Exception:
        pass
    return b


def fit(agent_id, width, height, mobile=True):
    """The page takes the viewer's size, the way OpenClaw fits its panel: on a phone the site lays
    itself out for a phone, readable without zooming. Undone when the last viewer leaves."""
    b = _browsers.get(agent_id)
    if not b or not b.alive:
        return
    w = max(320, min(int(width or 0), 1600))
    h = max(400, min(int(height or 0), 1600))
    b.page("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2 if mobile else 1,
                                                   "mobile": bool(mobile)})
    b.page("Emulation.setTouchEmulationEnabled", {"enabled": bool(mobile), "maxTouchPoints": 5 if mobile else 0})
    b.fitted = True
    b.refs, b.ref_page = {}, None


def unfit(b):
    if getattr(b, "fitted", False) and b.alive:
        try:
            b.send_nowait("Emulation.clearDeviceMetricsOverride", session=b.sessions.get(b.active))
            b.send_nowait("Emulation.setTouchEmulationEnabled", {"enabled": False}, session=b.sessions.get(b.active))
        except Exception:
            pass
        b.fitted = False
        b.refs, b.ref_page = {}, None


def unwatch(agent_id, sink):
    b = _browsers.get(agent_id)
    if not b:
        return
    with b.lock:
        b.frame_sinks.discard(sink)
        b.watchers = max(0, b.watchers - 1)
        last = b.watchers == 0
        if last:
            b.control = False
    b.used = clock.now()
    if last and b.alive:
        try:
            b.send_nowait("Page.stopScreencast", session=b.sessions.get(b.active))
        except Exception:
            pass
        # Back to the desktop size the agent works at.
        unfit(b)


def control(agent_id, on):
    """The owner takes the page, or hands it back. While he has it, the agent's actions wait, and a
    login he types can be saved to the vault."""
    b = _browsers.get(agent_id)
    if not b or not b.alive:
        raise RuntimeError("that browser is not open")
    b.control = bool(on)
    _listen(b, b.control)
    b.used = clock.now()
    log.info("browsers: The owner %s %s's browser", "took" if on else "handed back", agent_id)
    return {"control": b.control}


def human(agent_id, msg):
    """One input from the live view, in page pixels: a tap, typed text, a key, a scroll."""
    b = _browsers.get(agent_id)
    if not b or not b.alive or not b.control:
        return
    b.used = clock.now()
    kind = msg.get("t")
    if kind == "tap":
        if getattr(b, "fitted", False):
            x, y = float(msg["x"]), float(msg["y"])
            for t in ("touchStart", "touchEnd"):
                b.page("Input.dispatchTouchEvent", {"type": t, "touchPoints": [{"x": x, "y": y}] if t == "touchStart" else []})
        else:
            b.click_at(float(msg["x"]), float(msg["y"]))
    elif kind == "type":
        b.type_text(str(msg.get("text") or ""))
    elif kind == "key":
        b.key(str(msg.get("key") or "enter"))
    elif kind == "scroll":
        b.page("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": float(msg.get("x") or SCREEN[0] / 2),
                                            "y": float(msg.get("y") or SCREEN[1] / 2), "deltaX": 0,
                                            "deltaY": float(msg.get("dy") or 0)})
    elif kind == "back":
        b.page("Runtime.evaluate", {"expression": "history.back()"})
    elif kind == "open":
        url = str(msg.get("url") or "")
        if url:
            b.page("Page.navigate", {"url": url if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url) else "https://" + url})
    b.refs, b.ref_page = {}, None


# -- saving a login he typed -------------------------------------------------------------
# A password manager's "Save this login?", without an extension. While the owner has the page, a
# small script watches for a submitted password and hands the site, the user name and the
# password to the daemon through a binding. The live view then asks him; Save puts it in the
# vault for this agent, Not now or two quiet minutes forget it. The agent never sees any of it.

CAPTURE_BINDING = "__ergoLogin"
OFFER_SECONDS = 120
USER_SECONDS = 15 * 60

CAPTURE = ("(()=>{if(window.__ergoCap)return;window.__ergoCap=1;"
           "const send=o=>{try{window.__ergoLogin(JSON.stringify(o))}catch(e){}};"
           "const vis=i=>i.value&&i.offsetParent!==null;"
           "const grab=r=>{const pw=[...r.querySelectorAll('input[type=password]')].find(i=>i.value);"
           "const us=[...r.querySelectorAll('input[type=email],input[type=text],input[type=tel],input:not([type])')].filter(vis);"
           "return{pw:pw?pw.value:'',user:us.length?us[us.length-1].value:''}};"
           "const fire=r=>{const g=grab(r&&r.querySelectorAll?r:document);if(g.pw||g.user)send({origin:location.origin,user:g.user,pw:g.pw})};"
           "document.addEventListener('submit',e=>fire(e.target),true);"
           "document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target&&e.target.tagName==='INPUT')fire(e.target.form||document)},true);"
           "document.addEventListener('click',e=>{const b=e.target.closest&&e.target.closest('button,input[type=submit],[role=button],a');"
           "if(b)fire(b.form||document)},true);})()")


def _listen(b, on):
    sid = b.sessions.get(b.active)
    try:
        if on:
            b.send("Runtime.addBinding", {"name": CAPTURE_BINDING}, session=sid)
            b.capture_script = b.send("Page.addScriptToEvaluateOnNewDocument", {"source": CAPTURE},
                                      session=sid).get("identifier")
            b.send("Runtime.evaluate", {"expression": CAPTURE}, session=sid)
        elif b.capture_script:
            b.send_nowait("Page.removeScriptToEvaluateOnNewDocument", {"identifier": b.capture_script}, session=sid)
            b.capture_script = None
    except Exception:
        log.exception("browsers: the login capture on %s's browser did not switch", b.agent)


def _site(origin):
    host = (urllib.parse.urlsplit(origin).hostname or "").lower()
    if re.fullmatch(r"[0-9.]+|[0-9a-f:]+", host):
        return host
    parts = host.split(".")
    # amazon.in and www.amazon.in are one site; a.co.uk keeps three labels.
    keep = 3 if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") else 2
    return ".".join(parts[-keep:]) if host else ""


def _captured(b, got):
    site = _site(got.get("origin") or "")
    if not site:
        return
    now = clock.now()
    user = (got.get("user") or "").strip()
    if user and not got.get("pw"):
        b.typed_user[site] = (user, now)
        return
    pw = got.get("pw") or ""
    if not pw:
        return
    if not user:
        held = b.typed_user.get(site)
        user = held[0] if held and now - held[1] < USER_SECONDS else ""
    same = b.offer and (b.offer["site"], b.offer["user"], b.offer["pw"]) == (site, user, pw)
    b.offer = {"site": site, "url": got.get("origin") + "/", "user": user, "pw": pw, "at": now}
    if same:
        return
    log.info("browsers: %s's browser can save a login for %s", b.agent, site)
    for sink in list(b.event_sinks):
        try:
            sink({"t": "save_login", "site": site, "user": user})
        except Exception:
            b.event_sinks.discard(sink)
    # A daemon timer, so an offer nobody answered never holds the process open at exit.
    timer = threading.Timer(OFFER_SECONDS, _forget, args=(b, b.offer))
    timer.daemon = True
    timer.start()


def _forget(b, offer):
    if b.offer is offer:
        b.offer = None


def answer_offer(agent_id, yes):
    """Save or drop the login he just typed. Answers what happened, never the password."""
    from . import registry, vault
    b = _browsers.get(agent_id)
    offer = b.offer if b else None
    if b:
        b.offer = None
    if not offer or not yes:
        return {"saved": False}
    if clock.now() - offer["at"] > OFFER_SECONDS:
        return {"saved": False, "why": "too late, it was forgotten"}
    title = offer["site"]
    if any(r.get("agent") == agent_id and r.get("title") == title
           for r in vault.listing(vault.side_of(registry.get(agent_id)))):
        vault.remove({"agent": agent_id, "title": title})
    vault.add({"agent": agent_id, "title": title, "url": offer["url"],
               "username": offer["user"], "password": offer["pw"]})
    return {"saved": True, "title": title, "user": offer["user"]}

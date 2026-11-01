"""The daemon retrieving an address, because a screen has no network of its own.

A view runs in a frame whose origin matches nothing and whose policy is `connect-src 'none'`, so
it cannot reach anything at all. That rule stays: it is what stops a stranger's code in the
operator's browser talking to the stranger's server. The screen asks the daemon instead.

Answer 30 of the daemon grill: any address. Not a list, not a permission, not a key holder. Two
limits remain and neither is about trust. A time limit, because a screen waiting on a dead host
holds a thread and a tab. A size limit, because a screen asking for a two gigabyte file would
otherwise freeze the tab it is drawn in.

It forwards nothing of the operator's: no cookies, no Authorization the operator holds, no invented
headers. What it does forward is what the screen asked for, and that is deliberate. A screen is its
own agent's code, so a key in it is that agent's key, and the agent already holds its own keys by
the decision of 18 August.

**This makes the laptop a proxy for anything the laptop can reach**, which includes the tailnet and
so the operator's own devices. That is written here rather than left as a surprise. Nothing on the
tailnet is reachable that an agent could not already reach with curl.
"""

import base64
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import config, logs

log = logs.get(__name__)


class Failed(ValueError):
    """The fetch was attempted and did not work: a dead host, a timeout, a refused connection.

    Separate from an ordinary ValueError, which here means the caller asked for something this will
    not send. The difference is the status code the screen sees, and it matters: a screen told 502
    for its own typo sends you looking at the network.
    """

# Hop-by-hop headers belong to one connection, and a Cookie or Host from a screen would either
# break the request or claim to be someone. Everything else the screen asks for is passed on.
BLOCKED = {"host", "cookie", "connection", "keep-alive", "proxy-authorization",
           "transfer-encoding", "upgrade", "te", "trailer", "content-length"}

AGENT = "commandcenter/2 (a screen asked)"


def get(url, method="GET", headers=None, body=None, timeout=None, max_bytes=None,
        who="a screen"):
    """Fetch one address and hand back what came, or raise ValueError with why not."""
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("only http and https addresses can be fetched")
    method = (method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise ValueError(f"{method} is not a method this will send")

    timeout = min(float(timeout or config.FETCH_TIMEOUT), config.FETCH_TIMEOUT)
    cap = min(int(max_bytes or config.FETCH_MAX_BYTES), config.FETCH_MAX_BYTES)

    send = {"User-Agent": AGENT, "Accept-Encoding": "identity"}
    for key, value in (headers or {}).items():
        if str(key).lower() in BLOCKED:
            continue
        send[str(key)] = str(value)

    payload = body
    if isinstance(payload, (dict, list)):
        payload = json.dumps(payload).encode()
        send.setdefault("Content-Type", "application/json")
    elif isinstance(payload, str):
        payload = payload.encode()

    started = time.time()
    req = urllib.request.Request(url, data=payload, method=method, headers=send)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = _read(resp, cap)
            out["status"] = resp.status
            out["url"] = resp.geturl()
            out["headers"] = {k.lower(): v for k, v in resp.headers.items()
                              if k.lower() in ("content-type", "last-modified", "etag", "date")}
    except urllib.error.HTTPError as exc:
        # An error status is an answer, not a failure. A screen showing "429, try later" is more
        # use than a screen showing "the fetch failed".
        out = _read(exc, cap)
        out["status"] = exc.code
        out["url"] = url
        out["headers"] = {k.lower(): v for k, v in (exc.headers or {}).items()
                          if k.lower() in ("content-type", "retry-after")}
    except Exception as exc:
        # The address is logged without its query, both here and on success. A key in a query
        # string is a key in the log file otherwise, and the log is kept for seven days.
        log.warning("fetch for %s: %s://%s%s failed: %s", who, parts.scheme, parts.netloc,
                    parts.path, exc)
        raise Failed(str(exc))

    out["ms"] = int((time.time() - started) * 1000)
    log.info("fetch for %s: %s %s://%s%s -> %s, %d bytes in %dms%s", who, method, parts.scheme,
             parts.netloc, parts.path, out["status"], out["bytes"], out["ms"],
             " (cut off)" if out["truncated"] else "")
    return out


def _read(resp, cap):
    """Read at most cap bytes, and say plainly if there was more.

    One byte over the limit is read on purpose. Reading exactly cap and getting cap bytes cannot
    tell a file that fits from a file that does not, and a screen quietly drawing three quarters of
    a table is worse than a screen saying it was cut off.
    """
    raw = resp.read(cap + 1)
    truncated = len(raw) > cap
    raw = raw[:cap]
    ctype = (resp.headers.get("Content-Type") or "").lower()
    out = {"bytes": len(raw), "truncated": truncated}

    if "json" in ctype and not truncated:
        try:
            out["json"] = json.loads(raw.decode("utf-8", "replace"))
            out["text"] = None
            return out
        except Exception:
            pass
    text_like = (ctype.startswith("text/") or "json" in ctype or "xml" in ctype
                 or "javascript" in ctype or not ctype)
    if text_like:
        out["text"] = raw.decode("utf-8", "replace")
    else:
        # Not text, so it goes across as data. A screen can put it in an img src and nothing has to
        # guess an encoding.
        out["base64"] = base64.b64encode(raw).decode()
        out["text"] = None
    return out

"""Network health, in pairs.

A single number is noise. "The internet is slow" is not a diagnosis, and neither is one latency
figure: 90ms to the internet with 1ms to the router means the line is slow, and 90ms to both means
the router is the problem. So everything here comes in twos, and the pair is the point.

    gateway latency  beside  internet latency     which hop is hurting
    link rate        beside  signal strength      a weak signal or a busy channel
    down             beside  up                   which direction is capped

This laptop has a real reason for the file. The router is a Tenda N301 from 2013 with 16 MB of RAM
and the connection table lives in that memory, so when several people are home it falls over every
half hour. The pair that shows it is gateway latency climbing from 1ms to 100ms while the internet
figure is unchanged: nothing is wrong with the line, the box in the hall is out of memory.

The speed test needs a button rather than a timer, because it costs real bandwidth on a connection
that is already the bottleneck. Everything else here is free: a few pings and three files in /sys and
/proc.
"""

import re
import subprocess
import threading
import time
import urllib.request

from . import logs

log = logs.get(__name__)

# One line about the missing /proc reading is enough; this is polled every few seconds.
_wireless_said = False

# Cloudflare, because it answers from a machine near you and needs no key. It refuses a bare urllib
# request with 403, so the browser-shaped user agent is not decoration.
DOWN = "https://speed.cloudflare.com/__down?bytes={bytes}"
UP = "https://speed.cloudflare.com/__up"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"

# Somebody else's resolver, chosen because it answers ICMP and is not on the same network as the
# laptop. It is the far end of the pair, not a recommendation.
INTERNET = "1.1.1.1"

PING = re.compile(r"([\d.]+)% packet loss")
RTT = re.compile(r"rtt min/avg/max/\S+ = ([\d.]+)/([\d.]+)/([\d.]+)/")
ROUTE = re.compile(r"^default via (\S+) dev (\S+)")

MIN_AGE = 20.0

_lock = threading.Lock()
_cached = None
_cached_at = 0.0


def _run(argv, timeout=6.0):
    out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return out.stdout


def gateway():
    """The router's address, and which interface reaches it."""
    try:
        for line in _run(["ip", "route"]).splitlines():
            m = ROUTE.match(line.strip())
            if m:
                return {"address": m.group(1), "device": m.group(2)}
    except Exception as exc:
        log.warning("cannot read the route table: %s", exc)
    return {"address": None, "device": None}


def _parse_ping(text):
    """What ping said: average, worst, and how much did not come back."""
    loss = PING.search(text or "")
    rtt = RTT.search(text or "")
    if not rtt:
        # Total loss still answers the question. None here would read as "not measured", which is a
        # different claim from "nothing came back".
        return {"avg_ms": None, "max_ms": None,
                "loss_pct": float(loss.group(1)) if loss else 100.0}
    return {"avg_ms": round(float(rtt.group(2)), 1), "max_ms": round(float(rtt.group(3)), 1),
            "loss_pct": float(loss.group(1)) if loss else 0.0}


def latency(host, count=3):
    """Round trip to one host. Fast on purpose: the pair is wanted on a page, not in a report."""
    if not host:
        return None
    try:
        # -i 0.2 sends them a fifth of a second apart, so three pings take half a second rather
        # than two. -W 1 gives up on a silent host after a second instead of ten.
        text = _run(["ping", "-c", str(count), "-i", "0.2", "-W", "1", "-q", host], timeout=6.0)
    except Exception as exc:
        log.info("ping %s failed: %s", host, exc)
        return None
    return _parse_ping(text)


def parse_nmcli(text):
    """The active network's signal and link rate, from `nmcli -t -f ACTIVE,SSID,SIGNAL,RATE`."""
    out = {"ssid": None, "signal_pct": None, "rate_mbit": None}
    for line in (text or "").splitlines():
        if not line.startswith("yes:"):
            continue
        parts = (line.split(":") + ["", "", ""])[:4]
        out["ssid"] = parts[1] or None
        out["signal_pct"] = int(parts[2]) if parts[2].isdigit() else None
        got = re.search(r"(\d+)", parts[3] or "")
        out["rate_mbit"] = int(got.group(1)) if got else None
        break
    return out


def parse_wireless(text):
    """Signal level and the retry counter, from /proc/net/wireless.

    The columns are: interface, status, link, level, noise, nwid, crypt, frag, retry, misc, beacon.
    Retry is the eighth number after the colon and there is nowhere else to read it.
    """
    for line in (text or "").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        fields = rest.split()
        if len(fields) < 8:
            continue
        try:
            return {"device": name.strip(), "level_dbm": int(float(fields[2])),
                    "retries": int(fields[7])}
        except ValueError:
            continue
    return {"device": None, "level_dbm": None, "retries": None}


def wifi(device=None):
    """Link rate beside signal strength, plus the retry counter.

    The retry counter is the most useful number on this laptop and the least known. It climbs before
    a bad access point gives up entirely, so it is the one that moves first on an evening when the
    router is about to fall over.
    """
    out = {"ssid": None, "signal_pct": None, "rate_mbit": None, "level_dbm": None,
           "retries": None, "device": device}
    try:
        # `list --rescan no` reads the last scan NetworkManager already did, instead of forcing a
        # fresh one. A forced scan needs the wifi.scan polkit action, and in a remote (xrdp) session
        # polkit will not grant it, so it threw an authentication dialog at the operator every poll.
        # The connected AP's signal is in the cache and NetworkManager refreshes it on its own, so
        # the readout stays live without us asking to scan.
        out.update(parse_nmcli(
            _run(["nmcli", "-t", "-f", "ACTIVE,SSID,SIGNAL,RATE", "dev", "wifi", "list",
                  "--rescan", "no"])))
    except Exception as exc:
        log.info("nmcli did not answer: %s", exc)
    try:
        # /proc/net/wireless needs no permission, and is the only place the retry count is.
        got = parse_wireless(open("/proc/net/wireless").read())
        out["level_dbm"], out["retries"] = got["level_dbm"], got["retries"]
        out["device"] = device or got["device"]
    except Exception:
        # macOS has no /proc. The retry counter is a Linux-only reading; the rest of the health
        # panel still works, so this says so once and stays quiet after that.
        global _wireless_said
        if not _wireless_said:
            _wireless_said = True
            log.info("no /proc/net/wireless here (macOS); the wifi retry count is unavailable")
    return out


def count_disconnects(text):
    """How many times NetworkManager said the link went down, in one boot's worth of its log."""
    return sum(1 for line in (text or "").splitlines()
               if "state is now DISCONNECTED" in line
               or "state change: ip-config -> failed" in line)


def drops(device=None):
    """How often the link has gone down since this laptop booted.

    Two sources because they answer slightly different questions. `carrier_changes` counts every
    up and down transition, so a single disconnection is two, and it is free. NetworkManager's log
    says how many times it actually reported a disconnection, which is the number a person means.
    """
    out = {"carrier_changes": None, "disconnects": None}
    if device:
        try:
            out["carrier_changes"] = int(open(f"/sys/class/net/{device}/carrier_changes").read())
        except Exception:
            pass
    try:
        out["disconnects"] = count_disconnects(
            _run(["journalctl", "-b", "-u", "NetworkManager", "--no-pager", "-q"], timeout=8.0))
    except Exception as exc:
        # Readable without root on this laptop, but a machine where it is not should say nothing
        # rather than nought: nought means "it never dropped" and that is a different claim.
        log.info("cannot read the NetworkManager log: %s", exc)
    return out


def cached():
    """The last snapshot, if one was taken recently enough to still mean anything.

    For callers that want the verdict but must not pay for it. `snapshot` costs about half a
    second when the cache is cold, and the voice front desk is a person waiting on a phone, so it
    asks here and says the network has not been measured when the answer is None.
    """
    with _lock:
        if _cached is not None and (time.time() - _cached_at) < 60.0:
            return _cached
    return None


def snapshot(max_age=MIN_AGE):
    """The pairs, taken together and shared. Costs about half a second when it is not cached."""
    global _cached, _cached_at
    with _lock:
        if _cached is not None and (time.time() - _cached_at) < max_age:
            return _cached

    router = gateway()
    both = {}

    def measure(name, host):
        both[name] = latency(host)

    # Side by side in the wall clock as well as on the screen. Measured a second apart, a spike that
    # hit one and not the other would read as a difference between the two hops.
    threads = [threading.Thread(target=measure, args=("gateway", router["address"])),
               threading.Thread(target=measure, args=("internet", INTERNET))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=8)

    link = wifi(router["device"])
    out = {
        "at": time.time(),
        "gateway": router,
        "latency": {"gateway": both.get("gateway"), "internet": both.get("internet")},
        "wifi": link,
        "drops": drops(router["device"]),
        # Filled in by a button, never by a timer.
        "speed": None,
    }
    out["verdict"] = verdict(out)
    with _lock:
        _cached, _cached_at = out, time.time()
    return out


def verdict(snap):
    """One line naming the hop, because the pair is only useful to somebody who reads it as a pair.

    The thresholds are this laptop's own history. A healthy evening here is about 1ms to the router;
    the evening the router is dying it is 100ms with the internet figure unchanged, which is what the
    first branch catches.
    """
    g = (snap.get("latency") or {}).get("gateway") or {}
    i = (snap.get("latency") or {}).get("internet") or {}
    ga, ia = g.get("avg_ms"), i.get("avg_ms")
    if g.get("loss_pct") == 100.0:
        return "the router is not answering at all"
    if ga is None:
        return "no measurement"
    if ga > 30:
        return f"the router is the problem: {ga:.0f}ms to reach it in the hall"
    if ia is None or i.get("loss_pct", 0) > 20:
        return f"the router is fine ({ga:.0f}ms) and the line beyond it is not"
    if ia > 120:
        return f"the line is slow: {ia:.0f}ms out, {ga:.0f}ms to the router"
    retries = (snap.get("wifi") or {}).get("retries")
    if retries and retries > 5000:
        return f"looks fine, but {retries} wifi retries since boot is high"
    return f"fine: {ga:.0f}ms to the router, {ia:.0f}ms out"


def speed(down_bytes=4_000_000, up_bytes=1_000_000, timeout=30.0):
    """Throughput both ways. Costs bandwidth, so nothing calls this on a timer."""
    out = {"down_mbit": None, "up_mbit": None, "at": time.time(), "error": None}
    try:
        started = time.time()
        req = urllib.request.Request(DOWN.format(bytes=down_bytes), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            got = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                got += len(chunk)
        seconds = max(time.time() - started, 0.001)
        out["down_mbit"] = round(got * 8 / seconds / 1e6, 1)

        started = time.time()
        req = urllib.request.Request(UP, data=b"x" * up_bytes, method="POST",
                                     headers={"User-Agent": UA,
                                              "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        seconds = max(time.time() - started, 0.001)
        out["up_mbit"] = round(up_bytes * 8 / seconds / 1e6, 1)
    except Exception as exc:
        out["error"] = str(exc)
        log.warning("speed test failed: %s", exc)
    log.info("speed test: %s down, %s up Mbit/s", out["down_mbit"], out["up_mbit"])
    with _lock:
        if _cached is not None:
            _cached["speed"] = out
    return out


def reset():
    """Forget the cached measurement. For tests."""
    global _cached, _cached_at
    with _lock:
        _cached, _cached_at = None, 0.0

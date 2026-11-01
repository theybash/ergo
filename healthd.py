#!/usr/bin/env python3
"""Laptop health telemetry for the console.

The first piece of the platform, and deliberately the simplest thing that exercises every
layer: collector on the laptop -> Tailscale -> a reverse proxy -> auth -> browser -> live
updates. If this works end to end, agents plug into the same rails.

Endpoints:
    GET /health          one JSON snapshot
    GET /stream          server-sent events, a snapshot every INTERVAL seconds
    GET /                a self-contained page, so it is testable before any dashboard exists

Auth is a bearer token, checked on every request. Not because the token is strong security
on its own, but because this box is reachable over Tailscale and will later be behind a
public proxy, and an unauthenticated endpoint that enumerates your processes is a gift.

Stdlib plus psutil only. No framework: fewer moving parts to keep running for years.
"""

import json
import os
import re
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

PORT = int(os.environ.get("HEALTHD_PORT", "8090"))
BIND = os.environ.get("HEALTHD_BIND", "0.0.0.0")
TOKEN = os.environ.get("HEALTHD_TOKEN", "")
INTERVAL = float(os.environ.get("HEALTHD_INTERVAL", "2"))

BOOT = psutil.boot_time()

# Claude Desktop is Electron and spawns a couple of dozen helper processes, so a naive
# search for "claude" reports thirty agents when there are two. The CLI's comm is exactly
# "claude"; Desktop's is "claude-desktop". Match on that rather than the executable path,
# because /proc/<pid>/comm is world readable while /proc/<pid>/exe is not: reading exe for
# another user's process returns AccessDenied, which would hide every agent but your own.
AGENT_COMM = "claude"

# Said once, on the first poll that finds no pressure files. Linux-only reading.
_pressure_said = False


def _run(cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def temperatures():
    out = {}
    try:
        for name, entries in (psutil.sensors_temperatures() or {}).items():
            for e in entries:
                if e.current:
                    out[e.label or name] = round(e.current, 1)
    except Exception:
        pass
    return out


def battery():
    try:
        b = psutil.sensors_battery()
        if not b:
            return None
        return {
            "percent": round(b.percent, 1),
            "plugged": b.power_plugged,
            # secsleft is a sentinel when plugged in or still being calculated.
            "minutes_left": (round(b.secsleft / 60) if b.secsleft and b.secsleft > 0 else None),
        }
    except Exception:
        return None


def disks():
    out = []
    for p in psutil.disk_partitions(all=False):
        # Skip the pseudo filesystems; only report things a human would recognise as a drive.
        if not p.mountpoint.startswith(("/", "/media")):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        out.append({
            "mount": p.mountpoint,
            "fstype": p.fstype,
            "total_gb": round(u.total / 1024**3, 1),
            "used_gb": round(u.used / 1024**3, 1),
            "free_gb": round(u.free / 1024**3, 1),
            "percent": u.percent,
        })
    return out


def sessions():
    """Who is logged in, and how. Distinguishes the physical laptop from remote desktops,
    which is the difference between 'someone is sitting there' and 'someone dialled in'."""
    out = []
    raw = _run(["loginctl", "list-sessions", "--no-legend"])
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        sid = parts[0]
        # Parse KEY=VALUE rather than using --value: systemd emits properties in its own
        # order, not the order they were requested, so positional parsing silently
        # mismatches fields.
        detail = _run(["loginctl", "show-session", sid,
                       "-p", "Name", "-p", "Seat", "-p", "Display",
                       "-p", "State", "-p", "Remote"])
        kv = {}
        for line in detail.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                kv[k.strip()] = v.strip()
        out.append({
            "id": sid,
            "user": kv.get("Name", ""),
            "where": "laptop" if kv.get("Seat") else "remote",
            "display": kv.get("Display", ""),
            "state": kv.get("State", ""),
        })
    return out


def agents():
    """Running Claude Code processes, which is what the platform will call agents.

    Works across users. The working directory is the useful label (it says which project the
    agent is on) but reading it needs permission, so it degrades to None for other users
    rather than dropping the agent from the list."""
    out = []
    for p in psutil.process_iter(["pid", "name", "username", "create_time", "memory_info"]):
        try:
            if p.info.get("name") != AGENT_COMM:
                continue
            try:
                cwd = p.cwd()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                cwd = None
            out.append({
                "pid": p.info["pid"],
                "user": p.info["username"],
                "rss_mb": round(p.info["memory_info"].rss / 1024**2),
                "uptime_s": round(time.time() - p.info["create_time"]),
                "cwd": cwd,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return sorted(out, key=lambda a: -a["rss_mb"])


def network():
    out = {"tailscale_ip": None, "lan_ip": None, "interfaces": []}
    for name, addrs in psutil.net_if_addrs().items():
        if name == "lo":
            continue
        for a in addrs:
            if a.family == socket.AF_INET:
                out["interfaces"].append({"name": name, "ip": a.address})
                if name == "tailscale0":
                    out["tailscale_ip"] = a.address
                elif a.address.startswith(("192.168.", "10.", "172.")):
                    out["lan_ip"] = a.address
    return out


def snapshot():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    load1, load5, load15 = os.getloadavg()
    return {
        "ts": time.time(),
        "host": socket.gethostname(),
        "uptime_s": round(time.time() - BOOT),
        "cpu": {
            "cores": psutil.cpu_count(logical=True),
            # interval=None means "since the last call", which is what you want when polling.
            "percent": psutil.cpu_percent(interval=None),
            "load": [round(load1, 2), round(load5, 2), round(load15, 2)],
        },
        "memory": {
            "total_gb": round(vm.total / 1024**3, 2),
            "available_gb": round(vm.available / 1024**3, 2),
            "used_gb": round((vm.total - vm.available) / 1024**3, 2),
            "percent": vm.percent,
            "swap_total_gb": round(sw.total / 1024**3, 2),
            "swap_used_gb": round(sw.used / 1024**3, 2),
        },
        # PSI is the honest answer to "is memory actually a problem": it measures stalls
        # rather than guessing from a percentage.
        "pressure": _read_pressure(),
        "temps_c": temperatures(),
        "battery": battery(),
        "disks": disks(),
        "network": network(),
        "sessions": sessions(),
        "agents": agents(),
    }


def _read_pressure():
    global _pressure_said
    out = {}
    for kind in ("cpu", "memory", "io"):
        try:
            with open(f"/proc/pressure/{kind}") as fh:
                for line in fh:
                    if line.startswith("some"):
                        m = re.search(r"avg10=([\d.]+)", line)
                        if m:
                            out[kind] = float(m.group(1))
        except FileNotFoundError:
            # macOS has no pressure stall information. The rest of the snapshot stands.
            if not _pressure_said:
                _pressure_said = True
                print("healthd: no /proc/pressure here (macOS); pressure readings are omitted")
        except Exception:
            pass
    return out


PAGE = """<!doctype html><meta charset=utf-8><title>laptop</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
@font-face{font-family:"Instrument Serif";src:url(/fonts/instrument-serif-400.woff2)format("woff2");font-weight:400;font-display:swap}
@font-face{font-family:"Inter";src:url(/fonts/inter-400.woff2)format("woff2");font-weight:400;font-display:swap}
@font-face{font-family:"Inter";src:url(/fonts/inter-500.woff2)format("woff2");font-weight:500;font-display:swap}
@font-face{font-family:"JetBrains Mono";src:url(/fonts/jetbrains-mono-400.woff2)format("woff2");font-weight:400;font-display:swap}

/* Light is the default and lives on bare :root, so no colour exists only inside
   a media query. Dark is layered twice: once for the system preference, once for an
   explicit choice, so the toggle wins in both directions. */
:root{
  --bg:#ffffff; --surface:#ffffff; --border:#e7e5e4; --border-hi:#d6d3d1;
  --text:#1c1917; --dim:#78716c; --faint:#a8a29e;
  --accent:#155e75; --accent-bg:#f2f8fa;
  --up:#4d7c0f; --down:#b91c1c; --wait:#b45309;
  /* One red for both themes: 4.42:1 on white, 4.36:1 on the dark ground, so it does not
     need a per-mode variant the way the old #b91c1c did (2.98:1 on dark, which failed). */
  --low:#cc4b61;
  --track:#f0efee;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme=light]){
    --bg:#0f0e0d; --surface:transparent; --border:#292524; --border-hi:#44403c;
    --text:#e8e3dc; --dim:#a8a29e; --faint:#78716c;
    --accent:#ea580c; --accent-bg:#1c1310;
    --track:#211e1c;
  }
}
:root[data-theme=dark]{
  --bg:#0f0e0d; --surface:transparent; --border:#292524; --border-hi:#44403c;
  --text:#e8e3dc; --dim:#a8a29e; --faint:#78716c;
  --accent:#ea580c; --accent-bg:#1c1310;
  --track:#211e1c;
}

*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);margin:0;padding:40px;
  font:400 13px/1.55 "Inter",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.num{font-family:"JetBrains Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}

header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:40px}
h1{font:400 44px/1 "Instrument Serif",Georgia,serif;margin:0 0 8px;letter-spacing:-.01em}
.sub{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}

.status{display:flex;align-items:center;gap:16px}
.pill{display:flex;align-items:baseline;gap:6px}
.pill b{font:400 15px/1 "JetBrains Mono",monospace;font-variant-numeric:tabular-nums;color:var(--text)}
.pill span{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
button{background:none;border:1px solid var(--border);border-radius:4px;color:var(--dim);
  width:28px;height:28px;display:grid;place-items:center;cursor:pointer;padding:0}
button:hover{border-color:var(--border-hi);color:var(--text)}
svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:1.5}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:16px}
.card{border:1px solid var(--border);border-radius:4px;padding:16px;background:var(--surface)}
.label{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-bottom:12px}
.big{font:400 28px/1 "Instrument Serif",Georgia,serif;font-variant-numeric:tabular-nums}
.unit{font:400 11px/1 "Inter",sans-serif;color:var(--faint);letter-spacing:.04em;margin-left:4px}
.bar{height:3px;background:var(--track);border-radius:2px;margin:12px 0;overflow:hidden}
.bar>i{display:block;height:100%;background:var(--accent)}
/* A value in trouble turns red, in the number and the bar together. One signal, two
   places, so it is visible whether you are reading the figure or scanning the shapes. */
.low{color:var(--low)}
.bar.low>i{background:var(--low)}
table{width:100%;border-collapse:collapse;margin-top:12px}
td{padding:3px 0;font-size:13px}
td:first-child{color:var(--dim)}
td:last-child{text-align:right;color:var(--text);
  font-family:"JetBrains Mono",monospace;font-variant-numeric:tabular-nums;font-size:12px}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:8px;vertical-align:middle}
.empty{color:var(--faint);font-size:13px}
</style>

<header>
  <div>
    <h1>laptop</h1>
    <div class=sub id=sub></div>
  </div>
  <div class=status>
    <div class=pill><b id=savail>-</b><span>GB free</span></div>
    <div class=pill><b id=scpu>-</b><span>% cpu</span></div>
    <button id=theme title="light, dark, system"></button>
  </div>
</header>
<div class=grid id=g></div>

<script>
const ICON={
  system:'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="6"/><path d="M8 2a6 6 0 000 12z" fill="currentColor" stroke="none"/></svg>',
  light:'<svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="3.2"/><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3 3l1.1 1.1M11.9 11.9L13 13M13 3l-1.1 1.1M4.1 11.9L3 13"/></svg>',
  dark:'<svg viewBox="0 0 16 16"><path d="M13.5 9.5A6 6 0 016.5 2.5a6 6 0 107 7z"/></svg>'
};
const order=['system','light','dark'];
let mode=localStorage.getItem('theme')||'system';
function applyTheme(){
  if(mode==='system') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme',mode);
  document.getElementById('theme').innerHTML=ICON[mode];
  localStorage.setItem('theme',mode);
}
document.getElementById('theme').onclick=()=>{mode=order[(order.indexOf(mode)+1)%3];applyTheme()};
applyTheme();

const g=document.getElementById('g');

// Gathered here so the numbers can be argued with in one place instead of being buried
// in the rendering. Anything past these turns red.
const LIMIT={memAvailGb:1.5, batteryPct:20, diskFreePct:10, pressure:1.0};

const bar=(v,low)=>`<div class="bar${low?' low':''}"><i style="width:${Math.min(100,v)}%"></i></div>`;
const big=(v,u,low)=>`<div class="big${low?' low':''}">${v}<span class=unit>${u}</span></div>`;
const rows=o=>'<table>'+Object.entries(o).map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('')+'</table>';
const card=(l,b)=>`<div class=card><div class=label>${l}</div>${b}</div>`;

const es=new EventSource('/stream'+location.search);
es.onmessage=e=>{
  const d=JSON.parse(e.data);
  document.getElementById('sub').textContent=d.host.toLowerCase()+' · up '+(d.uptime_s/3600).toFixed(1)+'h';
  document.getElementById('savail').textContent=d.memory.available_gb;
  document.getElementById('scpu').textContent=Math.round(d.cpu.percent);

  const c=[];
  const memLow=d.memory.available_gb<LIMIT.memAvailGb;
  c.push(card('memory',big(d.memory.available_gb,'GB available',memLow)+bar(d.memory.percent,memLow)+
    rows({used:d.memory.used_gb+' GB',total:d.memory.total_gb+' GB',
          swap:d.memory.swap_used_gb+' / '+d.memory.swap_total_gb+' GB'})));

  // No red for a busy processor. Being busy is what it is for, not a fault.
  c.push(card('processor',big(Math.round(d.cpu.percent),'% busy')+bar(d.cpu.percent)+
    rows({cores:d.cpu.cores,load:d.cpu.load.join('  ')})));

  if(d.battery){
    const batLow=!d.battery.plugged&&d.battery.percent<LIMIT.batteryPct;
    c.push(card('battery',big(Math.round(d.battery.percent),'%',batLow)+bar(d.battery.percent,batLow)+
      rows({state:d.battery.plugged?'charging':'on battery',
            remaining:d.battery.minutes_left?Math.floor(d.battery.minutes_left/60)+'h '+(d.battery.minutes_left%60)+'m':'-'})));
  }

  const t=Object.entries(d.temps_c);
  const hot=t.length?Math.max(...t.map(x=>x[1])):0;
  c.push(card('temperature',big(hot,'°C peak')+
    rows(Object.fromEntries(t.filter(x=>!/^Core/.test(x[0])).slice(0,4).map(([k,v])=>[k,v+' °C'])))));

  const root=d.disks.find(x=>x.mount==='/');
  const diskLow=root&&(100-root.percent)<LIMIT.diskFreePct;
  c.push(card('storage',big(root?root.free_gb:'-','GB free on /',diskLow)+bar(root?root.percent:0,diskLow)+
    rows(Object.fromEntries(d.disks.filter(x=>x.mount!=='/'&&x.total_gb>1)
      .map(x=>[x.mount.split('/').pop().slice(0,18),x.free_gb+' GB'])))));

  const presLow=(d.pressure.memory??0)>LIMIT.pressure;
  c.push(card('pressure',big(d.pressure.memory!==undefined?d.pressure.memory.toFixed(2):'-','memory stalls, avg10',presLow)+
    rows({cpu:(d.pressure.cpu??'-'),io:(d.pressure.io??'-')})));

  c.push(card('sessions',big(d.sessions.length,d.sessions.length===1?'session':'sessions')+
    rows(Object.fromEntries(d.sessions.map(s=>
      [`<span class=dot style="background:${s.state==='active'?'var(--up)':'var(--faint)'}"></span>${s.user}`,s.where])))));

  c.push(card('agents',big(d.agents.length,d.agents.length===1?'running':'running')+
    (d.agents.length?rows(Object.fromEntries(d.agents.map(a=>
      [a.cwd?a.cwd.split('/').pop():a.user,a.rss_mb+' MB']))):'<div class=empty>nothing running yet</div>')));

  c.push(card('network',rows({tailscale:d.network.tailscale_ip||'-',lan:d.network.lan_ip||'-'})));

  g.innerHTML=c.join('');
};
</script>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _authed(self):
        if not TOKEN:
            return True
        hdr = self.headers.get("Authorization", "")
        if hdr == f"Bearer {TOKEN}":
            return True
        # Query param too, because EventSource cannot set headers.
        return f"token={TOKEN}" in (self.path.split("?", 1)[-1] if "?" in self.path else "")

    def _deny(self):
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # Fonts are served before the auth check on purpose: a browser does not attach an
        # Authorization header or a query token to font requests, so a gated font 401s and
        # the page silently falls back to system faces. They are not secret.
        if path.startswith("/fonts/") and path.endswith(".woff2"):
            here = os.path.dirname(os.path.abspath(__file__))
            fp = os.path.join(here, "fonts", os.path.basename(path))
            if os.path.isfile(fp):
                body = open(fp, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "font/woff2")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if not self._authed():
            return self._deny()

        if path == "/health":
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        elif path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            # Without this, nginx or Caddy may buffer the stream into uselessness.
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(snapshot())
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(INTERVAL)
            except (BrokenPipeError, ConnectionResetError):
                pass

        elif path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # the default logger writes a line per SSE poll, which is unusable


if __name__ == "__main__":
    psutil.cpu_percent(interval=None)  # prime the counter so the first reading is not 0
    print(f"healthd on http://{BIND}:{PORT}  token={'set' if TOKEN else 'NONE (open)'}")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()

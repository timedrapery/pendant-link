#!/usr/bin/env python3
"""Browse your pendant days from your phone or computer, served by the Pi on your home network.

Pages: your days, one day's transcript (tap a line to hear it), search, and starred moments.
Only Python's standard library, so nothing to install. Protected by a password, because
transcripts hold other people's words too.

Run by pendant-web.service. Settings (environment, or ~/.config/pendant/env):
    PENDANT_WEB_PASSWORD   required; install-web.sh makes one
    PENDANT_WEB_PORT       default 8765
    PENDANT_ARCHIVE        default ~/recordings
    PENDANT_BIN            the pendant command, used to rebuild a tidied WAV
"""

import base64
import hmac
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FILE_RE = re.compile(r"^pendant_\d{8}_\d{6}(_r\d+)?$")
LINE_RE = re.compile(r"^(★ )?\[(\d{2}:\d{2}:\d{2})\] (.*)$")
NO_SPEECH = "(no speech found)"
GAP_MINUTES = 10  # a pause this long starts a new conversation on the day page


def setting(name, default=None):
    if os.environ.get(name):
        return os.environ[name]
    env = Path.home() / ".config" / "pendant" / "env"
    try:
        for line in env.read_text().splitlines():
            if line.startswith(name + "="):
                value = line.split("=", 1)[1].strip()
                if value:
                    default = value
    except OSError:
        pass
    return default


def archive():
    return Path(setting("PENDANT_ARCHIVE", str(Path.home() / "recordings")))


# ---------- reading the archive ----------

def list_days(root: Path):
    """[(day, recordings, lines, stars)] newest first."""
    out = []
    for d in sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"), reverse=True):
        if not (d.is_dir() and DAY_RE.match(d.name)):
            continue
        lines = day_lines(root, d.name)
        recs = len({p.name.split(".")[0] for p in d.glob("pendant_*.opus")})
        out.append((d.name, recs, len(lines), sum(1 for l in lines if l["starred"])))
    return out


def _recording_lines(day_dir: Path, base: str):
    """Lines for one recording, from .words.json (exact times) or the .txt."""
    words = day_dir / f"{base}.words.json"
    if words.exists():
        try:
            data = json.loads(words.read_text())
            return [{
                "file": base, "start": float(s.get("start", 0)), "at": s.get("at"),
                "time": (s.get("at") or "")[11:19], "text": s.get("text", ""),
                "starred": bool(s.get("starred")),
            } for s in data.get("segments", []) if s.get("text")]
        except (OSError, ValueError):
            pass
    txt = day_dir / f"{base}.txt"
    lines = []
    if txt.exists():
        for raw in txt.read_text().splitlines():
            m = LINE_RE.match(raw)
            if m:
                lines.append({"file": base, "start": None, "at": None, "time": m.group(2),
                              "text": m.group(3), "starred": bool(m.group(1))})
    return lines


def day_lines(root: Path, day: str):
    """Every transcript line of a day, in clock order."""
    d = root / day
    bases = sorted({p.name.split(".")[0] for p in d.glob("pendant_*.txt")} |
                   {p.name.split(".")[0] for p in d.glob("pendant_*.words.json")})
    lines = []
    for base in bases:
        if FILE_RE.match(base):
            lines.extend(_recording_lines(d, base))
    lines.sort(key=lambda l: (l["time"], l["file"], l["start"] or 0))
    for i, l in enumerate(lines):
        l["id"] = f"l{i}"
    return lines


def untranscribed(root: Path, day: str):
    d = root / day
    return sorted(p.stem for p in d.glob("pendant_*.opus")
                  if FILE_RE.match(p.stem) and not (d / f"{p.stem}.txt").exists())


def conversations(lines):
    """Split a day's lines where there's a pause of GAP_MINUTES or more."""
    groups, last = [], None
    for l in lines:
        t = datetime.strptime(l["time"], "%H:%M:%S") if l["time"] else None
        if not groups or (t and last and (t - last).total_seconds() >= GAP_MINUTES * 60):
            groups.append([])
        groups[-1].append(l)
        last = t or last
    return groups


def search(root: Path, query: str, limit=200):
    q = query.lower().strip()
    hits = []
    if not q:
        return hits
    for day, *_ in list_days(root):
        for l in day_lines(root, day):
            if q in l["text"].lower():
                hits.append((day, l))
                if len(hits) >= limit:
                    return hits
    return hits


def starred(root: Path):
    out = []
    for day, *_ in list_days(root):
        out.extend((day, l) for l in day_lines(root, day) if l["starred"])
    return out


# ---------- pages ----------

CSS = """
:root{--bg:#f7f5f0;--card:#fff;--ink:#22201c;--muted:#6d675e;--line:#e6e1d8;--star:#b7791f;--starbg:#fbf3e4;--accent:#2f5d50}
@media (prefers-color-scheme:dark){:root{--bg:#161513;--card:#1f1d1a;--ink:#ece8e1;--muted:#a39d93;--line:#34312c;--star:#e0a84a;--starbg:#2a2318;--accent:#7fb8a6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px 16px 120px}
header{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:8px 0 20px}
header a.home{font-weight:700;color:var(--ink);text-decoration:none;font-size:20px;margin-right:auto}
nav a{color:var(--accent);text-decoration:none;margin-left:14px}
form.search{display:flex;gap:8px;width:100%;min-width:0}
input[type=search]{flex:1;min-width:0;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink)}
button{font:inherit;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
h1{font-size:24px;margin:0 0 4px}h2{font-size:14px;color:var(--muted);font-weight:600;letter-spacing:.04em;text-transform:uppercase;margin:28px 0 8px}
.muted{color:var(--muted)}
ul.days li a span.muted{white-space:nowrap;flex-shrink:0}
ul.days{list-style:none;padding:0;margin:0}
ul.days li a{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:14px 16px;background:var(--card);border:1px solid var(--line);border-radius:12px;margin-bottom:8px;color:var(--ink);text-decoration:none}
.line{display:grid;grid-template-columns:96px 1fr;gap:10px;padding:8px 10px;border-radius:10px;cursor:pointer}
.line:hover{background:var(--card)}
.line time{white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums;font-size:15px;padding-top:2px}
.line.star{background:var(--starbg)}.line.star time{color:var(--star);font-weight:600}
.line.star time::before{content:"★ "}
.line.playing{outline:2px solid var(--accent)}
.line.noaudio{cursor:default}
.hit .day{display:block;font-size:14px;color:var(--accent);text-decoration:none}
#player{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 16px;display:none;gap:10px;align-items:center}
#player.on{display:flex}#player audio{flex:1;width:100%}
.note{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:12px 0}
"""

JS = """
const player=document.getElementById('player'),audio=player&&player.querySelector('audio');
let current=null;
document.querySelectorAll('.line[data-src]').forEach(el=>el.addEventListener('click',()=>{
  if(current)current.classList.remove('playing');current=el;el.classList.add('playing');
  const src=el.dataset.src,start=parseFloat(el.dataset.start||'0');
  player.classList.add('on');
  const go=()=>{audio.currentTime=Math.max(start-0.5,0);audio.play();};
  if(audio.getAttribute('src')!==src){audio.setAttribute('src',src);audio.addEventListener('loadedmetadata',go,{once:true});audio.load();}else go();
}));
document.querySelectorAll('form.rebuild').forEach(f=>f.addEventListener('submit',()=>{f.querySelector('button').textContent='Rebuilding…';}));
"""


def page(title, body, player=False):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>
<header><a class="home" href="/">Pendant</a><nav><a href="/stars">★ Stars</a></nav>
<form class="search" action="/search"><input type="search" name="q" placeholder="Search your days" aria-label="Search"><button>Search</button></form></header>
{body}</main>
{'<div id="player"><audio controls preload="metadata"></audio></div>' if player else ''}
<script>{JS}</script></body></html>"""


def nice_day(day):
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%A, %B %-d, %Y")
    except ValueError:
        return day


def line_html(root, day, l, with_day=False):
    wav = root / day / f"{l['file']}.wav"
    attrs = ""
    cls = "line" + (" star" if l["starred"] else "")
    if wav.exists() and l["start"] is not None:
        attrs = f' data-src="/audio/{day}/{l["file"]}.wav" data-start="{l["start"]}"'
    else:
        cls += " noaudio"
    day_link = f'<a class="day" href="/day/{day}#{l["id"]}">{html.escape(nice_day(day))}</a>' if with_day else ""
    return (f'<div class="{cls}{" hit" if with_day else ""}" id="{l["id"]}"{attrs}>'
            f'<time>{html.escape(l["time"])}</time><div>{day_link}{html.escape(l["text"])}</div></div>')


def home_page(root):
    days = list_days(root)
    if not days:
        return page("Pendant", "<h1>Your days</h1><p class='muted'>Nothing here yet. Recordings appear after the first sync.</p>")
    items = "".join(
        f'<li><a href="/day/{d}"><span>{html.escape(nice_day(d))}</span>'
        f'<span class="muted">{n} line{"s" if n != 1 else ""}{f" · ★ {s}" if s else ""}</span></a></li>'
        for d, _, n, s in days)
    return page("Pendant", f"<h1>Your days</h1><ul class='days'>{items}</ul>")


def day_page(root, day):
    lines = day_lines(root, day)
    missing = sorted({l["file"] for l in lines if l["start"] is not None
                      and not (root / day / f"{l['file']}.wav").exists()
                      and (root / day / f"{l['file']}.opus").exists()})
    body = f"<h1>{html.escape(nice_day(day))}</h1>"
    if missing:
        forms = "".join(
            f'<form class="rebuild" method="post" action="/rebuild/{day}/{f}"><button>Rebuild audio for {f[17:19]}:{f[19:21]}</button></form>'
            for f in missing)
        body += f"<div class='note'>Some audio from this day was tidied away to save space. Rebuild it to listen again (a few seconds each).{forms}</div>"
    if not lines:
        body += "<p class='muted'>No speech transcribed for this day.</p>"
    for group in conversations(lines):
        body += f"<h2>{html.escape(group[0]['time'][:5])}</h2>" + "".join(line_html(root, day, l) for l in group)
    waiting = untranscribed(root, day)
    if waiting:
        body += f"<p class='muted'>{len(waiting)} recording(s) not transcribed yet; they'll appear after tonight's run.</p>"
    return page(nice_day(day), body, player=True)


def search_page(root, q):
    hits = search(root, q)
    body = f"<h1>Search</h1><p class='muted'>{len(hits)} line{'s' if len(hits) != 1 else ''} with “{html.escape(q)}”</p>"
    body += "".join(line_html(root, d, l, with_day=True) for d, l in hits)
    return page(f"Search: {q}", body, player=True)


def stars_page(root):
    hits = starred(root)
    body = "<h1>★ Starred moments</h1>"
    if not hits:
        body += "<p class='muted'>None yet. One quick press of the pendant button stars a moment.</p>"
    body += "".join(line_html(root, d, l, with_day=True) for d, l in hits)
    return page("Starred moments", body, player=True)


# ---------- server ----------

def check_password(header_value, password):
    if not (header_value and password and header_value.startswith("Basic ")):
        return False
    try:
        _, _, given = base64.b64decode(header_value[6:]).decode().partition(":")
    except Exception:
        return False
    return hmac.compare_digest(given.encode(), password.encode())


def parse_range(header, size):
    """(start, end) inclusive for a 'bytes=a-b' header, or None for the whole file."""
    m = re.match(r"bytes=(\d*)-(\d*)$", header or "")
    if not m or size == 0:
        return None
    a, b = m.groups()
    if a == "":
        n = int(b or 0)
        return (max(size - n, 0), size - 1) if n else None
    start = int(a)
    end = min(int(b), size - 1) if b else size - 1
    return (start, end) if start <= end else None


def from_home_network(addr: str) -> bool:
    """True for this computer and private home-network addresses (192.168.x.x, 10.x.x.x,
    fe80::, ...). The page is plain HTTP with a password, which is fine across your own
    Wi-Fi but not across the internet, so anything else is turned away even with the right
    password (say, if a router port were opened to it by mistake)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    # 100.64.0.0/10: Tailscale gives each of your devices an address here, and the link
    # between them is encrypted, so the page can be opened from your laptop or phone away
    # from home. Nothing on the internet can reach this range unless you share your tailnet.
    tailnet = ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10")
    return ip.is_loopback or ip.is_private or ip.is_link_local or tailnet


def tighten_settings(path: Path):
    """The settings file holds the page's password: keep it readable by its owner only."""
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            path.chmod(0o600)
            return True
    except OSError:
        pass
    return False


class Handler(BaseHTTPRequestHandler):
    root = None
    password = None

    def log_message(self, fmt, *args):
        pass  # keep the journal quiet; nothing about what was viewed is logged

    def _auth(self):
        if not from_home_network(self.client_address[0]):
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if check_password(self.headers.get("Authorization"), self.password):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Pendant", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _send(self, body, status=200, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _not_found(self):
        self._send(page("Not found", "<h1>Not found</h1><p><a href='/'>Back to your days</a></p>"), 404)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._auth():
            return
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        root = self.root
        if not parts:
            return self._send(home_page(root))
        if parts[0] == "day" and len(parts) == 2 and DAY_RE.match(parts[1]) and (root / parts[1]).is_dir():
            return self._send(day_page(root, parts[1]))
        if parts == ["search"]:
            return self._send(search_page(root, parse_qs(url.query).get("q", [""])[0][:200]))
        if parts == ["stars"]:
            return self._send(stars_page(root))
        if parts[0] == "audio" and len(parts) == 3 and DAY_RE.match(parts[1]) and parts[2].endswith(".wav") \
                and FILE_RE.match(parts[2][:-4]):
            return self._audio(root / parts[1] / parts[2])
        return self._not_found()

    def do_POST(self):
        if not self._auth():
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 3 and parts[0] == "rebuild" and DAY_RE.match(parts[1]) and FILE_RE.match(parts[2]):
            opus = self.root / parts[1] / f"{parts[2]}.opus"
            wav = opus.with_suffix(".wav")
            if opus.exists() and not wav.exists():
                pendant = setting("PENDANT_BIN", "pendant")
                subprocess.run([pendant, "decode", str(opus), str(wav)], capture_output=True, timeout=600)
            self.send_response(303)
            self.send_header("Location", f"/day/{parts[1]}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._not_found()

    def _audio(self, path: Path):
        if not path.is_file():
            return self._not_found()
        size = path.stat().st_size
        rng = parse_range(self.headers.get("Range"), size)
        start, end = rng if rng else (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as f:
            f.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser stopped listening, e.g. after seeking


def make_server(root: Path, password: str, host="0.0.0.0", port=8765):
    handler = type("PendantHandler", (Handler,), {"root": root, "password": password})
    return ThreadingHTTPServer((host, port), handler)


def main():
    if tighten_settings(Path.home() / ".config" / "pendant" / "env"):
        print("Made ~/.config/pendant/env readable by you only (it holds this page's password).", flush=True)
    password = setting("PENDANT_WEB_PASSWORD")
    if not password:
        sys.exit("PENDANT_WEB_PASSWORD isn't set. Run install-web.sh, which makes one.")
    port = int(setting("PENDANT_WEB_PORT", "8765"))
    server = make_server(archive(), password, host=setting("PENDANT_WEB_HOST", "0.0.0.0"), port=port)
    print(f"Serving your pendant days on port {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

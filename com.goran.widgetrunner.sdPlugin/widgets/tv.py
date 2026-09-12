"""Open on TV: sends the currently playing YouTube video to an Android TV over adb, opening it in
SmartTube (or the YouTube app when SmartTube is not installed).

The exact URL comes from the browser's history (matched by the title Windows media controls report);
if it is not there, the video is looked up on YouTube's search page. Settings: TV, adb, target app.
"""
import asyncio
import glob
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, SSL_CONTEXT, fit_text, font
from widgets.lyrics import parse_track

log = logging.getLogger("widgetrunner.tv")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

from config import TV_IP as DEFAULT_TV
DEFAULT_PORT = 5555
STATUS_SECONDS = 30
WINRT_TIMEOUT = 3.0
CREATE_NO_WINDOW = 0x08000000
ADB_CANDIDATES = [shutil.which("adb") or "", os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
                  r"C:\platform-tools\adb.exe"]
SMARTTUBE = ["org.smarttube.stable", "org.smarttube.beta", "com.teamsmart.videomanager.tv", "com.liskovsoft.smarttubetv.beta", "com.liskovsoft.videomanager"]
YOUTUBE_TV = "com.google.android.youtube.tv"
BROWSER_HISTORY = [   # newest 'History' file among these profiles is used
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data\*\History"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data\*\History"),
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data\*\History"),
]
HISTORY_MAX_AGE = 6 * 3600   # only trust history entries from the last few hours
YT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en-US,en;q=0.8", "Cookie": "CONSENT=YES+1"}

BG = (18, 20, 28)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)
OK = (70, 190, 120)
WARN = (240, 190, 60)
BAD = (230, 80, 80)


# ---------------------------------------------------------------- helpers (blocking)
def find_adb():
    for c in ADB_CANDIDATES:
        if c and os.path.exists(c):
            return c
    return ""


def run(cmd, timeout=15):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
    return (r.stdout + r.stderr).strip()


def adb_state(adb, target):
    """-> 'device' | 'unauthorized' | 'offline' | 'missing'"""
    if not adb:
        return "missing"
    out = run([adb, "connect", target], timeout=8)
    if "unable" in out.lower() or "cannot" in out.lower() or "failed to connect" in out.lower():
        return "offline"
    if "unauthorized" in out.lower() or "failed to authenticate" in out.lower():
        return "unauthorized"
    for line in run([adb, "devices"], timeout=8).splitlines():
        if line.startswith(target):
            return "device" if "\tdevice" in line or line.endswith("device") else ("unauthorized" if "unauthorized" in line else "offline")
    return "offline"


def detect_package(adb, target, prefer="auto"):
    """prefer: auto (SmartTube, else YouTube), smarttube, youtube, chooser (no package) or a package id."""
    if prefer == "chooser":
        return ""
    if prefer not in ("auto", "smarttube", "youtube"):
        return prefer                                  # explicit package id from the settings
    out = run([adb, "-s", target, "shell", "pm", "list", "packages"], timeout=15)
    installed = [pkg for pkg in SMARTTUBE if f"package:{pkg}" in out]
    if prefer in ("auto", "smarttube") and installed:
        return installed[0]
    if f"package:{YOUTUBE_TV}" in out:
        return YOUTUBE_TV
    return ""


def history_url(title):
    """Exact URL of the video the browser is playing, from its history (title match, newest first).
    The browser keeps the file locked, but a copy can be read. -> url or ''."""
    files = []
    for pattern in BROWSER_HISTORY:
        files += glob.glob(pattern)
    if not files:
        return ""
    files.sort(key=os.path.getmtime, reverse=True)
    want = title.strip().lower()
    for h in files[:3]:
        try:
            tmp = os.path.join(tempfile.gettempdir(), "widgetrunner_history.sqlite")
            shutil.copy2(h, tmp)
            con = sqlite3.connect(tmp)
            rows = con.execute("SELECT url, title, last_visit_time FROM urls WHERE url LIKE '%youtube.com/watch%' "
                               "ORDER BY last_visit_time DESC LIMIT 60").fetchall()
            con.close()
        except Exception as e:
            log.debug("history read failed for %s: %s", h, e)
            continue
        for url, t, visited in rows:
            age = time.time() - (visited / 1_000_000 - 11644473600)
            if age > HISTORY_MAX_AGE or not t:
                continue
            tl = t.lower()
            if tl.startswith(want) or want in tl:
                return clean_watch_url(url)
    return ""


def clean_watch_url(url):
    """Keep only the video id (and a start time): mix/playlist parameters make SmartTube open a list
    instead of the player."""
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url)
    if not m:
        return url
    t = re.search(r"[?&]t=(\d+)", url)
    return f"https://www.youtube.com/watch?v={m.group(1)}" + (f"&t={t.group(1)}" if t else "")


def youtube_id(title, artist=""):
    """First YouTube search result for artist + title. Titles from 'Topic' channels carry no artist,
    so the channel name (minus '- Topic', 'VEVO', ...) is added to make the search unambiguous."""
    a, t = parse_track(title, artist)
    query = f"{a} {t}".strip() or title
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers=YT_HEADERS)
    with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as r:
        html = r.read().decode("utf-8", "replace")
    # prefer the first result whose title contains the song title; else the very first result
    best = ""
    for m in re.finditer(r'"videoId":"([A-Za-z0-9_-]{11})".{0,600}?"title":\{"runs":\[\{"text":"([^"]+)"', html):
        best = best or m.group(1)
        if t.lower() in m.group(2).lower():
            return m.group(1)
    if best:
        return best
    m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    return m.group(1) if m else ""


def open_on_tv(adb, target, url, package):
    # the argument string is run by the TV's shell: quote the URL so '&' in playlist links survives
    quoted = "'" + url.replace("'", "%27") + "'"
    cmd = [adb, "-s", target, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", quoted]
    if package:
        cmd.append(package)
    out = run(cmd, timeout=15)
    ok = "Error" not in out and "Exception" not in out and "does not exist" not in out
    if not ok and package:   # package missing on the TV: let Android pick a handler
        out = run(cmd[:-1], timeout=15)
        ok = "Error" not in out and "Exception" not in out
    return ok, out


# ---------------------------------------------------------------- render
def render(state, name, title="") -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    color = {"device": OK, "unauthorized": WARN, "offline": BAD, "missing": BAD, "busy": WARN}.get(state, DIM)
    d.rounded_rectangle((cx - 40, 22, cx + 40, 74), radius=6, outline=color, width=4)
    d.line((cx - 14, 84, cx + 14, 84), fill=color, width=4)
    d.line((cx, 74, cx, 84), fill=color, width=4)
    if state == "device":
        d.polygon([(cx - 8, 38), (cx - 8, 58), (cx + 10, 48)], fill=color)
    label = {"device": name, "unauthorized": "allow on TV", "offline": "TV offline", "missing": "no adb", "busy": "sending…"}.get(state, name)
    d.text((cx, 104), label, fill=TEXT if state == "device" else color, font=fit_text(d, label, SIZE - 12, 14, "semibold"), anchor="mm")
    if title:
        d.text((cx, 126), title, fill=DIM, font=fit_text(d, title, SIZE - 12, 12, min_size=10), anchor="mm")
    return img


# ---------------------------------------------------------------- widget
class OpenOnTV(Widget):
    action = "com.goran.widgetrunner.tv"
    _manager = None
    _ids = {}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.state = "offline"
        self.package = ""
        self._busy = False

    @property
    def target(self):
        ip = (self.settings.get("tv_ip") or DEFAULT_TV).strip()
        return ip if ":" in ip else f"{ip}:{DEFAULT_PORT}"

    @property
    def name(self):
        return (self.settings.get("tv_name") or "TV").strip()

    @property
    def adb(self):
        custom = (self.settings.get("adb_path") or "").strip()
        return custom if custom and os.path.exists(custom) else find_adb()

    async def on_appear(self):
        self.every(STATUS_SECONDS, self.tick)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        self.package = ""
        await self.tick()

    @property
    def prefer(self):
        custom = (self.settings.get("package") or "").strip()
        return custom or (self.settings.get("app") or "auto").strip().lower()

    async def tick(self):
        if self._busy:
            return
        adb, target = self.adb, self.target
        self.state = await asyncio.to_thread(adb_state, adb, target)
        if self.state == "device" and not self.package:
            self.package = await asyncio.to_thread(detect_package, adb, target, self.prefer)
            log.info("tv %s: opening with %r", target, self.package or "(system chooser)")
        await self.set_image(render(self.state, self.name))

    async def current_track(self):
        """-> (title, artist) of the current media session, or ("", "")."""
        if not HAVE_WINSDK:
            return "", ""
        try:
            if OpenOnTV._manager is None:
                OpenOnTV._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
            s = OpenOnTV._manager.get_current_session()
            if s is None:
                return "", ""
            p = await asyncio.wait_for(s.try_get_media_properties_async(), WINRT_TIMEOUT)
            return p.title or "", p.artist or ""
        except Exception as e:
            log.debug("media title failed: %s", e)
            OpenOnTV._manager = None
            return "", ""

    async def on_key_down(self, payload):
        if self._busy:
            return
        self._busy = True
        try:
            title, artist = await self.current_track()
            if not title:
                await self.deck.show_alert(self.context)
                return
            await self.set_image(render("busy", self.name, title))
            # 1) the exact URL from the browser's history, 2) fall back to a YouTube search
            url = await asyncio.to_thread(history_url, title)
            source = "history"
            if not url:
                vid = OpenOnTV._ids.get((title, artist))
                if not vid:
                    vid = await asyncio.to_thread(youtube_id, title, artist)
                    OpenOnTV._ids[(title, artist)] = vid
                if not vid:
                    log.warning("no YouTube result for %r", title)
                    await self.deck.show_alert(self.context)
                    return
                url, source = f"https://www.youtube.com/watch?v={vid}", "search"
            adb, target = self.adb, self.target
            state = await asyncio.to_thread(adb_state, adb, target)
            if state != "device":
                self.state = state
                await self.deck.show_alert(self.context)
                return
            if not self.package and self.prefer != "chooser":
                self.package = await asyncio.to_thread(detect_package, adb, target, self.prefer)
            ok, out = await asyncio.to_thread(open_on_tv, adb, target, url, self.package)
            log.info("open on tv %s: %r / %r -> %s (%s): %s | %s", target, artist, title, url, source,
                     "ok" if ok else "FAILED", out.replace("\n", " ")[:120])
            await (self.deck.show_ok if ok else self.deck.show_alert)(self.context)
            self.state = "device"
        except Exception as e:
            log.warning("open on tv failed: %s", e)
            await self.deck.show_alert(self.context)
        finally:
            self._busy = False
            await self.set_image(render(self.state, self.name))

    async def on_dial_down(self, payload):
        await self.on_key_down(payload)

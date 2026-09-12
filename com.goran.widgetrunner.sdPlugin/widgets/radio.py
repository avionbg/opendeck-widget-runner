"""Internet radio through mpv (no window, controlled over its JSON IPC pipe).

Two station sources, switchable in the panel or by holding the key for a second:
  playlist - an .m3u file (EXTM3U with names, logos and group titles), optionally one group
  search   - radio-browser.info (free, no key): country / tag / name, ordered by votes
Key: press = play/stop, double press = add/remove favorite, hold = on-screen menu (stations, favorites,
source). Dial: rotate = previous/next station (or menu row), press = play/stop (or select).
Favorites live in favorites.m3u next to the plugin, a normal playlist you can use anywhere.
The key shows the station (logo as background when available) and the ICY "now playing" title.
"""
import asyncio
import atexit
import ctypes
import math
import io
import json
import logging
import msvcrt
import os
import queue
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageEnhance

from widgetlib import Widget
from widgets._draw import SIZE, SSL_CONTEXT, fit_text, font, get_json

log = logging.getLogger("widgetrunner.radio")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

from config import MPV_PATH as DEFAULT_MPV, M3U_PATH as DEFAULT_M3U, M3U_GROUP as DEFAULT_GROUP
RB_HOSTS = ["https://de1.api.radio-browser.info", "https://at1.api.radio-browser.info", "https://nl1.api.radio-browser.info"]
RB_UA = {"User-Agent": "opendeck-widget-runner/0.1"}
PIPE = r"\\.\pipe\widgetrunner-mpv"
CREATE_NO_WINDOW = 0x08000000
HOLD_SECONDS = 1.0
DOUBLE_CLICK = 0.35
MENU_IDLE = 8.0
POLL_SECONDS = 1.0
DEFAULT_FAV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "favorites.m3u")
DEFAULT_BLACKLIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blacklist.m3u")
OVERLAY_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "overlay.py")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
STAR = (240, 190, 60)
NOTICE_SECONDS = 2.5
WINRT_TIMEOUT = 3.0
PEAK_THRESHOLD = 0.01     # output meter level that counts as "something is playing"

BG = (18, 20, 28)
TEXT = (245, 246, 250)
DIM = (185, 190, 205)
ACCENT = (240, 120, 60)
OK = (70, 190, 120)
BAD = (230, 80, 80)


# ---------------------------------------------------------------- shared "now playing" for other widgets
# The SMTC-based widgets (Now Playing, Track Info) read this so they can show the current radio song
# even though mpv registers no Windows media session. Computed live from the shared mpv object, so it
# works even when no Radio key is on screen (the station name is remembered when a station is started).
CURRENT_STATION = {"name": ""}


def radio_now_playing():
    """-> {station, title, artist} while mpv is actually playing, else None."""
    if not mpv.playing():
        return None
    t = (mpv.now_playing() or "").strip()
    if " - " in t:
        artist, title = t.split(" - ", 1)
        artist, title = artist.strip(), title.strip()
    else:
        artist, title = "", t
    return {"station": CURRENT_STATION["name"], "title": title, "artist": artist}


# ---------------------------------------------------------------- station sources
def natural_key(name):
    """Case- and diacritic-insensitive natural sort key: 'Radio 2' < 'Radio 10', 'Ž' near 'Z'."""
    folded = (name or "").casefold()
    trans = str.maketrans("čćđšžàáâäèéêëìíîïòóôöùúûüñç", "ccdszaaaaeeeeiiiioooouuuunc")
    folded = folded.translate(trans)
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", folded)]


def load_m3u(path, group=""):
    """EXTM3U -> [{name, url, logo, group}], optionally only one group (case-insensitive)."""
    try:
        text = open(path, encoding="utf-8-sig", errors="replace").read()
    except OSError as e:
        log.warning("m3u not readable: %s", e)
        return []
    out, meta = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line.split(",", 1)[1].strip() if "," in line else ""
            logo = re.search(r'tvg-logo="([^"]*)"', line)
            grp = re.search(r'group-title="([^"]*)"', line)
            meta = {"name": name, "logo": (logo.group(1) if logo else ""), "group": (grp.group(1) if grp else "")}
        elif line and not line.startswith("#") and meta is not None:
            if not group or meta["group"].lower() == group.lower():
                out.append({**meta, "url": line})
            meta = None
    return out


def m3u_groups(path):
    seen = []
    for s in load_m3u(path):
        if s["group"] and s["group"] not in seen:
            seen.append(s["group"])
    return seen


def search_radio_browser(country="", tag="", name="", limit=20):
    params = {"order": "votes", "reverse": "true", "limit": str(limit), "hidebroken": "true"}
    if country:
        params["countrycode"] = country.upper()
    if tag:
        params["tag"] = tag
    if name:
        params["name"] = name
    q = urllib.parse.urlencode(params)
    last = None
    for host in RB_HOSTS:
        try:
            data = get_json(f"{host}/json/stations/search?{q}", headers=RB_UA)
            return [{"name": s["name"].strip(), "url": s.get("url_resolved") or s.get("url"), "logo": s.get("favicon") or "",
                     "group": f"{s.get('countrycode', '')} {s.get('codec', '')} {s.get('bitrate', 0)}k".strip()}
                    for s in data if s.get("url_resolved") or s.get("url")]
        except Exception as e:
            last = e
    log.warning("radio-browser search failed: %s", last)
    return []


def fetch_logo(url):
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10, context=SSL_CONTEXT) as r:
            return Image.open(io.BytesIO(r.read())).convert("RGB")
    except Exception:
        return None


# ---------------------------------------------------------------- "is something else playing?"
_smtc = {"mgr": None}


async def _playing_sessions():
    if not HAVE_WINSDK:
        return []
    try:
        if _smtc["mgr"] is None:
            _smtc["mgr"] = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
        return [s for s in _smtc["mgr"].get_sessions() if s.get_playback_info().playback_status.name == "PLAYING"]
    except Exception as e:
        log.debug("smtc check failed: %s", e)
        _smtc["mgr"] = None
        return []


def _app_name(sess):
    app = sess.source_app_user_model_id or "media"
    if "!" in app:
        app = app.split("!")[-1]
    return app.replace("_", " ")[:14]


async def other_audio_playing():
    """-> (description, sessions): what is already playing. description is '' when nothing is,
    the app name for media-control sessions, or 'audio' when only the output meter shows signal."""
    sessions = await _playing_sessions()
    if sessions:
        return _app_name(sessions[0]), sessions
    try:
        from widgets.audiolevel import AudioLevel
        peaks = AudioLevel._meter.peaks()
        if peaks and max(peaks) > PEAK_THRESHOLD and not mpv.playing():
            return "audio", []
    except Exception as e:
        log.debug("meter check failed: %s", e)
    return "", []


async def pause_sessions(sessions):
    for s in sessions:
        try:
            await asyncio.wait_for(s.try_pause_async(), WINRT_TIMEOUT)
        except Exception as e:
            log.debug("pause failed: %s", e)


# ---------------------------------------------------------------- mpv over JSON IPC
class Mpv:
    """One mpv process for the whole runner, driven over its JSON IPC named pipe.

    A Windows named pipe handle must not be read and written from two threads at once, so a single
    worker thread owns the handle: it sends queued commands and drains replies/events, using
    PeekNamedPipe to avoid blocking reads."""

    def __init__(self):
        self.proc = None
        self.pipe = None
        self.rid = 0
        self.pending = {}
        self.outbox = queue.Queue()
        self.props = {}            # observed properties: pause, idle-active, metadata, media-title
        self.events = []
        self._worker = None
        self._stop = threading.Event()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None and self.pipe is not None

    def start(self, exe):
        if self.alive():
            return True
        if not exe or not os.path.exists(exe):
            log.warning("mpv not found at %r", exe)
            return False
        # mpv.exe started without a console switches itself to "pseudo-gui" (forces a window):
        # pin the plain player mode and disable every video output explicitly.
        self.proc = subprocess.Popen([exe, "--player-operation-mode=cplayer", "--no-config", "--vo=null", "--vid=no",
                                      "--force-window=no", "--audio-display=no", "--no-terminal", "--really-quiet",
                                      "--idle=yes", "--cache=yes", "--demuxer-max-bytes=8MiB",
                                      f"--input-ipc-server={PIPE}"], creationflags=CREATE_NO_WINDOW,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.pipe = open(PIPE, "r+b", buffering=0)
                break
            except OSError:
                time.sleep(0.1)
        if self.pipe is None:
            log.warning("mpv IPC pipe did not come up")
            self.stop()
            return False
        self._stop.clear()
        self._worker = threading.Thread(target=self._io_loop, daemon=True)
        self._worker.start()
        for i, prop in enumerate(("pause", "idle-active", "metadata", "media-title", "core-idle"), 1):
            self.command("observe_property", i, prop)
        log.info("mpv started (pid %s)", self.proc.pid)
        return True

    def _available(self):
        avail = wt.DWORD(0)
        h = msvcrt.get_osfhandle(self.pipe.fileno())
        if not ctypes.windll.kernel32.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
            raise OSError("pipe closed")
        return avail.value

    def _io_loop(self):
        buf = b""
        while not self._stop.is_set() and self.pipe is not None:
            try:
                while True:   # send everything queued
                    try:
                        line = self.outbox.get_nowait()
                    except queue.Empty:
                        break
                    self.pipe.write(line)
                n = self._available()
                if n:
                    buf += self.pipe.read(n)
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        self._handle(raw)
                else:
                    time.sleep(0.02)
            except (OSError, ValueError) as e:
                log.warning("mpv pipe error: %s", e)
                break
        self.pipe = None

    def _handle(self, raw):
        try:
            msg = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return
        if "request_id" in msg:
            ev = self.pending.pop(msg["request_id"], None)
            if ev:
                ev[1] = msg
                ev[0].set()
        elif msg.get("event") == "property-change":
            self.props[msg.get("name")] = msg.get("data")
        elif msg.get("event"):
            self.events.append(msg)
            self.events = self.events[-20:]

    def command(self, *args, wait=0.0):
        if not self.alive():
            return None
        self.rid += 1
        rid = self.rid
        ev = [threading.Event(), None]
        self.pending[rid] = ev
        self.outbox.put((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode("utf-8"))
        if wait and ev[0].wait(wait):
            return ev[1]
        return None

    def play(self, url):
        self.events.clear()
        self.props["metadata"] = None
        self.props["media-title"] = None
        self.command("loadfile", url, "replace")
        self.command("set_property", "pause", False)

    def stop_playback(self):
        self.command("stop")

    def now_playing(self):
        md = self.props.get("metadata") or {}
        for k in ("icy-title", "StreamTitle", "title"):
            if md.get(k):
                return str(md[k]).strip()
        return ""

    def playing(self):
        return self.alive() and not self.props.get("idle-active", True) and not self.props.get("pause", False)

    def last_error(self):
        for ev in reversed(self.events):
            if ev.get("event") == "end-file" and ev.get("reason") == "error":
                return ev.get("file_error") or "error"
        return ""

    def stop(self):
        try:
            if self.alive():
                self.command("quit")
                time.sleep(0.2)
        except Exception:
            pass
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc, self.pipe = None, None


mpv = Mpv()
atexit.register(mpv.stop)   # no orphaned mpv when the runner exits


# ---------------------------------------------------------------- favorites (an .m3u of their own)
def load_favorites(path):
    return load_m3u(path) if os.path.exists(path) else []


def _save_list(path, stations, group):
    lines = ["#EXTM3U", f"#PLAYLIST:{group} (widget-runner radio)"]
    for s in stations:
        logo = f' tvg-logo="{s.get("logo", "")}"' if s.get("logo") else ""
        lines.append(f'#EXTINF:-1{logo} group-title="{group}", {s["name"]}')
        lines.append(s["url"])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def save_favorites(path, stations):
    _save_list(path, stations, "Favorites")


def load_blacklist(path):
    return load_m3u(path) if os.path.exists(path) else []


def blacklist_urls(path):
    return {f["url"] for f in load_blacklist(path)}


def toggle_blacklist(path, station):
    """-> True when the station is blacklisted after the call."""
    bl = load_blacklist(path)
    if any(b["url"] == station["url"] for b in bl):
        bl = [b for b in bl if b["url"] != station["url"]]
        _save_list(path, bl, "Blacklist")
        return False
    bl.append({"name": station["name"], "url": station["url"], "logo": station.get("logo", ""), "group": "Blacklist"})
    _save_list(path, bl, "Blacklist")
    return True


def is_favorite(path, station):
    return any(f["url"] == station["url"] for f in load_favorites(path))


def toggle_favorite(path, station):
    """-> True when the station is a favorite after the call."""
    favs = load_favorites(path)
    if any(f["url"] == station["url"] for f in favs):
        favs = [f for f in favs if f["url"] != station["url"]]
        save_favorites(path, favs)
        return False
    favs.append({"name": station["name"], "url": station["url"], "logo": station.get("logo", ""), "group": "Favorites"})
    save_favorites(path, favs)
    return True


# ---------------------------------------------------------------- render
def render(station, title, playing, source_label, logo=None, error="", notice="", favorite=False) -> Image.Image:
    if logo:
        w, h = logo.size
        scale = max(SIZE / w, SIZE / h)
        img = logo.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        x, y = (img.width - SIZE) // 2, (img.height - SIZE) // 2
        img = ImageEnhance.Brightness(img.crop((x, y, x + SIZE, y + SIZE))).enhance(0.35)
    else:
        img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img, "RGBA")
    for i in range(70):
        d.line((0, SIZE - 70 + i, SIZE, SIZE - 70 + i), fill=(10, 12, 18, int(170 * i / 70)))
    if playing:
        d.polygon([(10, 10), (10, 26), (24, 18)], fill=OK)
    else:
        d.rectangle((10, 10, 24, 24), fill=DIM)
    d.text((SIZE - 8, 10), source_label, fill=DIM, font=fit_text(d, source_label, 84, 12, min_size=10), anchor="ra")
    if favorite:
        star(d, 40, 18, 8, STAR)
    if not station:
        d.text((SIZE // 2, 76), "no stations", fill=DIM, font=font(14), anchor="mm")
        return img
    d.text((8, SIZE - 42), station, fill=TEXT, font=fit_text(d, station, SIZE - 16, 17, "bold", min_size=12), anchor="lm")
    sub = notice or error or title or ("playing" if playing else "stopped")
    color = ACCENT if notice else (BAD if error else DIM)
    d.text((8, SIZE - 20), sub, fill=color, font=fit_text(d, sub, SIZE - 16, 13, min_size=10), anchor="lm")
    return img


def star(d, cx, cy, r, color):
    pts = []
    for i in range(10):
        a = math.radians(-90 + i * 36)
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
    d.polygon(pts, fill=color)


# ---------------------------------------------------------------- widget
SOURCES = ["playlist", "search", "favorites"]
SOURCE_NAMES = {"playlist": "Playlist", "search": "Search", "favorites": "Favorites"}


class Radio(Widget):
    action = "com.goran.widgetrunner.radio"
    _logos = {}
    _overlay = None
    _menu = None          # {"owner": Radio, "items": [...], "index": int, "until": float}
    _live = set()         # live Radio instances; mpv stops when the last one disappears
    _stop_task = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.stations = []
        self.index = 0
        self.source = "playlist"
        self._pressed_at = None
        self._clicks = 0
        self._click_task = None
        self._hold_task = None
        self._hold_fired = False
        self._last_drawn = None
        self._notice = ("", 0.0)
        Radio._live.add(self)
        if Radio._stop_task and not Radio._stop_task.done():
            Radio._stop_task.cancel()

    # --- settings ------------------------------------------------------
    def setting(self, key, default=""):
        v = self.settings.get(key)
        return default if v in (None, "") else v

    @property
    def mpv_path(self):
        return str(self.setting("mpv_path", DEFAULT_MPV))

    @property
    def fav_path(self):
        return str(self.setting("fav_path", DEFAULT_FAV))

    @property
    def bl_path(self):
        return str(self.setting("bl_path", DEFAULT_BLACKLIST))

    @property
    def busy_action(self):
        v = str(self.setting("busy_action", "skip")).lower()
        return v if v in ("skip", "replace", "ignore") else "skip"

    def source_label(self):
        if self.source == "playlist":
            g = str(self.setting("m3u_group", DEFAULT_GROUP))
            return g[:14] if g else "playlist"
        if self.source == "favorites":
            return "favorites"
        parts = [str(self.setting("rb_country", "RS")).upper(), str(self.setting("rb_tag", "")), str(self.setting("rb_name", ""))]
        return " ".join(p for p in parts if p)[:14] or "search"

    async def load_stations(self):
        if self.source == "playlist":
            stations = await asyncio.to_thread(load_m3u, str(self.setting("m3u_path", DEFAULT_M3U)), str(self.setting("m3u_group", DEFAULT_GROUP)))
            if not stations:
                stations = await asyncio.to_thread(load_m3u, str(self.setting("m3u_path", DEFAULT_M3U)))
        elif self.source == "favorites":
            stations = await asyncio.to_thread(load_favorites, self.fav_path)
        else:
            try:
                limit = int(float(self.setting("rb_limit", 20)))
            except (TypeError, ValueError):
                limit = 20
            stations = await asyncio.to_thread(search_radio_browser, str(self.setting("rb_country", "RS")),
                                               str(self.setting("rb_tag", "")), str(self.setting("rb_name", "")), max(1, min(100, limit)))
        blocked = blacklist_urls(self.bl_path)
        if blocked:
            stations = [s for s in stations if s["url"] not in blocked]
        self.stations = sorted(stations, key=lambda s: natural_key(s["name"]))
        remembered = self.settings.get(f"last_{self.source}")
        self.index = next((i for i, s in enumerate(self.stations) if s["name"] == remembered), 0)
        log.info("radio %s: %d stations (%s)", self.source, len(self.stations), self.source_label())

    # --- lifecycle -----------------------------------------------------
    async def on_appear(self):
        # start from the last played station's source if we're going to resume it
        resume = bool(self.settings.get("resume", False)) and self.settings.get("resume_url")
        self.source = str(self.settings.get("resume_source") or self.setting("source", "playlist")) if resume \
            else str(self.setting("source", "playlist"))
        if self.source not in SOURCES:
            self.source = "playlist"
        await self.load_stations()
        # point the selection at the last played station when it is in this source
        url = self.settings.get("resume_url")
        if url:
            i = next((k for k, s in enumerate(self.stations) if s["url"] == url), None)
            if i is not None:
                self.index = i
        await self.draw()
        self.every(POLL_SECONDS, self.tick)
        if resume and self.station and not mpv.playing():
            await asyncio.sleep(0.3)          # let the first draw land before audio starts
            await self.play_station(self.station)
            await self.draw(force=True)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        new_source = str(self.setting("source", "playlist"))
        if new_source in SOURCES and new_source != self.source:
            self.source = new_source
        await self.load_stations()
        if self.menu_mine():
            await Radio.overlay_send({"config": self.menu_config()})
            await self.push_menu()
        await self.draw(force=True)

    async def on_disappear(self):
        await super().on_disappear()
        if Radio._menu and Radio._menu["owner"] is self:
            await self.close_menu()
        Radio._live.discard(self)
        if not Radio._live:
            # last radio key removed -> stop mpv, but deferred so a profile reload (rapid
            # disappear/appear) or a second radio key cancels it before it fires.
            if Radio._stop_task and not Radio._stop_task.done():
                Radio._stop_task.cancel()
            Radio._stop_task = asyncio.create_task(self._deferred_stop())

    @classmethod
    async def _deferred_stop(cls):
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return
        if not cls._live and mpv.alive():
            log.info("radio: last key removed, stopping mpv")
            await asyncio.to_thread(mpv.stop)

    @property
    def station(self):
        return self.stations[self.index] if self.stations else None

    async def tick(self):
        if Radio._menu and Radio._menu["owner"] is self and time.monotonic() > Radio._menu["until"]:
            await self.close_menu()
        await self.draw()

    async def draw(self, force=False):
        st = self.station
        playing = mpv.playing() and mpv.props.get("_station") == (st or {}).get("url")
        title = mpv.now_playing() if playing else ""
        err = mpv.last_error() if (st and mpv.props.get("_station") == st.get("url") and not playing) else ""
        logo = None
        if st and st.get("logo"):
            if st["logo"] not in Radio._logos:
                Radio._logos[st["logo"]] = await asyncio.to_thread(fetch_logo, st["logo"])
            logo = Radio._logos[st["logo"]]
        fav = bool(st) and await asyncio.to_thread(is_favorite, self.fav_path, st)
        notice = self._notice[0] if time.monotonic() < self._notice[1] else ""
        key = (st and st["name"], title, playing, self.source_label(), bool(logo), err, notice, fav)
        if force or key != self._last_drawn:
            self._last_drawn = key
            await self.set_image(render(st["name"] if st else "", title, playing, self.source_label(), logo, err, notice, fav))

    def notify(self, text):
        self._notice = (text, time.monotonic() + NOTICE_SECONDS)

    # --- playback ------------------------------------------------------
    async def play_station(self, st):
        if self.busy_action != "ignore" and not mpv.playing():
            other, sessions = await other_audio_playing()
            if other and self.busy_action == "replace" and sessions:
                await pause_sessions(sessions)
                log.info("radio: paused %s to play the radio", other)
                self.notify(f"paused {other}")
                await asyncio.sleep(0.3)
            elif other:
                log.info("radio: not starting, %s is playing", other)
                self.notify(f"{other} is playing")
                await self.deck.show_alert(self.context)
                await self.draw(force=True)
                return False
        if not await asyncio.to_thread(mpv.start, self.mpv_path):
            await self.deck.show_alert(self.context)
            return False
        mpv.props["_station"] = st["url"]
        CURRENT_STATION["name"] = st["name"]
        await asyncio.to_thread(mpv.play, st["url"])
        log.info("radio: playing %s <%s>", st["name"], st["url"])
        # remember the last station actually played, so it can be restored / resumed
        self.settings["resume_source"] = self.source
        self.settings["resume_name"] = st["name"]
        self.settings["resume_url"] = st["url"]
        await self.deck.set_settings(self.context, self.settings)
        return True

    async def toggle_play(self):
        st = self.station
        if not st:
            await self.deck.show_alert(self.context)
            return
        if mpv.playing() and mpv.props.get("_station") == st["url"]:
            await asyncio.to_thread(mpv.stop_playback)
        else:
            await self.play_station(st)
        await asyncio.sleep(0.4)
        await self.draw(force=True)

    async def set_source(self, source):
        # switch the live source for this session only; the panel's "Starting source" is left
        # untouched, so the widget still starts from what the user chose there.
        if source not in SOURCES:
            return
        self.source = source
        await self.load_stations()
        await self.draw(force=True)

    async def step(self, delta):
        if not self.stations:
            return
        self.index = (self.index + delta) % len(self.stations)
        self.settings[f"last_{self.source}"] = self.station["name"]
        await self.deck.set_settings(self.context, self.settings)
        if mpv.playing():
            mpv.props["_station"] = self.station["url"]
            await asyncio.to_thread(mpv.play, self.station["url"])
            log.info("radio: switched to %s", self.station["name"])
        await self.draw(force=True)

    async def toggle_favorite(self):
        st = self.station
        if not st:
            return
        now_fav = await asyncio.to_thread(toggle_favorite, self.fav_path, st)
        self.notify("added to favorites" if now_fav else "removed from favorites")
        log.info("radio: %s %s favorites", st["name"], "added to" if now_fav else "removed from")
        if self.source == "favorites":
            await self.load_stations()
        await self.draw(force=True)

    async def blacklist_current(self):
        st = self.station
        if not st:
            return
        now_bl = await asyncio.to_thread(toggle_blacklist, self.bl_path, st)
        self.notify("blacklisted" if now_bl else "un-blacklisted")
        log.info("radio: %s %s blacklist", st["name"], "added to" if now_bl else "removed from")
        await self.load_stations()   # a blacklisted station drops out of every source
        await self.draw(force=True)

    # --- on-screen menu (overlay process) --------------------------------
    MENU_DEFAULTS = {"width_pct": 26, "height_pct": 56, "position": "middle", "align": "right",
                     "font_pct": 1.7, "text_color": "#ffffff", "bg_color": "#0b0d14", "bg_opacity": 88}

    def menu_config(self):
        """Overlay geometry/colours for the menu, from the property inspector (menu_* keys),
        falling back to MENU_DEFAULTS."""
        cfg = dict(self.MENU_DEFAULTS)
        for key in ("width_pct", "height_pct", "position", "align", "font_pct", "text_color", "bg_color", "bg_opacity"):
            v = self.settings.get(f"menu_{key}")
            if v in (None, ""):
                continue
            if key in ("position", "align", "text_color", "bg_color"):
                cfg[key] = str(v)
            else:
                try:
                    cfg[key] = float(v)
                except (TypeError, ValueError):
                    pass
        return cfg

    @classmethod
    def overlay_start(cls):
        if cls._overlay is not None and cls._overlay.poll() is None:
            return
        exe = PYTHONW if os.path.exists(PYTHONW) else sys.executable
        errlog = open(os.path.join(os.path.dirname(OVERLAY_SCRIPT), "overlay.log"), "ab")
        cls._overlay = subprocess.Popen([exe, OVERLAY_SCRIPT], stdin=subprocess.PIPE, stdout=errlog, stderr=errlog,
                                        cwd=os.path.dirname(OVERLAY_SCRIPT), creationflags=CREATE_NO_WINDOW)
        log.info("radio menu overlay started (pid %s)", cls._overlay.pid)

    @classmethod
    async def overlay_send(cls, msg):
        p = cls._overlay
        if p is None or p.poll() is not None:
            return
        try:
            p.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
            p.stdin.flush()
        except (OSError, ValueError) as e:
            log.warning("overlay pipe broken: %s", e)
            cls._overlay = None

    def build_items(self, mode):
        """Rows for the given menu mode. 'actions' = compact action menu; 'stations' = the sorted
        station list with a Back row on top."""
        fav_urls = {f["url"] for f in load_favorites(self.fav_path)}
        if mode == "actions":
            items = [{"kind": "stations", "text": f"Stations… ({len(self.stations)})"}]
            st = self.station
            if st:
                items.append({"kind": "fav", "star": True,
                              "text": ("Remove from favorites" if st["url"] in fav_urls else "Add to favorites")})
                items.append({"kind": "blacklist", "text": "Blacklist this station"})
            for src in SOURCES:
                if src != self.source:
                    items.append({"kind": "source", "source": src, "text": f"Source: {SOURCE_NAMES[src]}"})
            bl = load_blacklist(self.bl_path)
            if bl:
                items.append({"kind": "blacklisted", "text": f"Blacklisted… ({len(bl)})"})
            items.append({"kind": "close", "text": "Close"})
            return items
        if mode == "blacklist":
            items = [{"kind": "back", "text": "‹ Back"}]
            for b in sorted(load_blacklist(self.bl_path), key=lambda x: natural_key(x["name"])):
                items.append({"kind": "unblacklist", "url": b["url"], "text": b["name"]})
            return items
        items = [{"kind": "back", "text": "‹ Back"}]
        items += [{"kind": "station", "i": i, "text": s["name"], "star": s["url"] in fav_urls,
                   "playing": mpv.playing() and mpv.props.get("_station") == s["url"]}
                  for i, s in enumerate(self.stations)]
        return items

    async def open_menu(self, mode="actions"):
        was_open = Radio._menu is not None
        if was_open and Radio._menu["owner"] is not self:
            await Radio._menu["owner"].close_menu()
        Radio.overlay_start()
        if not was_open:
            await asyncio.sleep(0.3)
        Radio._menu = {"owner": self, "mode": mode, "items": self.build_items(mode), "index": 0,
                       "until": time.monotonic() + MENU_IDLE}
        await Radio.overlay_send({"config": self.menu_config()})
        await self.push_menu()
        await Radio.overlay_send({"show": True})

    def _menu_title(self):
        m = Radio._menu
        if m and m["mode"] == "stations":
            return f"Stations · {SOURCE_NAMES[self.source]} ({len(self.stations)})"
        if m and m["mode"] == "blacklist":
            return f"Blacklisted ({len(load_blacklist(self.bl_path))})"
        return f"Radio · {SOURCE_NAMES[self.source]}"

    async def push_menu(self):
        m = Radio._menu
        await Radio.overlay_send({"menu": {"title": self._menu_title(),
                                           "items": [{k: v for k, v in it.items() if k != "i"} for it in m["items"]],
                                           "index": m["index"]}})

    async def close_menu(self):
        Radio._menu = None
        await Radio.overlay_send({"show": False})

    async def _set_mode(self, mode, index):
        m = Radio._menu
        m["mode"] = mode
        m["items"] = self.build_items(mode)
        m["index"] = max(0, min(index, len(m["items"]) - 1))
        m["until"] = time.monotonic() + MENU_IDLE
        await self.push_menu()

    async def menu_move(self, delta):
        m = Radio._menu
        m["index"] = (m["index"] + delta) % len(m["items"])
        m["until"] = time.monotonic() + MENU_IDLE
        await self.push_menu()

    async def menu_select(self):
        m = Radio._menu
        it = m["items"][m["index"]]
        kind = it["kind"]
        if kind == "station":
            self.index = it["i"]
            self.settings[f"last_{self.source}"] = self.station["name"]
            await self.deck.set_settings(self.context, self.settings)
            await self.close_menu()
            await self.play_station(self.station)
            await asyncio.sleep(0.4)
            await self.draw(force=True)
        elif kind == "stations":
            await self._set_mode("stations", 1 + self.index)   # open on the current station (row 0 is Back)
        elif kind == "back":
            await self._set_mode("actions", 0)
        elif kind == "fav":
            await self.toggle_favorite()
            await self._set_mode(m["mode"], m["index"])
        elif kind == "source":
            await self.set_source(it["source"])
            await self._set_mode("actions", 0)
        elif kind == "blacklist":
            await self.blacklist_current()
            await self._set_mode("actions", 0)
        elif kind == "blacklisted":
            await self._set_mode("blacklist", 0)
        elif kind == "unblacklist":
            await asyncio.to_thread(toggle_blacklist, self.bl_path, {"name": it["text"], "url": it["url"]})
            self.notify("un-blacklisted")
            await self.load_stations()
            await self.draw(force=True)
            if load_blacklist(self.bl_path):
                await self._set_mode("blacklist", m["index"])   # stay; _set_mode clamps the index
            else:
                await self._set_mode("actions", 0)
        else:
            await self.close_menu()

    def menu_mine(self):
        return Radio._menu is not None and Radio._menu["owner"] is self

    def menu_open(self):
        return Radio._menu is not None

    # --- gestures (shared by the key and the dial press) ---------------
    # short press = play/stop (or select while the menu is open), double press = favorite,
    # hold = open/close the menu. A hold timer fires the menu even if the device never sends an
    # "up" event (some encoders don't), so it works on both keypads and knobs.
    async def _press_down(self, payload):
        log.info("radio gesture: DOWN (%s)", self.context)
        self._pressed_at = time.monotonic()
        self._hold_fired = False
        if self._hold_task and not self._hold_task.done():
            self._hold_task.cancel()
        self._hold_task = asyncio.create_task(self._hold_watch())

    async def _hold_watch(self):
        try:
            await asyncio.sleep(HOLD_SECONDS)
        except asyncio.CancelledError:
            return
        self._hold_fired = True
        self._pressed_at = None
        if self._click_task and not self._click_task.done():
            self._click_task.cancel()
        self._clicks = 0
        log.info("radio gesture: HOLD -> menu")
        if self.menu_mine():
            await self.close_menu()
        else:
            await self.open_menu()

    async def _press_up(self, payload):
        log.info("radio gesture: UP (%s)", self.context)
        if self._hold_task and not self._hold_task.done():
            self._hold_task.cancel()
        if self._hold_fired:
            self._hold_fired = False
            return
        self._pressed_at = None
        if self.menu_open():
            await Radio._menu["owner"].menu_select()
            return
        self._clicks += 1
        if self._click_task and not self._click_task.done():
            self._click_task.cancel()
        self._click_task = asyncio.create_task(self._resolve_clicks())

    async def _resolve_clicks(self):
        try:
            await asyncio.sleep(DOUBLE_CLICK)
        except asyncio.CancelledError:
            return
        n, self._clicks = self._clicks, 0
        if n >= 2:
            await self.toggle_favorite()
        else:
            await self.toggle_play()

    async def on_key_down(self, payload):
        await self._press_down(payload)

    async def on_key_up(self, payload):
        await self._press_up(payload)

    async def on_dial_down(self, payload):
        await self._press_down(payload)

    async def on_dial_up(self, payload):
        await self._press_up(payload)

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        if not ticks:
            return
        if self.menu_open():
            await Radio._menu["owner"].menu_move(1 if ticks > 0 else -1)
        else:
            await self.step(1 if ticks > 0 else -1)

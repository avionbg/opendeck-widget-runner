"""Lyrics: synced lyrics (LRCLIB) for whatever plays, following the position reported by Windows
media controls. The key shows the current line; pressing it toggles a transparent overlay on the
primary monitor (overlay.py, separate process) with the current line centred and highlighted.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, fit_text, font, get_json

log = logging.getLogger("widgetrunner.lyrics")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

POLL_SECONDS = 1.0
WINRT_TIMEOUT = 3.0
OVERLAY_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "overlay.py")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
LRCLIB = "https://lrclib.net/api"

BG = (18, 20, 28)
TEXT = (245, 246, 250)
DIM = (130, 136, 150)
ACCENT = (150, 110, 240)

NOISE = re.compile(r"[\(\[][^\)\]]*(official|video|lyric|audio|hd|4k|remaster|visuali[sz]er|\bmv\b|music)[^\)\]]*[\)\]]", re.I)
CHANNEL_SUFFIX = re.compile(r"(vevo|official|- topic|\btv\b|records|music)\s*$", re.I)
LRC_LINE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


# ---------------------------------------------------------------- lyrics
def parse_track(title, artist):
    """'Artist - Song (Official Video)' + channel name -> (artist, song)."""
    clean = NOISE.sub("", title or "").replace("  ", " ").strip(" -|")
    if " - " in clean:
        a, t = [x.strip() for x in clean.split(" - ", 1)]
    elif " – " in clean:
        a, t = [x.strip() for x in clean.split(" – ", 1)]
    else:
        a, t = CHANNEL_SUFFIX.sub("", artist or "").strip(), clean
    return a, t


def parse_lrc(text):
    """LRC -> sorted list of (seconds, line)."""
    out = []
    for raw in (text or "").splitlines():
        stamps = LRC_LINE.findall(raw)
        if not stamps:
            continue
        line = LRC_LINE.sub("", raw).strip()
        for m, s in stamps:
            out.append((int(m) * 60 + float(s), line))
    out.sort(key=lambda x: x[0])
    return out


def fetch_lyrics(artist, track, duration):
    """-> list of (seconds, line) or [] when no synced lyrics are available."""
    q = {"artist_name": artist, "track_name": track}
    if duration:
        q["duration"] = int(round(duration))
    try:
        data = get_json(f"{LRCLIB}/get?" + urllib.parse.urlencode(q))
        if data.get("syncedLyrics"):
            return parse_lrc(data["syncedLyrics"])
    except Exception as e:
        log.debug("lrclib get failed: %s", e)
    try:
        results = get_json(f"{LRCLIB}/search?" + urllib.parse.urlencode({"q": f"{artist} {track}"}))
    except Exception as e:
        log.warning("lrclib search failed: %s", e)
        return []
    best = None
    for r in results:
        if not r.get("syncedLyrics"):
            continue
        d = r.get("duration") or 0
        if duration and abs(d - duration) > 8:
            continue
        best = r
        break
    return parse_lrc(best["syncedLyrics"]) if best else []


def line_index(lyrics, position):
    idx = -1
    for i, (t, _) in enumerate(lyrics):
        if t <= position:
            idx = i
        else:
            break
    return idx


# ---------------------------------------------------------------- render
def note(d, cx, cy, color, scale=1.0):
    """Eighth-note glyph drawn with primitives (the UI fonts lack the character)."""
    r = 9 * scale
    d.ellipse((cx - r - 6 * scale, cy + 10 * scale - r, cx + r - 6 * scale, cy + 10 * scale + r), fill=color)
    d.line((cx + r - 8 * scale, cy + 10 * scale, cx + r - 8 * scale, cy - 34 * scale), fill=color, width=int(4 * scale))
    d.line((cx + r - 8 * scale, cy - 34 * scale, cx + r + 16 * scale, cy - 26 * scale), fill=color, width=int(4 * scale))


def render(current, nxt, status) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    if status in ("none", "nolyrics"):
        note(d, SIZE // 2, 62, DIM)
        d.text((SIZE // 2, 108), "nothing playing" if status == "none" else "no synced lyrics", fill=DIM, font=font(13), anchor="mm")
        return img
    d.rectangle((0, 0, 4, SIZE), fill=ACCENT)
    if not current:
        note(d, SIZE // 2, 56, TEXT, 0.8)
        if nxt:
            d.text((SIZE // 2 + 2, SIZE - 20), nxt, fill=DIM, font=fit_text(d, nxt, SIZE - 18, 13, min_size=10), anchor="mm")
        return img
    text = current
    # wrap the current line into up to 3 rows
    f = font(17, "bold")
    words, rows, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=f) <= SIZE - 18:
            cur = trial
        else:
            if cur:
                rows.append(cur)
            cur = w
    if cur:
        rows.append(cur)
    rows = rows[:3]
    y = 30 if len(rows) < 3 else 22
    for r in rows:
        d.text((SIZE // 2 + 2, y), r, fill=TEXT, font=fit_text(d, r, SIZE - 18, 17, "bold", min_size=12), anchor="mm")
        y += 24
    if nxt:
        d.text((SIZE // 2 + 2, SIZE - 20), nxt, fill=DIM, font=fit_text(d, nxt, SIZE - 18, 13, min_size=10), anchor="mm")
    return img


# ---------------------------------------------------------------- widget
class Lyrics(Widget):
    action = "com.goran.widgetrunner.lyrics"
    _manager = None
    _overlay = None          # subprocess.Popen, shared by all instances
    _overlay_shown = False

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.track_key = None
        self.lyrics = []
        self.index = -2
        self.status = "none"
        self._fetching = None
        self._radio_key = None       # last (title, artist) seen from the radio
        self._radio_start = 0.0      # monotonic time the radio song last changed (position estimate)

    CONFIG_KEYS = ("width_pct", "height_pct", "position", "font_pct", "text_color", "bg_color", "bg_opacity")

    def overlay_config(self):
        """Overlay settings from the property inspector; missing values fall back to overlay defaults."""
        cfg = {}
        for k in self.CONFIG_KEYS:
            v = self.settings.get(k)
            if v in (None, ""):
                continue
            if k in ("position", "text_color", "bg_color"):
                cfg[k] = str(v)
            else:
                try:
                    cfg[k] = float(v)
                except (TypeError, ValueError):
                    pass
        return cfg

    async def on_appear(self):
        if not HAVE_WINSDK:
            await self.set_image(render("", "", "none"))
            return
        self._tasks.append(asyncio.create_task(self._loop()))

    async def on_settings(self, settings):
        await super().on_settings(settings)
        await Lyrics.overlay_send({"config": self.overlay_config()})

    async def on_disappear(self):
        await super().on_disappear()
        await Lyrics.overlay_send({"show": False})

    # --- media position --------------------------------------------------
    @classmethod
    async def manager(cls):
        if cls._manager is None:
            cls._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
        return cls._manager

    def _radio_np(self):
        """Radio (mpv) 'now playing' with an estimated position, or None.

        mpv registers no Windows media session, so the SMTC read never sees the radio. Stations
        refresh the ICY title at each song's start, so the moment the title changes approximates
        the song start; position is the elapsed time since then (good enough to follow LRC lines)."""
        try:
            from widgets.radio import radio_now_playing
            np = radio_now_playing()
        except Exception:
            return None
        if not np:
            return None
        title, artist = (np.get("title") or "").strip(), (np.get("artist") or "").strip()
        if not title:
            return None
        key = (title, artist)
        now = time.monotonic()
        if key != self._radio_key:
            self._radio_key = key
            self._radio_start = now
        return title, artist, now - self._radio_start

    async def now_playing(self):
        """-> (title, artist, duration, position, playing) or None.

        Prefers an app that is actively playing; otherwise falls back to the internet radio,
        and finally to a paused app so its lyrics stay on screen."""
        s = None
        smtc = None
        smtc_playing = False
        try:
            s = (await self.manager()).get_current_session()
            if s is not None:
                p = await asyncio.wait_for(s.try_get_media_properties_async(), WINRT_TIMEOUT)
                pb, tl = s.get_playback_info(), s.get_timeline_properties()
                smtc_playing = pb.playback_status.name == "PLAYING"
                pos = tl.position.total_seconds()
                if smtc_playing:
                    try:
                        pos += time.time() - tl.last_updated_time.timestamp()
                    except Exception:
                        pass
                smtc = (p.title or "", p.artist or "", tl.end_time.total_seconds(), pos, smtc_playing)
        except Exception as e:
            log.debug("media query failed: %s", e)
            Lyrics._manager = None
            s = smtc = None
        if smtc is not None and smtc_playing:
            return smtc
        radio = self._radio_np()
        if radio is not None:
            title, artist, pos = radio
            return title, artist, 0.0, pos, True
        return smtc  # a paused app (or None when nothing is playing at all)

    # --- main loop -------------------------------------------------------
    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("lyrics tick failed")
            await asyncio.sleep(POLL_SECONDS)

    async def tick(self):
        np_ = await self.now_playing()
        if not np_ or not np_[0]:
            await self._set(status="none", index=-1)
            return
        title, artist, duration, pos, playing = np_
        key = (title, artist)
        if key != self.track_key:
            self.track_key = key
            self.lyrics, self.index = [], -2
            a, t = parse_track(title, artist)
            log.info("lyrics lookup: %r / %r (%ss)", a, t, int(duration))
            self.lyrics = await asyncio.to_thread(fetch_lyrics, a, t, duration)
            self.status = "ok" if self.lyrics else "nolyrics"
            await Lyrics.overlay_send({"lines": [l for _, l in self.lyrics], "index": -1})
            self.index = -2
        if not self.lyrics:
            await self._set(status="nolyrics", index=-1)
            return
        await self._set(status="ok", index=line_index(self.lyrics, pos))

    async def _set(self, status, index):
        if status == self.status and index == self.index:
            return
        self.status, self.index = status, index
        if status != "ok":
            await self.set_image(render("", "", status))
        else:
            cur = self.lyrics[index][1] if index >= 0 else ""
            nxt = self.lyrics[index + 1][1] if index + 1 < len(self.lyrics) else ""
            await self.set_image(render(cur, nxt, "ok"))
            await Lyrics.overlay_send({"index": index})

    # --- overlay process -------------------------------------------------
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

    @classmethod
    def overlay_start(cls):
        if cls._overlay is not None and cls._overlay.poll() is None:
            return
        exe = PYTHONW if os.path.exists(PYTHONW) else sys.executable
        errlog = open(os.path.join(os.path.dirname(OVERLAY_SCRIPT), "overlay.log"), "ab")
        cls._overlay = subprocess.Popen([exe, OVERLAY_SCRIPT], stdin=subprocess.PIPE,
                                        stdout=errlog, stderr=errlog, cwd=os.path.dirname(OVERLAY_SCRIPT),
                                        creationflags=0x08000000)
        log.info("overlay started (pid %s)", cls._overlay.pid)

    async def on_key_down(self, payload):
        Lyrics._overlay_shown = not Lyrics._overlay_shown
        if Lyrics._overlay_shown:
            Lyrics.overlay_start()
            await asyncio.sleep(0.3)  # give the overlay a moment to come up before the first message
            await Lyrics.overlay_send({"config": self.overlay_config()})
            await Lyrics.overlay_send({"lines": [l for _, l in self.lyrics], "index": self.index})
        await Lyrics.overlay_send({"show": Lyrics._overlay_shown})

    async def on_dial_down(self, payload):
        await self.on_key_down(payload)

"""Track info: album, year, genre and cover art for the current track, from MusicBrainz and the
Cover Art Archive (both free, no key). The key shows a summary; pressing it toggles an on-screen
card (same overlay process and settings as the lyrics overlay) with the cover and the details.
"""
import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from PIL import Image, ImageDraw, ImageEnhance

from widgetlib import Widget
from widgets._draw import SIZE, SSL_CONTEXT, fit_text, font, get_json
from widgets.lyrics import parse_track
from widgets.nowplaying import read_thumbnail

log = logging.getLogger("widgetrunner.trackinfo")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

POLL_SECONDS = 1.5
WINRT_TIMEOUT = 3.0
MB = "https://musicbrainz.org/ws/2"
CAA = "https://coverartarchive.org"
from config import MUSICBRAINZ_CONTACT as _MB_CONTACT
UA = {"User-Agent": f"opendeck-widget-runner/0.1 ( {_MB_CONTACT} )", "Accept": "application/json"}
MB_RETRIES = 4            # MusicBrainz answers 503 "busy" quite often; retry with a pause
MB_PAUSE = 1.2            # s between MusicBrainz requests (their limit is 1/s)
OVERLAY_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "overlay.py")
PYTHONW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")

BG = (18, 20, 28)
TEXT = (245, 246, 250)
DIM = (185, 190, 205)
ACCENT = (240, 190, 60)


# ---------------------------------------------------------------- lookups (blocking, run in a thread)
def mb_get(url):
    last = None
    for i in range(MB_RETRIES):
        try:
            return get_json(url, headers=UA)
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (503, 429):
                raise
        except Exception as e:  # network hiccup
            last = e
        time.sleep(MB_PAUSE * (i + 1))
    raise last


def fetch_cover(mbids):
    """Try Cover Art Archive for each (kind, mbid); -> PIL image or None."""
    for kind, mbid in mbids:
        if not mbid:
            continue
        try:
            req = urllib.request.Request(f"{CAA}/{kind}/{mbid}/front-250", headers={"User-Agent": UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as r:
                return Image.open(io.BytesIO(r.read())).convert("RGB")
        except Exception:
            continue
    return None


def pick_release(recs):
    """Aggregate the releases of every matching recording and prefer the earliest official studio
    album (then single/EP, then anything dated). -> (release dict or None, first year)."""
    rels = []
    for rec in recs:
        for r in rec.get("releases", []) or []:
            rg = r.get("release-group") or {}
            rels.append({"title": r.get("title", ""), "date": r.get("date") or "", "status": r.get("status", ""),
                         "ptype": rg.get("primary-type"), "stypes": rg.get("secondary-types") or [],
                         "rgid": rg.get("id"), "id": r.get("id")})
    dated = sorted([r for r in rels if r["date"]], key=lambda r: r["date"])
    studio = [r for r in dated if r["ptype"] == "Album" and not r["stypes"] and r["status"] in ("Official", "")]
    single = [r for r in dated if r["ptype"] in ("Single", "EP") and not r["stypes"]]
    pick = (studio or single or dated or rels or [None])[0]
    return pick, (dated[0]["date"][:4] if dated else "")


def lookup(artist, title):
    """-> dict(album, year, genre, length, country, since, cover) or None when nothing matches."""
    q = urllib.parse.quote(f'artist:"{artist}" AND recording:"{title}"')
    data = mb_get(f"{MB}/recording/?query={q}&fmt=json&limit=25")
    all_recs = [r for r in data.get("recordings", []) if r.get("score", 0) >= 90]
    exact = [r for r in all_recs if (r.get("title") or "").lower() == title.lower()]
    recs = exact or all_recs or [r for r in data.get("recordings", []) if r.get("score", 0) >= 60][:5]
    if not recs:
        return None
    rec = recs[0]
    pick, year = pick_release(recs)
    tags = {}
    for r in recs:
        for t in r.get("tags", []) or []:
            tags[t["name"]] = tags.get(t["name"], 0) + t.get("count", 1)
    info = {
        "album": pick["title"] if pick else "",
        "year": year or (rec.get("first-release-date") or "")[:4],
        "length": next((int(r["length"] / 1000) for r in recs if r.get("length")), 0),
        "genre": ", ".join(k for k, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:2]),
        "country": "",
        "since": "",
    }
    credit = (rec.get("artist-credit") or [{}])[0].get("artist") or {}
    if credit.get("id"):
        try:
            time.sleep(MB_PAUSE)
            ar = mb_get(f"{MB}/artist/{credit['id']}?inc=genres+tags&fmt=json")
            if not info["genre"]:
                pool = sorted(ar.get("genres") or ar.get("tags") or [], key=lambda t: -t.get("count", 0))
                info["genre"] = ", ".join(t["name"] for t in pool[:2])
            info["country"] = ar.get("country") or ""
            info["since"] = ((ar.get("life-span") or {}).get("begin") or "")[:4]
        except Exception as e:
            log.debug("artist lookup failed: %s", e)
    ids = []
    if pick:
        ids += [("release-group", pick["rgid"]), ("release", pick["id"])]
    for r in recs[0].get("releases", [])[:4]:
        ids += [("release-group", (r.get("release-group") or {}).get("id")), ("release", r.get("id"))]
    info["cover"] = fetch_cover(ids)
    return info


# ---------------------------------------------------------------- key render
def render(state) -> Image.Image:
    art = state.get("cover") or state.get("thumb")
    if art:
        w, h = art.size
        scale = max(SIZE / w, SIZE / h)
        img = art.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        x, y = (img.width - SIZE) // 2, (img.height - SIZE) // 2
        img = ImageEnhance.Brightness(img.crop((x, y, x + SIZE, y + SIZE))).enhance(0.4)
    else:
        img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img, "RGBA")
    if state.get("status") == "none":
        d.text((SIZE // 2, 60), "i", fill=DIM, font=font(44, "bold"), anchor="mm")
        d.text((SIZE // 2, 104), "nothing playing", fill=DIM, font=font(13), anchor="mm")
        return img
    if state.get("status") == "looking":
        d.text((SIZE // 2, 72), "looking up…", fill=DIM, font=font(14), anchor="mm")
        return img
    if state.get("status") == "nomatch":
        d.text((SIZE // 2, 60), "?", fill=DIM, font=font(44, "bold"), anchor="mm")
        d.text((SIZE // 2, 104), "no match", fill=DIM, font=font(13), anchor="mm")
        return img
    for i in range(60):
        d.line((0, SIZE - 60 + i, SIZE, SIZE - 60 + i), fill=(10, 12, 18, int(170 * i / 60)))
    album = state.get("album") or "—"
    d.text((8, SIZE - 46), album, fill=TEXT, font=fit_text(d, album, SIZE - 16, 15, "bold", min_size=11), anchor="lm")
    line2 = " · ".join(x for x in (state.get("year"), state.get("genre")) if x)
    d.text((8, SIZE - 24), line2, fill=DIM, font=fit_text(d, line2, SIZE - 16, 13, min_size=10), anchor="lm")
    return img


# ---------------------------------------------------------------- widget
class TrackInfo(Widget):
    action = "com.goran.widgetrunner.trackinfo"
    _manager = None
    _overlay = None
    _overlay_shown = False
    _cache = {}

    CONFIG_KEYS = ("width_pct", "height_pct", "position", "font_pct", "text_color", "bg_color", "bg_opacity")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.key = None
        self.state = {"status": "none"}
        self._lookup_task = None

    # --- settings ------------------------------------------------------
    def overlay_config(self):
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
        cfg.setdefault("height_pct", 32)
        return cfg

    async def on_settings(self, settings):
        await super().on_settings(settings)
        await TrackInfo.overlay_send({"config": self.overlay_config()})

    # --- lifecycle -----------------------------------------------------
    async def on_appear(self):
        if not HAVE_WINSDK:
            await self.set_image(render({"status": "none"}))
            return
        self.every(POLL_SECONDS, self.tick)

    async def on_disappear(self):
        await super().on_disappear()
        await TrackInfo.overlay_send({"show": False})

    async def session(self):
        try:
            if TrackInfo._manager is None:
                TrackInfo._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
            return TrackInfo._manager.get_current_session()
        except Exception as e:
            log.debug("session failed: %s", e)
            TrackInfo._manager = None
            return None

    async def tick(self):
        s = await self.session()
        smtc_playing = False
        if s is not None:
            try:
                smtc_playing = s.get_playback_info().playback_status.name == "PLAYING"
            except Exception:
                smtc_playing = False
        title_artist = None
        if not smtc_playing:
            title_artist = self._radio_title()   # no app actively playing: try the radio's ICY title
        if title_artist is None and s is None:
            if self.key is not None:
                self.key, self.state = None, {"status": "none"}
                await self.set_image(render(self.state))
                await TrackInfo.overlay_send({"show": False})
            return
        if title_artist is not None:
            raw_title, raw_artist, thumb = title_artist
        else:
            try:
                p = await asyncio.wait_for(s.try_get_media_properties_async(), WINRT_TIMEOUT)
            except Exception:
                return
            raw_title, raw_artist, thumb = p.title or "", p.artist or "", p.thumbnail
        key = (raw_title, raw_artist)
        if key == self.key:
            return
        self.key = key
        if not key[0]:
            self.state = {"status": "none"}
            await self.set_image(render(self.state))
            return
        artist, title = parse_track(*key)
        thumb_img = None
        if thumb is not None and not isinstance(thumb, Image.Image):   # a WinRT thumbnail reference
            try:
                thumb_img = await asyncio.wait_for(read_thumbnail(thumb), WINRT_TIMEOUT)
            except Exception:
                pass
        self.state = {"status": "looking", "artist": artist, "title": title, "thumb": thumb_img}
        await self.set_image(render(self.state))
        if self._lookup_task and not self._lookup_task.done():
            self._lookup_task.cancel()
        self._lookup_task = asyncio.create_task(self._lookup(key, artist, title, thumb_img))

    def _radio_title(self):
        """-> (title, artist, None) from the radio's ICY 'now playing', or None."""
        try:
            from widgets.radio import radio_now_playing
        except Exception:
            return None
        np = radio_now_playing()
        if not np or not np.get("title"):
            return None
        return np["title"], np["artist"] or np["station"], None

    async def _lookup(self, key, artist, title, thumb):
        info = TrackInfo._cache.get(key)
        if info is None:
            try:
                info = await asyncio.to_thread(lookup, artist, title)
            except Exception as e:
                log.warning("musicbrainz lookup failed for %r / %r: %s", artist, title, e)
                info = None
            TrackInfo._cache[key] = info or {}
        if self.key != key:
            return
        if not info:
            self.state = {"status": "nomatch", "artist": artist, "title": title, "thumb": thumb}
        else:
            self.state = {"status": "ok", "artist": artist, "title": title, "thumb": thumb, **info}
        await self.set_image(render(self.state))
        if TrackInfo._overlay_shown:
            await TrackInfo.overlay_send({"card": self.card()})

    # --- overlay card --------------------------------------------------
    def card(self):
        st = self.state
        art = st.get("cover") or st.get("thumb")
        b64 = None
        if art:
            buf = io.BytesIO()
            art.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        rows = []
        if st.get("status") == "ok":
            if st.get("album"):
                rows.append(("Album", st["album"]))
            if st.get("year"):
                rows.append(("Year", st["year"]))
            if st.get("genre"):
                rows.append(("Genre", st["genre"]))
            if st.get("length"):
                rows.append(("Length", f"{st['length'] // 60}:{st['length'] % 60:02d}"))
            extra = ", ".join(x for x in (st.get("country"), f"since {st['since']}" if st.get("since") else "") if x)
            if extra:
                rows.append(("Artist", extra))
        elif st.get("status") == "looking":
            rows.append(("", "looking up MusicBrainz…"))
        elif st.get("status") == "nomatch":
            rows.append(("", "not found on MusicBrainz"))
        return {"title": st.get("title") or "", "artist": st.get("artist") or "", "rows": rows, "image": b64}

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
        cls._overlay = subprocess.Popen([exe, OVERLAY_SCRIPT], stdin=subprocess.PIPE, stdout=errlog, stderr=errlog,
                                        cwd=os.path.dirname(OVERLAY_SCRIPT), creationflags=0x08000000)
        log.info("track info overlay started (pid %s)", cls._overlay.pid)

    async def on_key_down(self, payload):
        TrackInfo._overlay_shown = not TrackInfo._overlay_shown
        if TrackInfo._overlay_shown:
            TrackInfo.overlay_start()
            await asyncio.sleep(0.3)
            await TrackInfo.overlay_send({"config": self.overlay_config()})
            await TrackInfo.overlay_send({"card": self.card()})
        await TrackInfo.overlay_send({"show": TrackInfo._overlay_shown})

    async def on_dial_down(self, payload):
        await self.on_key_down(payload)

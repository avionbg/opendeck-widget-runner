"""Now Playing: what the browser (or any app) reports to Windows media controls (SMTC).

Shows the thumbnail as background with title / artist, play state and a progress bar.
Key: 1 click = play / pause, 2 quick clicks = next track, 3 quick clicks = previous track.
Dial: rotate = previous / next track, press = play / pause.
Uses the `winsdk` package (Windows.Media.Control); long title / artist bounce left-right.
"""
import asyncio
import io
import logging
import time
from datetime import datetime, timezone

from PIL import Image, ImageDraw, ImageEnhance

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.nowplaying")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    from winsdk.windows.storage.streams import DataReader as _DataReader
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

POLL_SECONDS = 1.0
WINRT_TIMEOUT = 3.0   # seconds; a stuck WinRT call must never freeze the widget
CLICK_WINDOW = 0.35   # seconds to wait for another click before acting
MARQUEE_FPS = 6
MARQUEE_STEP = 3   # px per frame

BG = (18, 20, 28)
TEXT = (245, 246, 250)
DIM = (190, 196, 210)
ACCENT = (80, 150, 240)
TRACK = (60, 64, 80)


async def read_thumbnail(ref):
    stream = await asyncio.wait_for(ref.open_read_async(), WINRT_TIMEOUT)
    size = stream.size
    reader = _DataReader(stream.get_input_stream_at(0))
    await asyncio.wait_for(reader.load_async(size), WINRT_TIMEOUT)
    data = bytes(reader.read_buffer(size))
    return Image.open(io.BytesIO(data)).convert("RGB")


def cover(img):
    """Scale + centre-crop the thumbnail to fill the key, darkened so text stays readable."""
    w, h = img.size
    scale = max(SIZE / w, SIZE / h)
    img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    x = (img.width - SIZE) // 2
    y = (img.height - SIZE) // 2
    img = img.crop((x, y, x + SIZE, y + SIZE))
    return ImageEnhance.Brightness(img).enhance(0.45)


DEFAULT_TITLE_SIZE = 20   # artist is always 4 px smaller
MARGIN = 6
HOLD_FRAMES = 8


def bounce(frame, span, step=MARQUEE_STEP, hold=HOLD_FRAMES):
    """Back-and-forth offset 0..span for a text wider than the key, pausing `hold` frames at each end."""
    if span <= 0:
        return 0
    travel = max(1, int(span / step))
    period = 2 * (travel + hold)
    f = frame % period
    if f < hold:
        return 0
    f -= hold
    if f < travel:
        return int(f * step)
    f -= travel
    if f < hold:
        return span
    return int(span - (f - hold) * step)


def draw_scrolling(d, text, y, fnt, color, frame):
    w = d.textlength(text, font=fnt)
    span = int(w - (SIZE - 2 * MARGIN))
    x = MARGIN - bounce(frame, span) if span > 0 else (SIZE - w) / 2
    d.text((x, y), text, fill=color, font=fnt, anchor="lm")
    return span > 0


def play_pause_icon(d, cx, cy, playing, size=18):
    if playing:
        d.polygon([(cx - size * 0.7, cy - size), (cx - size * 0.7, cy + size), (cx + size, cy)], fill=TEXT)
    else:
        w = size * 0.55
        d.rounded_rectangle((cx - size * 0.9, cy - size, cx - size * 0.9 + w, cy + size), radius=3, fill=TEXT)
        d.rounded_rectangle((cx + size * 0.9 - w, cy - size, cx + size * 0.9, cy + size), radius=3, fill=TEXT)


def render(state, frame=0, title_size=DEFAULT_TITLE_SIZE) -> Image.Image:
    """state: dict(title, artist, status, position, duration, art (PIL or None)). Returns the image;
    render.scrolling is set to True when any text is moving (caller uses it to pick the frame rate)."""
    img = cover(state["art"]) if state.get("art") else Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img, "RGBA")
    render.scrolling = False
    if not state.get("title"):
        d.ellipse((52, 74, 72, 94), fill=DIM)
        d.line((70, 84, 70, 40), fill=DIM, width=5)
        d.line((70, 40, 96, 34), fill=DIM, width=5)
        d.text((SIZE // 2, 116), "nothing playing", fill=DIM, font=font(14), anchor="mm")
        return img
    # darken the lower part so text stays readable over the artwork
    for i in range(76):
        d.line((0, SIZE - 76 + i, SIZE, SIZE - 76 + i), fill=(10, 12, 18, int(170 * i / 76)))
    title_size = max(14, min(30, int(title_size)))
    artist_size = title_size - 4
    y_title = SIZE - 14 - artist_size * 1.25 - 8 - title_size * 0.6
    y_artist = SIZE - 14 - artist_size * 0.65
    play_pause_icon(d, SIZE // 2, int((y_title - title_size * 0.6) / 2) + 4, state["status"] == "PLAYING")
    moving = draw_scrolling(d, state["title"], y_title, font(title_size, "bold"), TEXT, frame)
    if state.get("artist"):
        moving |= draw_scrolling(d, state["artist"], y_artist, font(artist_size, "semibold"), DIM, frame)
    render.scrolling = moving
    dur = state.get("duration") or 0
    d.rectangle((MARGIN, SIZE - 9, SIZE - MARGIN, SIZE - 5), fill=TRACK)
    if dur > 0:
        frac = max(0.0, min(1.0, (state.get("position") or 0) / dur))
        d.rectangle((MARGIN, SIZE - 9, MARGIN + (SIZE - 2 * MARGIN) * frac, SIZE - 5), fill=ACCENT)
    return img


class NowPlaying(Widget):
    action = "com.goran.widgetrunner.nowplaying"
    _manager = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.state = {"title": "", "artist": "", "status": "", "position": 0, "duration": 0, "art": None}
        self._art_key = None
        self._offset = 0
        self._last_poll = 0.0
        self._clicks = 0
        self._click_task = None

    @property
    def title_size(self):
        try:
            return max(14, min(30, int(self.settings.get("title_size") or DEFAULT_TITLE_SIZE)))
        except (TypeError, ValueError):
            return DEFAULT_TITLE_SIZE

    async def on_appear(self):
        if not HAVE_WINSDK:
            await self.set_image(render({"title": ""}))
            log.warning("winsdk not installed; Now Playing disabled")
            return
        self._tasks.append(asyncio.create_task(self._loop()))

    # --- data ----------------------------------------------------------
    @classmethod
    async def manager(cls):
        if cls._manager is None:
            cls._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
        return cls._manager

    async def session(self):
        try:
            return (await self.manager()).get_current_session()
        except Exception as e:
            log.debug("session manager failed: %s", e)
            NowPlaying._manager = None
            return None

    async def poll(self):
        s = await self.session()
        # An app that only registered a paused session (e.g. Chrome) keeps get_current_session()
        # non-None; the radio is the real audio, so prefer it whenever no app is actively playing.
        smtc_playing = False
        if s is not None:
            try:
                smtc_playing = s.get_playback_info().playback_status.name == "PLAYING"
            except Exception:
                smtc_playing = False
        radio_ok = (not smtc_playing) and await self._radio_fallback()
        if log.isEnabledFor(logging.DEBUG):
            try:
                from widgets.radio import radio_now_playing as _rnp
                _np = _rnp()
            except Exception as _e:
                _np = f"<err {_e}>"
            log.debug("NP poll: smtc_playing=%s radio_ok=%s radio_np=%r cur_session=%s",
                      smtc_playing, radio_ok, _np,
                      (s.source_app_user_model_id if s is not None else None))
        if radio_ok:
            self._from_radio = True
            return
        self._from_radio = False
        if s is None:
            self.state.update(title="", artist="", status="", position=0, duration=0, art=None)
            self._art_key = None
            return
        props = await asyncio.wait_for(s.try_get_media_properties_async(), WINRT_TIMEOUT)
        pb = s.get_playback_info()
        tl = s.get_timeline_properties()
        position = tl.position.total_seconds()
        if pb.playback_status.name == "PLAYING":
            # extrapolate: apps only refresh the timeline every few seconds
            try:
                position += time.time() - tl.last_updated_time.timestamp()
            except Exception:
                pass
        self.state.update(title=props.title or "", artist=props.artist or "", status=pb.playback_status.name,
                          position=position, duration=tl.end_time.total_seconds())
        if log.isEnabledFor(logging.DEBUG):
            others = []
            for o in (await self.manager()).get_sessions():
                try:
                    op = await asyncio.wait_for(o.try_get_media_properties_async(), WINRT_TIMEOUT)
                    others.append(f"{o.source_app_user_model_id}:{o.get_playback_info().playback_status.name}:{op.title!r}")
                except Exception as e:
                    others.append(f"?:{e}")
            log.debug("poll current=%s:%s:%r | all sessions: %s", s.source_app_user_model_id, pb.playback_status.name, props.title, others)
        key = (props.title, props.artist, props.album_title)
        if key != self._art_key:
            self._art_key = key
            self._offset = 0
            self.state["art"] = None
            if props.thumbnail:
                try:
                    self.state["art"] = await read_thumbnail(props.thumbnail)
                except Exception as e:
                    log.debug("thumbnail failed: %s", e)

    async def _radio_fallback(self):
        """When no app registers a Windows media session, show the radio's ICY 'now playing'
        (mpv doesn't expose SMTC). -> True when the radio provided something."""
        try:
            from widgets.radio import radio_now_playing
        except Exception:
            return False
        np = radio_now_playing()
        if not np:
            return False
        title = np["title"] or np["station"]
        artist = np["artist"] or np["station"]
        if (title, artist) != self._art_key:
            self._art_key = (title, artist)
            self._offset = 0
            self.state["art"] = None
        self.state.update(title=title, artist=artist, status="PLAYING", position=0, duration=0)
        return True

    # --- loop ----------------------------------------------------------
    async def _loop(self):
        while True:
            try:
                now = time.monotonic()
                if now - self._last_poll >= POLL_SECONDS:
                    self._last_poll = now
                    await self.poll()
                img = render(self.state, self._offset, self.title_size)
                await self.set_image(img)
                scrolling = render.scrolling
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("now playing frame failed")
                scrolling = False
            if scrolling:
                self._offset += 1
                await asyncio.sleep(1 / MARQUEE_FPS)
            else:
                await asyncio.sleep(POLL_SECONDS)

    # --- controls ------------------------------------------------------
    async def _control(self, what):
        if getattr(self, "_from_radio", False):
            # the shown track comes from the radio (mpv), which has no Windows media session;
            # play/pause/next here must not touch some other paused app. Control the radio from the
            # Radio key instead.
            await self.deck.show_alert(self.context)
            return
        s = await self.session()
        if s is None:
            await self.deck.show_alert(self.context)
            return
        try:
            ok = await asyncio.wait_for({"toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                                         "prev": s.try_skip_previous_async}[what](), WINRT_TIMEOUT)
        except Exception as e:
            log.warning("media control %s failed: %s", what, e)
            ok = False
        if not ok:
            await self.deck.show_alert(self.context)
        self._last_poll = 0  # refresh on the next frame

    async def on_key_down(self, payload):
        """Count quick clicks: 1 = play/pause, 2 = next, 3 = previous."""
        self._clicks += 1
        if self._click_task and not self._click_task.done():
            self._click_task.cancel()
        if self._clicks >= 3:
            await self._resolve_clicks()
        else:
            self._click_task = asyncio.create_task(self._wait_then_resolve())

    async def _wait_then_resolve(self):
        try:
            await asyncio.sleep(CLICK_WINDOW)
        except asyncio.CancelledError:
            return
        await self._resolve_clicks()

    async def _resolve_clicks(self):
        n, self._clicks = self._clicks, 0
        await self._control({1: "toggle", 2: "next"}.get(n, "prev"))

    async def on_dial_down(self, payload):
        await self._control("toggle")

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        if ticks:
            await self._control("next" if ticks > 0 else "prev")

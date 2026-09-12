"""Seek: dial rotate scrubs the current track (Windows media controls), press = play / pause.
Shows elapsed / total time and a progress bar. Step per tick is configurable (default 5 s)."""
import asyncio
import logging
import time

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.seek")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

POLL_SECONDS = 1.0
WINRT_TIMEOUT = 3.0
DEFAULT_STEP = 5
TICK = 10_000_000  # 100 ns units per second

BG = (18, 20, 28)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)
ACCENT = (80, 150, 240)
TRACK = (44, 48, 62)


def fmt(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def render(pos, dur, playing, has_media=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    if not has_media:
        d.text((cx, 60), "--:--", fill=DIM, font=font(30, "bold"), anchor="mm")
        d.text((cx, 100), "nothing playing", fill=DIM, font=font(13), anchor="mm")
        return img
    if playing:
        d.polygon([(cx - 7, 18), (cx - 7, 38), (cx + 9, 28)], fill=TEXT)
    else:
        d.rectangle((cx - 8, 18, cx - 3, 38), fill=TEXT)
        d.rectangle((cx + 2, 18, cx + 7, 38), fill=TEXT)
    d.text((cx, 68), fmt(pos), fill=TEXT, font=font(34, "bold"), anchor="mm")
    d.text((cx, 98), f"/ {fmt(dur)}" if dur else "", fill=DIM, font=font(15), anchor="mm")
    d.rounded_rectangle((12, 118, SIZE - 12, 128), radius=5, fill=TRACK)
    if dur > 0:
        d.rounded_rectangle((12, 118, 12 + (SIZE - 24) * max(0.0, min(1.0, pos / dur)), 128), radius=5, fill=ACCENT)
    return img


class Seek(Widget):
    action = "com.goran.widgetrunner.seek"
    _manager = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.pos = 0.0
        self.dur = 0.0
        self.playing = False
        self.has_media = False
        self._pending_until = 0.0   # after a seek, trust our own position until the app catches up

    @property
    def step(self):
        try:
            return max(1, min(60, int(self.settings.get("step") or DEFAULT_STEP)))
        except (TypeError, ValueError):
            return DEFAULT_STEP

    async def on_appear(self):
        if not HAVE_WINSDK:
            await self.set_image(render(0, 0, False, False))
            return
        self.every(POLL_SECONDS, self.tick)

    @classmethod
    async def manager(cls):
        if cls._manager is None:
            cls._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
        return cls._manager

    async def session(self):
        try:
            return (await self.manager()).get_current_session()
        except Exception as e:
            log.debug("session failed: %s", e)
            Seek._manager = None
            return None

    async def tick(self):
        s = await self.session()
        if s is None:
            self.has_media = False
            await self.set_image(render(0, 0, False, False))
            return
        try:
            pb, tl = s.get_playback_info(), s.get_timeline_properties()
        except Exception as e:
            log.debug("timeline failed: %s", e)
            return
        self.has_media = True
        self.playing = pb.playback_status.name == "PLAYING"
        self.dur = tl.end_time.total_seconds()
        if time.monotonic() >= self._pending_until:
            pos = tl.position.total_seconds()
            if self.playing:
                try:
                    pos += time.time() - tl.last_updated_time.timestamp()
                except Exception:
                    pass
            self.pos = pos
        elif self.playing:
            self.pos += POLL_SECONDS
        await self.set_image(render(self.pos, self.dur, self.playing))

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        s = await self.session()
        if not ticks or s is None:
            return
        target = max(0.0, min(self.dur or 1e9, self.pos + ticks * self.step))
        try:
            ok = await asyncio.wait_for(s.try_change_playback_position_async(int(target * TICK)), WINRT_TIMEOUT)
        except Exception as e:
            log.warning("seek failed: %s", e)
            ok = False
        if ok:
            self.pos = target
            self._pending_until = time.monotonic() + 2.0
            await self.set_image(render(self.pos, self.dur, self.playing))
        else:
            await self.deck.show_alert(self.context)

    async def toggle(self):
        s = await self.session()
        if s is None:
            await self.deck.show_alert(self.context)
            return
        try:
            await asyncio.wait_for(s.try_toggle_play_pause_async(), WINRT_TIMEOUT)
        except Exception as e:
            log.warning("play/pause failed: %s", e)
        await asyncio.sleep(0.3)
        await self.tick()

    async def on_key_down(self, payload):
        await self.toggle()

    async def on_dial_down(self, payload):
        await self.toggle()

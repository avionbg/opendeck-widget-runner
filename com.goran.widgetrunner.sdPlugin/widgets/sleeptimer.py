"""Sleep timer: press cycles through durations (default 15 / 30 / 60 min / off); dial rotate adds or
removes 5 minutes, dial press cancels. When it runs out the current media session is paused."""
import asyncio
import logging
import math
import time

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.sleeptimer")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

DEFAULT_DURATIONS = [15, 30, 60]
DIAL_STEP_MIN = 5
WINRT_TIMEOUT = 3.0

BG = (18, 20, 28)
TRACK = (44, 48, 62)
ON = (150, 110, 240)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)


def render(remaining, total) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = cy = SIZE // 2
    r = 56
    d.arc((cx - r, cy - r, cx + r, cy + r), 0, 360, fill=TRACK, width=8)
    if total > 0 and remaining > 0:
        frac = max(0.0, min(1.0, remaining / total))
        d.arc((cx - r, cy - r, cx + r, cy + r), -90, -90 + 360 * frac, fill=ON, width=8)
        m, s = divmod(int(math.ceil(remaining)), 60)
        d.text((cx, cy - 8), f"{m}:{s:02d}", fill=TEXT, font=font(30, "bold"), anchor="mm")
        d.text((cx, cy + 24), "sleep", fill=ON, font=font(14, "semibold"), anchor="mm")
    else:
        # moon glyph
        d.ellipse((cx - 22, cy - 30, cx + 22, cy + 14), fill=DIM)
        d.ellipse((cx - 10, cy - 36, cx + 32, cy + 8), fill=BG)
        d.text((cx, cy + 30), "OFF", fill=DIM, font=font(15, "semibold"), anchor="mm")
    return img


class SleepTimer(Widget):
    action = "com.goran.widgetrunner.sleeptimer"
    _manager = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.end_at = None
        self.total = 0.0
        self._cycle = -1

    @property
    def durations(self):
        raw = str(self.settings.get("durations") or "")
        vals = []
        for part in raw.replace(";", ",").split(","):
            try:
                v = int(float(part.strip()))
                if 1 <= v <= 600:
                    vals.append(v)
            except ValueError:
                pass
        return vals or DEFAULT_DURATIONS

    async def on_appear(self):
        await self.draw()
        self.every(1, self.tick)

    async def tick(self):
        if self.end_at is None:
            return
        remaining = self.end_at - time.monotonic()
        if remaining <= 0:
            self.end_at = None
            self._cycle = -1
            await self.pause_media()
            await self.set_image(render(0, 0))
            await self.deck.show_ok(self.context)
            return
        await self.set_image(render(remaining, self.total))

    async def draw(self):
        await self.set_image(render(self.end_at - time.monotonic() if self.end_at else 0, self.total))

    def start(self, minutes):
        self.total = minutes * 60
        self.end_at = time.monotonic() + self.total

    async def on_key_down(self, payload):
        opts = self.durations
        self._cycle += 1
        if self._cycle >= len(opts):
            self._cycle = -1
            self.end_at = None
        else:
            self.start(opts[self._cycle])
        await self.draw()

    async def on_dial_down(self, payload):
        self.end_at, self._cycle = None, -1
        await self.draw()

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        if not ticks:
            return
        remaining = (self.end_at - time.monotonic()) if self.end_at else 0
        new = remaining + ticks * DIAL_STEP_MIN * 60
        if new <= 0:
            self.end_at, self._cycle = None, -1
        else:
            new = min(new, 240 * 60)
            self.total = max(self.total, new)
            self.end_at = time.monotonic() + new
        await self.draw()

    async def pause_media(self):
        if not HAVE_WINSDK:
            return
        try:
            if SleepTimer._manager is None:
                SleepTimer._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
            s = SleepTimer._manager.get_current_session()
            if s is not None:
                await asyncio.wait_for(s.try_pause_async(), WINRT_TIMEOUT)
                log.info("sleep timer: media paused")
        except Exception as e:
            log.warning("sleep timer could not pause media: %s", e)
            SleepTimer._manager = None

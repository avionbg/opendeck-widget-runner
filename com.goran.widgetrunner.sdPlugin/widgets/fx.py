"""Exchange-rate widget (default EUR -> RSD) via open.er-api.com (free, no key, daily rates).
Base and quote currency are configurable in the property inspector. Press opens Google Finance."""
import asyncio
import logging
from datetime import datetime, timezone

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, fit_text, font, get_json, open_first

log = logging.getLogger("widgetrunner.fx")

REFRESH_SECONDS = 3600
RETRY_SECONDS = 300

BG = (18, 20, 28)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)
ACCENT = (240, 190, 60)
BAD = (230, 80, 80)


def fetch_rate(base, quote):
    data = get_json(f"https://open.er-api.com/v6/latest/{base}")
    if data.get("result") != "success":
        raise LookupError(data.get("error-type") or "api error")
    rate = data["rates"].get(quote)
    if rate is None:
        raise LookupError(f"unknown currency {quote}")
    updated = datetime.fromtimestamp(data.get("time_last_update_unix", 0), tz=timezone.utc).astimezone()
    return float(rate), updated


def fmt_rate(rate):
    if rate >= 100:
        return f"{rate:.2f}"
    if rate >= 10:
        return f"{rate:.3f}"
    return f"{rate:.4f}"


def render(base, quote, rate=None, updated=None, error=None) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    d.text((cx, 14), f"{base} → {quote}", fill=DIM, font=font(16, "semibold"), anchor="mm")
    if error:
        d.text((cx, 72), error, fill=BAD, font=fit_text(d, error, SIZE - 16, 20, "bold"), anchor="mm")
        return img
    txt = fmt_rate(rate)
    d.text((cx, 68), txt, fill=ACCENT, font=fit_text(d, txt, SIZE - 16, 44, "bold"), anchor="mm")
    d.text((cx, 104), f"1 {base}", fill=DIM, font=font(14), anchor="mm")
    if updated:
        d.text((cx, 126), updated.strftime("%d.%m. %H:%M"), fill=DIM, font=font(13), anchor="mm")
    return img


class Fx(Widget):
    action = "com.goran.widgetrunner.fx"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._next = 0.0

    @property
    def base(self):
        return (self.settings.get("base") or "EUR").strip().upper()[:3] or "EUR"

    @property
    def quote(self):
        return (self.settings.get("quote") or "RSD").strip().upper()[:3] or "RSD"

    async def on_appear(self):
        self.every(30, self._maybe_fetch)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        self._next = 0
        await self._maybe_fetch()

    async def on_key_down(self, payload):
        await asyncio.to_thread(open_first, [f"https://www.google.com/finance/quote/{self.base}-{self.quote}"])
        self._next = 0
        await self._maybe_fetch()

    async def _maybe_fetch(self):
        loop = asyncio.get_running_loop()
        if loop.time() < self._next:
            return
        base, quote = self.base, self.quote
        try:
            rate, updated = await asyncio.to_thread(fetch_rate, base, quote)
        except LookupError as e:
            self._next = loop.time() + REFRESH_SECONDS
            await self.set_image(render(base, quote, error=str(e)[:16]))
            return
        except Exception as e:
            log.warning("fx fetch failed: %s", e)
            self._next = loop.time() + RETRY_SECONDS
            await self.set_image(render(base, quote, error="offline"))
            return
        self._next = loop.time() + REFRESH_SECONDS
        await self.set_image(render(base, quote, rate, updated))

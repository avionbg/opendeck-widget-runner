"""Calendar-page date widget. Pressing the key opens a calendar app (configurable)."""
import asyncio
import logging
from datetime import datetime

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font, open_first

log = logging.getLogger("widgetrunner.date")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "June", "July", "Aug", "Sept", "Oct", "Nov", "Dec"]

# What a key press opens when the setting is empty: One Calendar (Store app), then Windows Calendar.
DEFAULT_TARGETS = [
    r"shell:AppsFolder\64885BlueEdge.OneCalendar_8kea50m9krsh2!App",
    "outlookcal:",
]

BG = (18, 20, 28)
BAND = (214, 60, 60)
PAGE = (245, 246, 250)
INK = (30, 32, 40)
RING = (150, 155, 170)


def render(now: datetime) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = 14, 18, SIZE - 14, SIZE - 12
    band_h = 40

    d.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=PAGE)
    d.rounded_rectangle((x0, y0, x1, y0 + band_h + 14), radius=14, fill=BAND)
    d.rectangle((x0, y0 + band_h - 6, x1, y0 + band_h), fill=BAND)  # square off band bottom
    for rx in (x0 + 28, x1 - 28):  # binder rings
        d.rounded_rectangle((rx - 5, y0 - 8, rx + 5, y0 + 14), radius=5, fill=RING)

    d.text(((x0 + x1) // 2, y0 + band_h // 2), MONTHS[now.month - 1], fill=PAGE,
           font=font(26, "bold"), anchor="mm")
    d.text(((x0 + x1) // 2, y0 + band_h + (y1 - y0 - band_h) // 2 + 2), str(now.day), fill=INK,
           font=font(64, "bold"), anchor="mm")
    return img


class DateWidget(Widget):
    action = "com.goran.widgetrunner.date"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._shown = None

    async def on_appear(self):
        self.every(30, self.tick)

    async def tick(self):
        today = datetime.now().date()
        if today != self._shown:
            self._shown = today
            await self.set_image(render(datetime.now()))

    async def on_key_down(self, payload):
        custom = (self.settings.get("open_target") or "").strip()
        targets = [custom] if custom else DEFAULT_TARGETS
        if await asyncio.to_thread(open_first, targets):
            await self.deck.show_ok(self.context)
        else:
            await self.deck.show_alert(self.context)

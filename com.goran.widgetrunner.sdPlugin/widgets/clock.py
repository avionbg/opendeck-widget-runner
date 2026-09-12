"""Clock widget with two modes (property inspector):
- analog: face drawn in the image, digital time sent as the key *title* so all OpenDeck title
  settings apply to it; the face shifts away from wherever the title is placed.
- digital: HH:MM large with seconds smaller underneath, all drawn in the image (title cleared).
"""
import math
from datetime import datetime

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import fit_text, font

SIZE = 144
BG = (18, 20, 28)
RING = (90, 96, 112)
HAND = (235, 238, 245)
SECOND = (230, 70, 70)

# vertical centre / radius of the face depending on where the title sits
LAYOUT = {
    "bottom": (60, 50),
    "top": (84, 50),
    "middle": (72, 62),
}


def render(now: datetime, title_align="bottom", show_seconds=True) -> Image.Image:
    cy, r = LAYOUT.get(title_align, LAYOUT["bottom"])
    cx = SIZE // 2
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)

    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=RING, width=3)
    for i in range(12):
        a = math.radians(i * 30)
        inner = r - (11 if i % 3 == 0 else 6)
        d.line((cx + math.sin(a) * inner, cy - math.cos(a) * inner,
                cx + math.sin(a) * (r - 3), cy - math.cos(a) * (r - 3)),
               fill=RING, width=3 if i % 3 == 0 else 2)

    def hand(angle_deg, length, width, color):
        a = math.radians(angle_deg)
        d.line((cx, cy, cx + math.sin(a) * length, cy - math.cos(a) * length), fill=color, width=width)

    hand((now.hour % 12 + now.minute / 60) * 30, r * 0.52, 6, HAND)
    hand((now.minute + now.second / 60) * 6, r * 0.78, 4, HAND)
    if show_seconds:
        hand(now.second * 6, r * 0.88, 2, SECOND)
    d.ellipse((cx - 4, cy - 4, cx + 4, cy + 4), fill=HAND)
    return img


DIGIT = (235, 238, 245)
SECONDS_COLOR = (150, 156, 172)


def render_digital(now: datetime) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    hhmm = now.strftime("%H:%M")
    d.text((cx, 58), hhmm, fill=DIGIT, font=fit_text(d, hhmm, SIZE - 12, 54, "bold"), anchor="mm")
    d.text((cx, 108), now.strftime("%S"), fill=SECONDS_COLOR, font=font(28, "semibold"), anchor="mm")
    return img


class Clock(Widget):
    action = "com.goran.widgetrunner.clock"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._last_title = None

    @property
    def title_align(self):
        return (self.title_params.get("titleAlignment") or "bottom").lower()

    @property
    def digital(self):
        return (self.settings.get("mode") or "analog").lower() == "digital"

    async def on_appear(self):
        self.every(1, self.tick, align=True)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        self._last_title = None  # mode changed: re-send (analog) or clear (digital) the title
        await self.tick()

    async def on_title_parameters(self, payload):
        # OpenDeck echoes this event after every setTitle, so never send a
        # title from here (that would loop). Only re-render if layout changed.
        before = self.title_align
        await super().on_title_parameters(payload)
        if self.title_align != before and not self.digital:
            await self.set_image(render(datetime.now(), self.title_align))

    async def tick(self):
        now = datetime.now()
        if self.digital:
            await self.set_image(render_digital(now))
            title = ""
        else:
            await self.set_image(render(now, self.title_align))
            title = now.strftime("%H:%M:%S")
        if title != self._last_title:
            self._last_title = title
            await self.set_title(title)

    async def on_key_down(self, payload):
        await self.tick()

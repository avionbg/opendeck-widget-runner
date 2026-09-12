"""Pomodoro timer. Press: start / pause / resume. Hold (>= 1 s): reset.
Focus and break lengths and the end-of-phase beep are configurable in the property inspector."""
import asyncio
import logging
import math
import time

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.pomodoro")

LONG_PRESS = 1.0

BG = (18, 20, 28)
TRACK = (44, 48, 62)
FOCUS = (230, 90, 80)
BREAK = (70, 190, 120)
PAUSED = (150, 156, 172)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)


def render(remaining, total, phase, state) -> Image.Image:
    """phase: 'focus' | 'break'; state: 'idle' | 'running' | 'paused'."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = cy = SIZE // 2
    r = 58
    color = PAUSED if state == "paused" else (BREAK if phase == "break" else FOCUS)
    box = (cx - r, cy - r, cx + r, cy + r)
    d.arc(box, 0, 360, fill=TRACK, width=9)
    if state != "idle" and total > 0:
        frac = max(0.0, min(1.0, 1 - remaining / total))
        if frac > 0:
            d.arc(box, -90, -90 + 360 * frac, fill=color, width=9)
    m, s = divmod(max(0, int(math.ceil(remaining))), 60)
    d.text((cx, cy - 6), f"{m:02d}:{s:02d}", fill=TEXT, font=font(38, "bold"), anchor="mm")
    label = {"idle": "START", "paused": "PAUSED"}.get(state, phase.upper())
    d.text((cx, cy + 26), label, fill=color if state != "idle" else DIM, font=font(15, "semibold"), anchor="mm")
    return img


def beep():
    try:
        import winsound
        for _ in range(2):
            winsound.Beep(880, 150)
            time.sleep(0.08)
    except Exception as e:  # pragma: no cover
        log.debug("beep failed: %s", e)


class Pomodoro(Widget):
    action = "com.goran.widgetrunner.pomodoro"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.state = "idle"
        self.phase = "focus"
        self.remaining = self.focus_seconds
        self._end_at = None
        self._pressed_at = None

    # --- settings ------------------------------------------------------
    def _minutes(self, key, default):
        try:
            return max(1, int(float(self.settings.get(key) or default)))
        except (TypeError, ValueError):
            return default

    @property
    def focus_seconds(self):
        return self._minutes("focus_min", 25) * 60

    @property
    def break_seconds(self):
        return self._minutes("break_min", 5) * 60

    @property
    def sound(self):
        return bool(self.settings.get("sound", True))

    # --- lifecycle -----------------------------------------------------
    async def on_appear(self):
        self.every(1, self.tick, align=True)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        if self.state == "idle":
            self.remaining = self.focus_seconds
        await self.draw()

    async def on_key_down(self, payload):
        self._pressed_at = time.monotonic()

    async def on_key_up(self, payload):
        held = time.monotonic() - (self._pressed_at or time.monotonic())
        self._pressed_at = None
        if held >= LONG_PRESS:
            self.reset()
        elif self.state == "running":
            self.remaining = max(0.0, self._end_at - time.monotonic())
            self.state = "paused"
        else:  # idle or paused -> (re)start
            self._end_at = time.monotonic() + self.remaining
            self.state = "running"
        await self.draw()

    def reset(self):
        self.state, self.phase = "idle", "focus"
        self.remaining = self.focus_seconds
        self._end_at = None

    async def tick(self):
        if self.state != "running":
            return
        self.remaining = self._end_at - time.monotonic()
        if self.remaining <= 0:
            # phase finished: switch and keep running
            self.phase = "break" if self.phase == "focus" else "focus"
            self.remaining = self.break_seconds if self.phase == "break" else self.focus_seconds
            self._end_at = time.monotonic() + self.remaining
            if self.sound:
                asyncio.get_running_loop().run_in_executor(None, beep)
        await self.draw()

    async def draw(self):
        total = self.break_seconds if self.phase == "break" else self.focus_seconds
        await self.set_image(render(self.remaining, total, self.phase, self.state))

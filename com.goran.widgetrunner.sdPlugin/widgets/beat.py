"""Beat pulse: onset detection on the bass of whatever is playing (shared loopback capture) - the
key pulses on every beat and shows an estimated BPM."""
import asyncio
import logging
import time

import numpy as np
from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font
from widgets._audio import loopback

log = logging.getLogger("widgetrunner.beat")

FPS = 25
WINDOW = 1024
HISTORY = 43              # ~1 s of frames for the running average
SENSITIVITY = 1.35        # onset when energy > SENSITIVITY * average
MIN_ENERGY = 1e-6
REFRACTORY = 0.25         # s between beats (240 BPM max)
LOW_HZ, HIGH_HZ = 40, 160

BG = (18, 20, 28)
RING = (44, 48, 62)
PULSE = (230, 80, 120)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)


def render(pulse, bpm, active, show_bpm=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    if show_bpm:
        cx, cy, base, grow = SIZE // 2, 58, 22, 30
    else:  # no label: the dot uses the whole key
        cx, cy, base, grow = SIZE // 2, SIZE // 2, 38, 34
    r = base + int(grow * pulse)
    glow = tuple(int(BG[i] + (PULSE[i] - BG[i]) * min(1.0, pulse * 0.6)) for i in range(3))
    d.ellipse((cx - r - 10, cy - r - 10, cx + r + 10, cy + r + 10), fill=glow)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=PULSE if active else RING)
    if show_bpm:
        label = f"{bpm:.0f} BPM" if bpm else ("listening" if active else "silent")
        d.text((cx, 120), label, fill=TEXT if bpm else DIM, font=font(18 if bpm else 14, "bold" if bpm else "regular"), anchor="mm")
    return img


class Beat(Widget):
    action = "com.goran.widgetrunner.beat"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.energies = []
        self.pulse = 0.0
        self.last_beat = 0.0
        self.intervals = []
        self.bpm = 0.0
        self._idle_drawn = False
        self._window = np.hanning(WINDOW).astype(np.float32)

    @property
    def show_bpm(self):
        return bool(self.settings.get("show_bpm", True))

    async def on_appear(self):
        loopback.acquire(self)
        self._tasks.append(asyncio.create_task(self._loop()))

    async def on_settings(self, settings):
        await super().on_settings(settings)
        self._idle_drawn = False
        await self.set_image(render(self.pulse, self.bpm, loopback.active(), self.show_bpm))

    async def on_disappear(self):
        await super().on_disappear()
        loopback.release(self)

    async def on_key_down(self, payload):
        self.intervals.clear()
        self.bpm = 0.0

    async def _loop(self):
        while True:
            try:
                await self.frame()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("beat frame failed")
            await asyncio.sleep(1 / FPS if loopback.active() or self.pulse > 0.02 else 0.5)

    def bass_energy(self):
        x = loopback.latest(WINDOW) * self._window
        spec = np.abs(np.fft.rfft(x)) ** 2
        freqs = np.fft.rfftfreq(WINDOW, 1.0 / loopback.rate)
        sel = (freqs >= LOW_HZ) & (freqs <= HIGH_HZ)
        return float(spec[sel].sum())

    async def frame(self):
        active = loopback.active()
        if not active and self.pulse <= 0.02:
            if not self._idle_drawn:
                self._idle_drawn = True
                self.energies.clear()
                await self.set_image(render(0.0, self.bpm if self.intervals else 0.0, False, self.show_bpm))
            return
        self._idle_drawn = False
        e = self.bass_energy() if active else 0.0
        avg = sum(self.energies) / len(self.energies) if self.energies else 0.0
        now = time.monotonic()
        if active and e > MIN_ENERGY and len(self.energies) >= 10 and e > SENSITIVITY * avg and now - self.last_beat > REFRACTORY:
            if self.last_beat and now - self.last_beat < 2.0:
                self.intervals.append(now - self.last_beat)
                self.intervals = self.intervals[-12:]
                if len(self.intervals) >= 4:
                    med = float(np.median(self.intervals))
                    bpm = 60.0 / med
                    while bpm > 180:
                        bpm /= 2
                    while bpm < 60:
                        bpm *= 2
                    self.bpm = bpm
            self.last_beat = now
            self.pulse = 1.0
        else:
            self.pulse *= 0.80
        self.energies.append(e)
        self.energies = self.energies[-HISTORY:]
        await self.set_image(render(self.pulse, self.bpm, active, self.show_bpm))

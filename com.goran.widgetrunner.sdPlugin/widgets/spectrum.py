"""Real-time spectrum analyser of whatever plays on the default output.

Captures the output mix with WASAPI loopback (IAudioClient + IAudioCaptureClient via ctypes, in a
background thread), runs an FFT with numpy and shows log-spaced frequency bands (bass left, treble
right) as bars with peak caps. ~12 fps while there is sound, idle when silent.
Property inspector: number of bands, sensitivity, peak caps.
"""
import asyncio
import logging
import time

import numpy as np
from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE

from widgets._audio import loopback as _loopback

log = logging.getLogger("widgetrunner.spectrum")

FFT_SIZE = 4096
FPS_ACTIVE, FPS_IDLE = 12, 2
DECAY = 0.72
CAP_FALL = 0.03
F_LOW, F_HIGH = 40.0, 16000.0
DEFAULT_BANDS = 24
DEFAULT_RANGE_DB = 60   # dynamic range shown; lower = more sensitive

BG = (18, 20, 28)
TRACK = (36, 40, 52)
LOW = (70, 190, 120)
MID = (240, 190, 60)
HIGH = (230, 80, 80)
CAP = (235, 238, 245)


# ---------------------------------------------------------------- analysis
_window = np.hanning(FFT_SIZE).astype(np.float32)
_window_gain = _window.sum()


def band_edges(n_bands, rate):
    hi = min(F_HIGH, rate / 2 - 1)
    return np.geomspace(F_LOW, hi, n_bands + 1)


def analyse(samples, rate, n_bands, range_db):
    """-> n_bands values in 0..1 (dB scale, 0 dB = full-scale sine)."""
    if len(samples) < FFT_SIZE:
        samples = np.pad(samples, (FFT_SIZE - len(samples), 0))
    spec = np.abs(np.fft.rfft(samples[-FFT_SIZE:] * _window)) * (2.0 / _window_gain)
    freqs = np.fft.rfftfreq(FFT_SIZE, 1.0 / rate)
    edges = band_edges(n_bands, rate)
    out = np.zeros(n_bands, dtype=np.float32)
    for i in range(n_bands):
        lo, hi = edges[i], edges[i + 1]
        sel = (freqs >= lo) & (freqs < hi)
        if not sel.any():  # narrow low bands may fall between bins: take the nearest bin
            sel = np.argmin(np.abs(freqs - (lo + hi) / 2))
        peak = spec[sel].max() if np.ndim(spec[sel]) else spec[sel]
        db = 20 * np.log10(peak + 1e-9)
        out[i] = np.clip((db + range_db) / range_db, 0.0, 1.0)
    return out


# ---------------------------------------------------------------- render
def render(bars, caps, show_caps=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    n = len(bars)
    top, bottom = 10, SIZE - 10
    slot = (SIZE - 12) / n
    gap = 1 if n > 24 else 2
    h_total = bottom - top
    for i, v in enumerate(bars):
        x0 = 6 + i * slot
        x1 = x0 + slot - gap
        d.rectangle((x0, top, x1, bottom), fill=TRACK)
        h = h_total * float(v)
        if h >= 1:
            # three colour zones: green up to 60 %, yellow to 85 %, red above
            zones = [(0.0, 0.6, LOW), (0.6, 0.85, MID), (0.85, 1.0, HIGH)]
            for z0, z1, col in zones:
                za, zb = h_total * z0, min(h, h_total * z1)
                if zb > za:
                    d.rectangle((x0, bottom - zb, x1, bottom - za), fill=col)
        if show_caps and caps is not None and caps[i] > 0.02:
            y = bottom - h_total * float(caps[i])
            d.rectangle((x0, y - 1, x1, y + 1), fill=CAP)
    return img


# ---------------------------------------------------------------- widget
class Spectrum(Widget):
    action = "com.goran.widgetrunner.spectrum"
    _loopback = _loopback
    _instances = set()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.bars = np.zeros(self.n_bands, dtype=np.float32)
        self.caps = np.zeros(self.n_bands, dtype=np.float32)
        self._idle = 0
        self._drawn_flat = False
        Spectrum._instances.add(self)

    @property
    def n_bands(self):
        try:
            return max(8, min(48, int(self.settings.get("bands") or DEFAULT_BANDS)))
        except (TypeError, ValueError):
            return DEFAULT_BANDS

    @property
    def range_db(self):
        try:
            return max(30, min(90, int(self.settings.get("range_db") or DEFAULT_RANGE_DB)))
        except (TypeError, ValueError):
            return DEFAULT_RANGE_DB

    @property
    def show_caps(self):
        return bool(self.settings.get("caps", True))

    async def on_appear(self):
        Spectrum._loopback.acquire(self)
        self._tasks.append(asyncio.create_task(self._loop()))

    async def on_disappear(self):
        await super().on_disappear()
        Spectrum._instances.discard(self)
        Spectrum._loopback.release(self)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        self.bars = np.zeros(self.n_bands, dtype=np.float32)
        self.caps = np.zeros(self.n_bands, dtype=np.float32)
        await self.frame(force=True)

    async def on_key_down(self, payload):
        # quick sensitivity toggle without opening the panel: 60 dB <-> 45 dB
        self.settings["range_db"] = 45 if self.range_db == 60 else 60
        await self.deck.set_settings(self.context, self.settings)
        await self.frame(force=True)

    async def _loop(self):
        while True:
            try:
                await self.frame()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("spectrum frame failed")
            await asyncio.sleep(1 / (FPS_IDLE if self._idle > FPS_ACTIVE else FPS_ACTIVE))

    async def frame(self, force=False):
        lb = Spectrum._loopback
        active = lb.active()
        if active or self.bars.max() > 0.01 or self.caps.max() > 0.02 or force:
            self._idle = 0 if active else self._idle + 1
            self._drawn_flat = False
            values = analyse(lb.latest(FFT_SIZE), lb.rate, self.n_bands, self.range_db) if active else np.zeros(self.n_bands, dtype=np.float32)
            self.bars = np.maximum(values, self.bars * DECAY)
            self.caps = np.where(self.bars >= self.caps, self.bars, np.maximum(0, self.caps - CAP_FALL))
            await self.set_image(render(self.bars, self.caps if self.show_caps else None, self.show_caps))
        else:
            self._idle += 1
            if not self._drawn_flat:
                self._drawn_flat = True
                await self.set_image(render(self.bars * 0, None, False))

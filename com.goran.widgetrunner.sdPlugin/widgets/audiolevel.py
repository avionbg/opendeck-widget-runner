"""Stereo level meter for the default output (what you hear), driven by the Core Audio peak meter
(IAudioMeterInformation) - nothing is recorded, just the per-channel peak level.

The key is split in two halves with a small gap: left channel on the left, right channel on the
right. Each half is a stack of horizontal segments that jump up and down with the level, plus a
peak-hold line that falls slowly. ~12 fps while there is sound, idle when silent.
"""
import asyncio
import ctypes
import logging
import math
from ctypes import wintypes as wt

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font
from widgets.mic import GUID, _method, _release, CLSCTX_ALL, CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator, eConsole, eRender

log = logging.getLogger("widgetrunner.audiolevel")

IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
FPS_ACTIVE, FPS_IDLE = 12, 2
DB_RANGE = 50.0          # dB below full scale shown as zero
DECAY = 0.78             # per-frame fall of the smoothed level
PEAK_HOLD_FRAMES = 10
PEAK_FALL = 0.03
SEGMENTS = 18            # horizontal lines per channel
GAP = 6                  # px between the two halves

BG = (18, 20, 28)
TRACK = (34, 38, 50)
LOW = (70, 190, 120)
MID = (240, 190, 60)
HIGH = (230, 80, 80)
PEAK = (235, 238, 245)
DIM = (110, 116, 130)


# ---------------------------------------------------------------- meter
class Meter:
    """Holds the COM meter object; created lazily on the calling thread (the asyncio loop)."""

    def __init__(self):
        self._meter = None
        self._channels = 0
        self._buf = None

    def _open(self):
        ole32 = ctypes.oledll.ole32
        ole32.CoInitialize(None)
        enum, dev, meter = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            ole32.CoCreateInstance(ctypes.byref(GUID.from_str(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                                   ctypes.byref(GUID.from_str(IID_IMMDeviceEnumerator)), ctypes.byref(enum))
            _method(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                enum, eRender, eConsole, ctypes.byref(dev))
            _method(dev, 3, ctypes.HRESULT, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(GUID.from_str(IID_IAudioMeterInformation)),
                                                     CLSCTX_ALL, None, ctypes.byref(meter))
            n = wt.UINT()
            _method(meter, 4, ctypes.HRESULT, ctypes.POINTER(wt.UINT))(meter, ctypes.byref(n))
            self._meter, self._channels = meter, max(1, n.value)
            self._buf = (ctypes.c_float * self._channels)()
        finally:
            _release(dev)
            _release(enum)

    def peaks(self):
        """-> list of linear peak values (0..1) per channel; reopens the device on failure."""
        try:
            if self._meter is None:
                self._open()
            _method(self._meter, 5, ctypes.HRESULT, wt.UINT, ctypes.POINTER(ctypes.c_float))(
                self._meter, self._channels, self._buf)
            return list(self._buf)
        except OSError as e:
            log.debug("meter read failed, reopening: %s", e)
            self.close()
            return [0.0, 0.0]

    def close(self):
        _release(self._meter)
        self._meter = None


def to_db_scale(peak):
    """linear 0..1 -> 0..1 on a dB scale so quiet passages are still visible."""
    if peak <= 1e-5:
        return 0.0
    return max(0.0, min(1.0, 1.0 + 20 * math.log10(peak) / DB_RANGE))


def seg_color(frac):
    return HIGH if frac > 0.85 else MID if frac > 0.6 else LOW


# ---------------------------------------------------------------- render
def render(levels, peaks) -> Image.Image:
    """levels/peaks: two values 0..1 (L, R)."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    top, bottom = 10, SIZE - 18
    half = (SIZE - 2 * 8 - GAP) / 2
    seg_h = (bottom - top) / SEGMENTS
    line_h = max(2, seg_h - 2)
    for j in range(2):
        x0 = 8 + j * (half + GAP)
        x1 = x0 + half
        lit = int(round(levels[j] * SEGMENTS))
        for s in range(SEGMENTS):
            y1 = bottom - s * seg_h
            y0 = y1 - line_h
            frac = (s + 1) / SEGMENTS
            d.rectangle((x0, y0, x1, y1), fill=seg_color(frac) if s < lit else TRACK)
        if peaks[j] > 0.02:
            y = bottom - (bottom - top) * peaks[j]
            d.rectangle((x0, y - 1, x1, y + 1), fill=PEAK)
        d.text(((x0 + x1) / 2, SIZE - 8), "LR"[j], fill=DIM, font=font(12, "semibold"), anchor="mm")
    return img


# ---------------------------------------------------------------- widget
class AudioLevel(Widget):
    action = "com.goran.widgetrunner.audiolevel"
    _meter = Meter()

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.levels = [0.0, 0.0]
        self.peaks = [0.0, 0.0]
        self.hold = [0, 0]
        self._idle = 0
        self._drawn_flat = False

    async def on_appear(self):
        self._tasks.append(asyncio.create_task(self._loop()))

    async def on_key_down(self, payload):
        await self.frame(force=True)

    async def _loop(self):
        while True:
            try:
                await self.frame()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("audio level frame failed")
            await asyncio.sleep(1 / (FPS_IDLE if self._idle > FPS_ACTIVE else FPS_ACTIVE))

    def sample(self):
        raw = [to_db_scale(p) for p in self._meter.peaks()]
        if len(raw) == 1:
            raw = raw * 2
        raw = raw[:2]
        for j, v in enumerate(raw):
            self.levels[j] = v if v > self.levels[j] else self.levels[j] * DECAY
            if v >= self.peaks[j]:
                self.peaks[j], self.hold[j] = v, 0
            else:
                self.hold[j] += 1
                if self.hold[j] > PEAK_HOLD_FRAMES:
                    self.peaks[j] = max(0.0, self.peaks[j] - PEAK_FALL)
        return max(raw)

    async def frame(self, force=False):
        level = self.sample()
        moving = level > 0.01 or max(self.levels) > 0.02 or max(self.peaks) > 0.02
        self._idle = 0 if moving else self._idle + 1
        if moving or force or not self._drawn_flat:
            self._drawn_flat = not moving
            await self.set_image(render(self.levels, self.peaks))

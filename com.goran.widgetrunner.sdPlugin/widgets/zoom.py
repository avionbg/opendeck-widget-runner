"""Screen zoom without the Magnifier window, via the Windows Magnification API (Magnification.dll).

Key: press toggles between 1x and the configured level.
Dial: rotate changes the level in steps, press toggles like the key (1x <-> configured level).
Zooming is animated toward the mouse cursor; while zoomed, the view follows the cursor either
Magnifier-style (pan only when the cursor reaches the edge) or keeping the cursor centred.
The zoom lives as long as this process does and resets when the last Zoom key disappears.
"""
import asyncio
import atexit
import ctypes
import logging
from ctypes import wintypes

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.zoom")

MIN_LEVEL, MAX_LEVEL = 1.0, 8.0
FOLLOW_INTERVAL = 1 / 60
ANIM_SECONDS = 0.25
EDGE_MARGIN = 2  # px from the viewport edge before it starts panning ("edge" follow mode)

BG = (18, 20, 28)
ON = (80, 150, 240)
OFF = (110, 116, 130)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)

user32 = ctypes.windll.user32
_mag = None


def _magnification():
    """Lazily load Magnification.dll and make the process DPI-aware so cursor coords are real pixels."""
    global _mag
    if _mag is None:
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass
        m = ctypes.WinDLL("Magnification.dll")
        m.MagInitialize.restype = ctypes.c_bool
        m.MagUninitialize.restype = ctypes.c_bool
        m.MagSetFullscreenTransform.argtypes = [ctypes.c_float, ctypes.c_int, ctypes.c_int]
        m.MagSetFullscreenTransform.restype = ctypes.c_bool
        if not m.MagInitialize():
            raise OSError("MagInitialize failed")
        atexit.register(lambda: (m.MagSetFullscreenTransform(1.0, 0, 0), m.MagUninitialize()))
        _mag = m
    return _mag


def virtual_screen():
    _magnification()  # DPI awareness must be on before reading metrics, or they come back scaled
    x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    return x, y, w, h


def cursor_pos():
    _magnification()
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def ease(t):
    return 1 - (1 - t) ** 3  # ease-out cubic


class Viewport:
    """Current full-screen magnification state, shared by every Zoom instance (one screen)."""
    level = 1.0
    ox = 0.0
    oy = 0.0

    @classmethod
    def size(cls, level=None):
        sx, sy, sw, sh = virtual_screen()
        lv = level or cls.level
        return sx, sy, sw, sh, sw / lv, sh / lv

    @classmethod
    def clamp(cls, ox, oy, level):
        sx, sy, sw, sh, vw, vh = cls.size(level)
        return min(max(ox, sx), sx + sw - vw), min(max(oy, sy), sy + sh - vh)

    @classmethod
    def push(cls):
        m = _magnification()
        if cls.level <= 1.0:
            return m.MagSetFullscreenTransform(1.0, 0, 0)
        return m.MagSetFullscreenTransform(cls.level, int(round(cls.ox)), int(round(cls.oy)))

    @classmethod
    def set(cls, level, anchor=None):
        """Change the level keeping the screen point `anchor` fixed (zoom toward the cursor)."""
        _, _, _, _, vw, vh = cls.size()
        if cls.level <= 1.0:
            sx, sy, sw, sh = virtual_screen()
            cls.ox, cls.oy, vw, vh = sx, sy, sw, sh  # at 1x the viewport is the whole screen
        if anchor is None:
            ax, ay = cls.ox + vw / 2, cls.oy + vh / 2
        else:
            ax, ay = anchor
        fx, fy = (ax - cls.ox) / vw, (ay - cls.oy) / vh
        cls.level = level
        _, _, _, _, nvw, nvh = cls.size(level)
        cls.ox, cls.oy = cls.clamp(ax - fx * nvw, ay - fy * nvh, level)
        cls.push()

    @classmethod
    def follow(cls, cx, cy, mode):
        """Pan the viewport so the cursor stays visible. Returns True if it moved."""
        _, _, _, _, vw, vh = cls.size()
        if mode == "center":
            nx, ny = cx - vw / 2, cy - vh / 2
        else:  # edge: only move when the cursor reaches the viewport edge
            nx, ny = cls.ox, cls.oy
            if cx < cls.ox + EDGE_MARGIN:
                nx = cx - EDGE_MARGIN
            elif cx > cls.ox + vw - EDGE_MARGIN:
                nx = cx - vw + EDGE_MARGIN
            if cy < cls.oy + EDGE_MARGIN:
                ny = cy - EDGE_MARGIN
            elif cy > cls.oy + vh - EDGE_MARGIN:
                ny = cy - vh + EDGE_MARGIN
        nx, ny = cls.clamp(nx, ny, cls.level)
        if (nx, ny) != (cls.ox, cls.oy):
            cls.ox, cls.oy = nx, ny
            cls.push()
            return True
        return False


def render(level, active) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    color = ON if active else OFF
    cx, cy, r = 62, 58, 30
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=6)
    d.line((cx + r * 0.7, cy + r * 0.7, cx + r + 22, cy + r + 22), fill=color, width=9)
    d.line((cx - 12, cy, cx + 12, cy), fill=color, width=4)
    if active:
        d.line((cx, cy - 12, cx, cy + 12), fill=color, width=4)
    txt = f"{level:.2g}×" if active else "1×"
    d.text((SIZE // 2, 124), txt, fill=TEXT if active else DIM, font=font(20, "bold"), anchor="mm")
    return img


class Zoom(Widget):
    action = "com.goran.widgetrunner.zoom"
    _instances = set()
    _anim_task = None
    _follow_task = None
    _target = 1.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        Zoom._instances.add(self)

    @property
    def level(self):
        try:
            return min(MAX_LEVEL, max(1.25, float(self.settings.get("level") or 2.0)))
        except (TypeError, ValueError):
            return 2.0

    @property
    def step(self):
        try:
            return min(2.0, max(0.1, float(self.settings.get("step") or 0.25)))
        except (TypeError, ValueError):
            return 0.25

    @property
    def follow_mode(self):
        return (self.settings.get("follow") or "edge").lower()  # edge | center | off

    async def on_appear(self):
        await self.draw()

    async def on_disappear(self):
        await super().on_disappear()
        Zoom._instances.discard(self)
        if not Zoom._instances:
            await Zoom.zoom_to(1.0, animate=False)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        await self.draw()

    async def toggle(self):
        """Zoomed -> back to 1x; at 1x -> the configured level."""
        await Zoom.zoom_to(1.0 if Zoom._target > 1.0 else self.level, follow=self.follow_mode)

    async def on_key_down(self, payload):
        await self.toggle()

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        new = min(MAX_LEVEL, max(MIN_LEVEL, round(Zoom._target + ticks * self.step, 2)))
        await Zoom.zoom_to(new, follow=self.follow_mode)

    async def on_dial_down(self, payload):
        await self.toggle()

    async def draw(self):
        await self.set_image(render(Zoom._target, Zoom._target > 1.0))

    # --- shared behaviour ----------------------------------------------
    @classmethod
    async def zoom_to(cls, level, follow="edge", animate=True):
        cls._target = level
        if cls._anim_task and not cls._anim_task.done():
            cls._anim_task.cancel()
        cls._anim_task = asyncio.create_task(cls._animate(level, follow, animate))
        for inst in list(cls._instances):
            await inst.draw()

    @classmethod
    async def _animate(cls, target, follow, animate):
        try:
            anchor = cursor_pos() if follow != "off" else None
            loop = asyncio.get_running_loop()
            start, t0 = Viewport.level, loop.time()
            if animate and abs(target - start) > 0.01:
                while True:
                    t = (loop.time() - t0) / ANIM_SECONDS
                    if t >= 1:
                        break
                    Viewport.set(start + (target - start) * ease(t), anchor)
                    await asyncio.sleep(1 / 60)
            Viewport.set(target, anchor)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("zoom failed: %s", e)
            return
        if target > 1.0 and follow != "off":
            if cls._follow_task is None or cls._follow_task.done():
                cls._follow_task = asyncio.create_task(cls._follow_loop(follow))
        elif cls._follow_task:
            cls._follow_task.cancel()
            cls._follow_task = None

    @classmethod
    async def _follow_loop(cls, mode):
        last = None
        while Viewport.level > 1.0:
            if cls._anim_task is None or cls._anim_task.done():  # don't fight the animation
                pos = cursor_pos()
                if pos != last:
                    last = pos
                    Viewport.follow(*pos, mode)
            await asyncio.sleep(FOLLOW_INTERVAL)

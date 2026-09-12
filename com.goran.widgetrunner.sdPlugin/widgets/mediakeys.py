"""Media keys: Play/Pause (shows the current state), Next and Previous. They act through Windows
media controls on the current session, and fall back to the keyboard media keys when no app has
registered a session (so they always do something)."""
import asyncio
import ctypes
import logging

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.mediakeys")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

WINRT_TIMEOUT = 3.0
POLL_SECONDS = 1.0
VK = {"toggle": 0xB3, "next": 0xB0, "prev": 0xB1, "stop": 0xB2}

BG = (18, 20, 28)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)
ACCENT = (80, 150, 240)


def press_media_key(vk):
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, 2, 0)   # KEYEVENTF_KEYUP


def render(kind, playing=None, has_media=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx, cy = SIZE // 2, 62
    color = TEXT if has_media else DIM
    if kind == "toggle":
        if playing:
            d.rounded_rectangle((cx - 24, cy - 26, cx - 7, cy + 26), radius=4, fill=color)
            d.rounded_rectangle((cx + 7, cy - 26, cx + 24, cy + 26), radius=4, fill=color)
            label = "pause"
        else:
            d.polygon([(cx - 20, cy - 28), (cx - 20, cy + 28), (cx + 28, cy)], fill=color)
            label = "play"
    elif kind == "next":
        d.polygon([(cx - 28, cy - 24), (cx - 28, cy + 24), (cx + 2, cy)], fill=color)
        d.polygon([(cx - 2, cy - 24), (cx - 2, cy + 24), (cx + 28, cy)], fill=color)
        d.rounded_rectangle((cx + 24, cy - 24, cx + 30, cy + 24), radius=2, fill=color)
        label = "next"
    else:
        d.polygon([(cx + 28, cy - 24), (cx + 28, cy + 24), (cx - 2, cy)], fill=color)
        d.polygon([(cx + 2, cy - 24), (cx + 2, cy + 24), (cx - 28, cy)], fill=color)
        d.rounded_rectangle((cx - 30, cy - 24, cx - 24, cy + 24), radius=2, fill=color)
        label = "previous"
    d.text((cx, 122), label, fill=DIM, font=font(14, "semibold"), anchor="mm")
    return img


class _MediaKey(Widget):
    kind = "toggle"
    _manager = None

    async def session(self):
        if not HAVE_WINSDK:
            return None
        try:
            if _MediaKey._manager is None:
                _MediaKey._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
            return _MediaKey._manager.get_current_session()
        except Exception as e:
            log.debug("session failed: %s", e)
            _MediaKey._manager = None
            return None

    async def on_appear(self):
        await self.set_image(render(self.kind))

    async def act(self):
        s = await self.session()
        ok = False
        if s is not None:
            try:
                fn = {"toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                      "prev": s.try_skip_previous_async}[self.kind]
                ok = await asyncio.wait_for(fn(), WINRT_TIMEOUT)
            except Exception as e:
                log.debug("smtc %s failed: %s", self.kind, e)
        if not ok:
            press_media_key(VK[self.kind])   # keyboard fallback, works for anything that listens to media keys
        await self.deck.show_ok(self.context)

    async def on_key_down(self, payload):
        await self.act()

    async def on_dial_down(self, payload):
        await self.act()


class PlayPause(_MediaKey):
    action = "com.goran.widgetrunner.playpause"
    kind = "toggle"

    async def on_appear(self):
        self.every(POLL_SECONDS, self.tick)

    async def tick(self):
        s = await self.session()
        if s is None:
            await self.set_image(render("toggle", False, False))
            return
        try:
            playing = s.get_playback_info().playback_status.name == "PLAYING"
        except Exception:
            playing = False
        await self.set_image(render("toggle", playing, True))

    async def act(self):
        await super().act()
        await asyncio.sleep(0.3)
        await self.tick()


class NextTrack(_MediaKey):
    action = "com.goran.widgetrunner.next"
    kind = "next"


class PrevTrack(_MediaKey):
    action = "com.goran.widgetrunner.prev"
    kind = "prev"

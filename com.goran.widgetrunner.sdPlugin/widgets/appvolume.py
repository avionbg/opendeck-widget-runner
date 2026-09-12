"""App volume: dial controls the volume of the application that is playing music (its Windows
audio session), not the whole system. Press = mute / unmute that app.

The app is the one Windows media controls report as playing (e.g. Chrome); if that can't be matched
to an audio session, the loudest non-system session is used instead."""
import asyncio
import logging

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, fit_text, font
from widgets._audio import app_sessions, set_app_mute, set_app_volume

log = logging.getLogger("widgetrunner.appvolume")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

POLL_SECONDS = 1.0
WINRT_TIMEOUT = 3.0
DEFAULT_STEP = 2

BG = (18, 20, 28)
TRACK = (44, 48, 62)
ON = (70, 190, 120)
MUTED = (230, 80, 80)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)

IGNORE = {"python.exe", "pythonw.exe"}   # our own loopback capture shows up as a session
PRETTY = {"mpv.exe": "Radio (mpv)", "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox", "spotify.exe": "Spotify",
          "vlc.exe": "VLC", "foobar2000.exe": "foobar", "brave.exe": "Brave", "opera.exe": "Opera"}


def render(app, level, muted, found=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    if not found:
        d.ellipse((cx - 16, 56, cx + 2, 74), fill=DIM)          # note head
        d.line((cx + 1, 65, cx + 1, 30), fill=DIM, width=4)       # stem
        d.line((cx + 1, 30, cx + 20, 36), fill=DIM, width=4)      # flag
        d.text((cx, 104), "no audio app", fill=DIM, font=font(13), anchor="mm")
        return img
    color = MUTED if muted else ON
    d.text((cx, 22), app, fill=DIM, font=fit_text(d, app, SIZE - 16, 15, "semibold"), anchor="mm")
    d.text((cx, 66), "muted" if muted else f"{level}%", fill=color if muted else TEXT, font=font(34, "bold"), anchor="mm")
    d.rounded_rectangle((14, 108, SIZE - 14, 120), radius=6, fill=TRACK)
    if level > 0 and not muted:
        d.rounded_rectangle((14, 108, 14 + (SIZE - 28) * level // 100, 120), radius=6, fill=color)
    return img


class AppVolume(Widget):
    action = "com.goran.widgetrunner.appvolume"
    _manager = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.names = set()      # exe names of the sessions we control
        self.label = ""
        self.level = 0
        self.muted = False
        self.found = False

    @property
    def step(self):
        try:
            return max(1, min(20, int(self.settings.get("step") or DEFAULT_STEP)))
        except (TypeError, ValueError):
            return DEFAULT_STEP

    async def on_appear(self):
        self.every(POLL_SECONDS, self.tick)

    async def media_app(self):
        """Executable-name guess for the app Windows reports as the current media source."""
        if not HAVE_WINSDK:
            return ""
        try:
            if AppVolume._manager is None:
                AppVolume._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
            s = AppVolume._manager.get_current_session()
            if s is None or s.get_playback_info().playback_status.name != "PLAYING":
                return ""          # a paused browser must not win over an app that is actually playing (e.g. mpv)
            aumid = s.source_app_user_model_id or ""
            if "!" in aumid:                       # packaged app: 'Publisher.App_hash!App' -> app.exe
                aumid = aumid.split("!")[-1]
            return aumid.lower().replace(" ", "") + ".exe"
        except Exception as e:
            log.debug("media app failed: %s", e)
            AppVolume._manager = None
            return ""

    async def resolve(self):
        sessions = await asyncio.to_thread(app_sessions)
        wanted = await self.media_app()
        apps = [s for s in sessions if not s["system"] and s["name"] and s["name"] not in IGNORE]
        match = [s for s in apps if s["name"] == wanted]
        if not match and apps:
            loud = max(apps, key=lambda s: s["peak"])
            if loud["peak"] > 0.001:
                match = [s for s in apps if s["name"] == loud["name"]]
        if not match and self.names:
            match = [s for s in apps if s["name"] in self.names]   # keep the last app while it is silent
        if not match:
            self.found = False
            return
        self.found = True
        self.names = {s["name"] for s in match}
        exe = match[0]["name"]
        self.label = PRETTY.get(exe, exe[:-4] if exe.endswith(".exe") else exe)
        self.level = max(s["volume"] for s in match)
        self.muted = all(s["muted"] for s in match)

    async def tick(self):
        try:
            await self.resolve()
        except Exception as e:
            log.warning("app volume read failed: %s", e)
            return
        await self.set_image(render(self.label, self.level, self.muted, self.found))

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        if not ticks or not self.names:
            return
        new = max(0, min(100, self.level + ticks * self.step))
        try:
            await asyncio.to_thread(set_app_volume, self.names, new)
            if self.muted and ticks > 0:
                await asyncio.to_thread(set_app_mute, self.names, False)
                self.muted = False
        except Exception as e:
            log.warning("app volume set failed: %s", e)
            return
        self.level = new
        await self.set_image(render(self.label, self.level, self.muted, True))

    async def toggle_mute(self):
        if not self.names:
            await self.deck.show_alert(self.context)
            return
        try:
            await asyncio.to_thread(set_app_mute, self.names, not self.muted)
        except Exception as e:
            log.warning("app mute failed: %s", e)
            return
        self.muted = not self.muted
        await self.set_image(render(self.label, self.level, self.muted, True))

    async def on_key_down(self, payload):
        await self.toggle_mute()

    async def on_dial_down(self, payload):
        await self.toggle_mute()

"""Find the current track on the *other* platform: playing from a browser (YouTube) -> search on
Spotify; playing from Spotify -> search on YouTube. Press opens the search in the default browser.
Property inspector: force the target (auto / Spotify / YouTube) and Spotify web vs desktop app."""
import asyncio
import logging
import re
import urllib.parse

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, fit_text, font, open_first
from widgets.lyrics import parse_track

log = logging.getLogger("widgetrunner.crossplay")

try:
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as _Mgr
    HAVE_WINSDK = True
except ImportError:  # pragma: no cover
    HAVE_WINSDK = False

POLL_SECONDS = 2.0
WINRT_TIMEOUT = 3.0

BG = (18, 20, 28)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)
YT = (255, 0, 0)
SP = (30, 215, 96)


def logo(d, target, cx, cy):
    if target == "youtube":
        d.rounded_rectangle((cx - 30, cy - 21, cx + 30, cy + 21), radius=12, fill=YT)
        d.polygon([(cx - 9, cy - 12), (cx - 9, cy + 12), (cx + 13, cy)], fill=(255, 255, 255))
    else:
        d.ellipse((cx - 26, cy - 26, cx + 26, cy + 26), fill=SP)
        for i, (w, r) in enumerate(((5, 22), (4, 16), (3, 10))):
            y = cy - 8 + i * 9
            d.arc((cx - r, y - 6, cx + r, y + 12), 200, 340, fill=(18, 20, 28), width=w)


def render(target, title, found=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    logo(d, target, cx, 50)
    d.text((cx, 96), "find on " + ("YouTube" if target == "youtube" else "Spotify"), fill=TEXT, font=font(14, "semibold"), anchor="mm")
    sub = title if found else "nothing playing"
    d.text((cx, 122), sub, fill=DIM, font=fit_text(d, sub, SIZE - 12, 12, min_size=10), anchor="mm")
    return img


class CrossPlay(Widget):
    action = "com.goran.widgetrunner.crossplay"
    _manager = None

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.title = ""
        self.artist = ""
        self.source = ""
        self.found = False

    @property
    def forced_target(self):
        return (self.settings.get("target") or "auto").lower()

    @property
    def spotify_app(self):
        return bool(self.settings.get("spotify_app", False))

    async def on_appear(self):
        self.every(POLL_SECONDS, self.tick)

    async def session(self):
        try:
            if CrossPlay._manager is None:
                CrossPlay._manager = await asyncio.wait_for(_Mgr.request_async(), WINRT_TIMEOUT)
            return CrossPlay._manager.get_current_session()
        except Exception as e:
            log.debug("session failed: %s", e)
            CrossPlay._manager = None
            return None

    def target(self):
        if self.forced_target in ("youtube", "spotify"):
            return self.forced_target
        # playing from Spotify -> look on YouTube; anything else (browser) -> look on Spotify
        return "youtube" if "spotify" in self.source.lower() else "spotify"

    async def tick(self):
        s = await self.session() if HAVE_WINSDK else None
        if s is None:
            self.found = False
            await self.set_image(render(self.target(), "", False))
            return
        try:
            p = await asyncio.wait_for(s.try_get_media_properties_async(), WINRT_TIMEOUT)
            self.title, self.artist, self.source = p.title or "", p.artist or "", s.source_app_user_model_id or ""
        except Exception as e:
            log.debug("props failed: %s", e)
            return
        self.found = bool(self.title)
        a, t = parse_track(self.title, self.artist)
        await self.set_image(render(self.target(), f"{a} - {t}" if a else t, self.found))

    def search_url(self):
        a, t = parse_track(self.title, self.artist)
        q = f"{a} {t}".strip() or self.title
        if self.target() == "youtube":
            return f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"
        if self.spotify_app:
            return f"spotify:search:{urllib.parse.quote(q)}"
        return f"https://open.spotify.com/search/{urllib.parse.quote(q)}"

    async def on_key_down(self, payload):
        if not self.found:
            await self.deck.show_alert(self.context)
            return
        url = self.search_url()
        log.info("opening %s", url)
        if await asyncio.to_thread(open_first, [url]):
            await self.deck.show_ok(self.context)
        else:
            await self.deck.show_alert(self.context)

    async def on_dial_down(self, payload):
        await self.on_key_down(payload)

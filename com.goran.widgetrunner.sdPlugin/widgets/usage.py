"""Usage-limit widgets (Claude Code here, Codex in codex.py) sharing one renderer and one
polling/backoff base class.

Claude: reads the access token Claude Code keeps in ~/.claude/.credentials.json (re-read on
every poll so refreshes are picked up; never logged) and calls the OAuth usage endpoint.
"""
import asyncio
import json
import logging
import math
import os
import urllib.error
from datetime import datetime, timezone

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font, get_json, open_first

log = logging.getLogger("widgetrunner.usage")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CREDS = os.path.expanduser("~/.claude/.credentials.json")
CLAUDE_DESKTOP = [r"shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude", "claude:"]  # Store app, then its URI scheme

POLL_SECONDS = 300        # normal refresh; the endpoints rate-limit aggressively
BACKOFF_MAX = 1800        # cap after repeated 429s
MANUAL_GAP = 60           # key press may force a refresh at most this often
CHECK_EVERY = 15          # how often the loop checks whether a fetch is due

BG = (18, 20, 28)
TRACK = (44, 48, 62)
LABEL = (200, 205, 215)
DIM = (130, 136, 150)
OK = (70, 190, 120)
WARN = (240, 190, 60)
BAD = (230, 80, 80)
CLAUDE = (217, 119, 87)
OPENAI = (235, 238, 245)


# ---------------------------------------------------------------- helpers
def humanize_until(when):
    """when: ISO string, epoch seconds, or aware datetime -> '45m' / '3h' / '1d 5h'."""
    if not when:
        return ""
    try:
        if isinstance(when, datetime):
            t = when
        elif isinstance(when, (int, float)):
            t = datetime.fromtimestamp(when, tz=timezone.utc)
        else:
            t = datetime.fromisoformat(when)
    except (ValueError, OSError, OverflowError):
        return ""
    secs = (t - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    m = int(secs // 60)
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 24:
        return f"{h}h"
    return f"{h // 24}d {h % 24}h"


def color_for(pct):
    return BAD if pct >= 80 else WARN if pct >= 50 else OK


def brand_icon(d, brand, cx, cy, r):
    """Small brand mark: Claude = orange starburst, Codex/OpenAI = hexagonal knot."""
    if brand.lower() == "claude":
        for i in range(8):
            a = math.radians(i * 45 + 12)
            ln = r if i % 2 == 0 else r * 0.8
            x, y = cx + math.cos(a) * ln, cy + math.sin(a) * ln
            d.line((cx, cy, x, y), fill=CLAUDE, width=3)
            d.ellipse((x - 1.5, y - 1.5, x + 1.5, y + 1.5), fill=CLAUDE)
        d.ellipse((cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5), fill=CLAUDE)
    else:
        pts = [(cx + math.cos(math.radians(60 * i - 90)) * r, cy + math.sin(math.radians(60 * i - 90)) * r)
               for i in range(6)]
        for i in range(6):
            (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % 6]
            ex, ey = x1 + (x1 - x0) * 0.45, y1 + (y1 - y0) * 0.45  # extend past the next vertex
            d.line((x0, y0, ex, ey), fill=OPENAI, width=2)


def render(rows, brand="Claude", show_name=False, error=None, stale=False) -> Image.Image:
    """rows: list of (label, percent, reset_text). Shared by the Claude and Codex widgets."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    if show_name:
        d.text((SIZE // 2, 4), brand, fill=DIM, font=font(14, "semibold"), anchor="ma")
        y, step = 26, 58
    else:
        y, step = 16, 62
    if stale:
        d.ellipse((SIZE - 12, 6, SIZE - 6, 12), fill=DIM)

    if error:
        brand_icon(d, brand, SIZE // 2, SIZE // 2 - 22, 12)
        d.text((SIZE // 2, SIZE // 2 + 14), error, fill=BAD, font=font(20, "bold"), anchor="mm")
        return img

    for i, (label, pct, reset) in enumerate(rows):
        pct = max(0, min(100, int(round(pct or 0))))
        d.text((14, y), label, fill=LABEL, font=font(21, "bold"), anchor="la")
        d.text((SIZE - 14, y), f"{pct}%", fill=color_for(pct), font=font(21, "bold"), anchor="ra")
        if i == 0:
            brand_icon(d, brand, SIZE // 2 - 2, y + 13, 9)
        by = y + 27
        d.rounded_rectangle((14, by, SIZE - 14, by + 9), radius=4, fill=TRACK)
        if pct > 0:
            d.rounded_rectangle((14, by, 14 + (SIZE - 28) * pct // 100, by + 9), radius=4, fill=color_for(pct))
        d.text((14, by + 12), f"resets {reset}", fill=DIM, font=font(14), anchor="la")
        y += step
    return img


# ---------------------------------------------------------------- base widget
class UsageBase(Widget):
    """Polls `fetch_rows()` with backoff. The result is cached per subclass (per provider), shared by
    every key instance on every profile, so switching profiles or having the key several times never
    triggers extra requests - only the interval does."""

    brand = "Claude"
    _shared = {}   # cls -> {"rows", "stale", "next", "backoff", "lock", "fetched"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._last_manual = -1e9
        UsageBase._live.add(self)

    async def on_disappear(self):
        await super().on_disappear()
        UsageBase._live.discard(self)

    @classmethod
    def shared(cls):
        return cls._shared.setdefault(cls, {"rows": None, "stale": False, "next": 0.0, "backoff": POLL_SECONDS,
                                            "lock": asyncio.Lock(), "fetched": 0.0})

    @property
    def show_name(self):
        return bool(self.settings.get("show_name", False))

    def fetch_rows(self):  # blocking, runs in a thread; returns [(label, pct, reset_text), ...]
        raise NotImplementedError

    async def on_appear(self):
        await self.draw()               # show the cached data immediately, no request
        self.every(CHECK_EVERY, self._maybe_fetch)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        await self.draw()

    async def on_key_down(self, payload):
        now = asyncio.get_running_loop().time()
        if now - self._last_manual >= MANUAL_GAP:
            self._last_manual = now
            self.shared()["next"] = 0
            await self._maybe_fetch()

    async def _maybe_fetch(self):
        st = self.shared()
        if asyncio.get_running_loop().time() < st["next"]:
            return
        async with st["lock"]:                       # one request even if several keys tick at once
            if asyncio.get_running_loop().time() < st["next"]:
                return
            await self.refresh()

    async def draw(self, error=None):
        st = self.shared()
        if st["rows"]:
            await self.set_image(render(st["rows"], self.brand, self.show_name, stale=st["stale"]))
        else:
            await self.set_image(render([], self.brand, self.show_name, error=error or "…"))

    async def _draw_all(self, error=None):
        for inst in list(self._instances_of(type(self))):
            await inst.draw(error)

    @classmethod
    def _instances_of(cls, klass):
        return [i for i in cls._live if type(i) is klass]

    _live = set()

    async def refresh(self):
        st = self.shared()
        loop = asyncio.get_running_loop()
        try:
            rows = await asyncio.to_thread(self.fetch_rows)
        except (FileNotFoundError, KeyError):
            st["next"] = loop.time() + POLL_SECONDS
            await self._draw_all("no login")
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                st["backoff"] = min(st["backoff"] * 2, BACKOFF_MAX)
                st["next"] = loop.time() + st["backoff"]
                st["stale"] = True
                log.warning("%s usage HTTP 429, backing off %ss", self.brand, st["backoff"])
                await self._draw_all("rate limit")
            else:
                st["next"] = loop.time() + POLL_SECONDS
                st["stale"] = True
                log.warning("%s usage HTTP %s", self.brand, e.code)
                await self._draw_all("auth?" if e.code in (401, 403) else f"http {e.code}")
            return
        except Exception as e:
            st["next"] = loop.time() + POLL_SECONDS
            st["stale"] = True
            log.warning("%s usage fetch failed: %s", self.brand, e)
            await self._draw_all("offline")
            return

        st.update(rows=rows, stale=False, backoff=POLL_SECONDS, next=loop.time() + POLL_SECONDS, fetched=loop.time())
        await self._draw_all()


# ---------------------------------------------------------------- Claude
def fetch_claude_rows():
    with open(CREDS, encoding="utf-8") as f:
        tok = json.load(f)["claudeAiOauth"]["accessToken"]
    data = get_json(USAGE_URL, headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"})
    five, week = data.get("five_hour") or {}, data.get("seven_day") or {}
    return [
        ("5H", five.get("utilization"), humanize_until(five.get("resets_at"))),
        ("7D", week.get("utilization"), humanize_until(week.get("resets_at"))),
    ]


class Usage(UsageBase):
    action = "com.goran.widgetrunner.usage"
    brand = "Claude"

    def fetch_rows(self):
        return fetch_claude_rows()

    async def on_key_down(self, payload):
        """Open Claude Desktop, then do the usual throttled refresh."""
        if await asyncio.to_thread(open_first, CLAUDE_DESKTOP):
            await self.deck.show_ok(self.context)
        else:
            await self.deck.show_alert(self.context)
        await super().on_key_down(payload)

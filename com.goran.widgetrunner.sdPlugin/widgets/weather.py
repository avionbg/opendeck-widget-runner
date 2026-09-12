"""Current weather via Open-Meteo (no API key). City is configurable in the property inspector."""
import asyncio
import logging
import math
import urllib.parse

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, fit_text, font, get_json

log = logging.getLogger("widgetrunner.weather")

DEFAULT_CITY = "Belgrade"
REFRESH_SECONDS = 600
RETRY_SECONDS = 60

BG = (18, 20, 28)
TEXT = (235, 238, 245)
DIM = (170, 176, 190)
SUN = (250, 200, 60)
MOON = (225, 228, 240)
CLOUD = (205, 210, 222)
DARK_CLOUD = (140, 146, 160)
RAIN = (90, 160, 240)
SNOW = (240, 245, 255)
BOLT = (255, 215, 70)
FOG = (160, 166, 180)

# WMO weather code -> (description, glyph kind)
CODES = {
    0: ("Clear", "sun"), 1: ("Mainly clear", "sun"), 2: ("Partly cloudy", "partly"), 3: ("Overcast", "cloud"),
    45: ("Fog", "fog"), 48: ("Rime fog", "fog"),
    51: ("Light drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy drizzle", "rain"),
    56: ("Freezing drizzle", "rain"), 57: ("Freezing drizzle", "rain"),
    61: ("Light rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"), 67: ("Freezing rain", "rain"),
    71: ("Light snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy snow", "snow"), 77: ("Snow grains", "snow"),
    80: ("Light showers", "rain"), 81: ("Showers", "rain"), 82: ("Heavy showers", "rain"),
    85: ("Snow showers", "snow"), 86: ("Snow showers", "snow"),
    95: ("Thunderstorm", "thunder"), 96: ("Thunder + hail", "thunder"), 99: ("Thunder + hail", "thunder"),
}


# ---------------------------------------------------------------- glyphs
def _sun(d, cx, cy, r, night):
    if night:
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=MOON)
        d.ellipse((cx - r + 10, cy - r - 6, cx + r + 10, cy + r - 6), fill=BG)
        return
    for i in range(8):
        a = math.radians(i * 45)
        d.line((cx + math.cos(a) * (r + 5), cy + math.sin(a) * (r + 5),
                cx + math.cos(a) * (r + 12), cy + math.sin(a) * (r + 12)), fill=SUN, width=3)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SUN)


def _cloud(d, cx, cy, w, color):
    h = w * 0.55
    d.ellipse((cx - w * 0.28, cy - h * 0.55, cx + w * 0.18, cy + h * 0.3), fill=color)
    d.ellipse((cx - w * 0.5, cy - h * 0.2, cx - w * 0.05, cy + h * 0.35), fill=color)
    d.ellipse((cx + w * 0.05, cy - h * 0.3, cx + w * 0.5, cy + h * 0.35), fill=color)
    d.rounded_rectangle((cx - w * 0.45, cy - h * 0.05, cx + w * 0.45, cy + h * 0.35), radius=6, fill=color)


def glyph(d, kind, cx, cy, night=False):
    if kind == "sun":
        _sun(d, cx, cy, 16, night)
    elif kind == "partly":
        _sun(d, cx - 10, cy - 10, 12, night)
        _cloud(d, cx + 6, cy + 6, 52, CLOUD)
    elif kind == "cloud":
        _cloud(d, cx, cy, 60, CLOUD)
    elif kind == "fog":
        _cloud(d, cx, cy - 8, 54, DARK_CLOUD)
        for i in range(3):
            d.line((cx - 24 + i * 4, cy + 12 + i * 7, cx + 24 - i * 4, cy + 12 + i * 7), fill=FOG, width=3)
    elif kind in ("rain", "snow"):
        _cloud(d, cx, cy - 8, 56, DARK_CLOUD if kind == "rain" else CLOUD)
        for i in range(3):
            x = cx - 14 + i * 14
            if kind == "rain":
                d.line((x, cy + 12, x - 4, cy + 24), fill=RAIN, width=3)
            else:
                d.ellipse((x - 3, cy + 14, x + 3, cy + 20), fill=SNOW)
    elif kind == "thunder":
        _cloud(d, cx, cy - 10, 56, DARK_CLOUD)
        d.polygon([(cx + 4, cy), (cx - 8, cy + 16), (cx, cy + 16), (cx - 6, cy + 30), (cx + 10, cy + 10), (cx + 2, cy + 10)],
                  fill=BOLT)


def render(desc, temp, city, kind, night=False, error=None) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    if error:
        glyph(d, "cloud", cx, 52)
        d.text((cx, 100), error, fill=DIM, font=font(18), anchor="mm")
        d.text((cx, 126), city, fill=DIM, font=fit_text(d, city, SIZE - 16, 16), anchor="mm")
        return img
    d.text((cx, 12), desc, fill=DIM, font=fit_text(d, desc, SIZE - 12, 15, min_size=11), anchor="mm")
    glyph(d, kind, cx, 56, night)
    d.text((cx, 100), f"{temp:.1f}°C", fill=TEXT, font=font(26, "bold"), anchor="mm")
    d.text((cx, 127), city, fill=DIM, font=fit_text(d, city, SIZE - 16, 17), anchor="mm")
    return img


# ---------------------------------------------------------------- data
def _search(name, count=10):
    q = urllib.parse.quote(name)
    data = get_json(f"https://geocoding-api.open-meteo.com/v1/search?name={q}&count={count}&language=en&format=json")
    return data.get("results") or []


def _matches(hit, hint):
    hint = hint.lower()
    fields = [hit.get("country", ""), hit.get("country_code", ""), hit.get("admin1", ""), hit.get("admin2", "")]
    return any(f and (f.lower() == hint or f.lower().startswith(hint)) for f in fields)


def geocode(query):
    """Accepts "Belgrade", "Belgrade, Serbia", "Belgrade Serbia", "Belgrade, RS", "Belgrade, Montana".
    The part after the comma (or the last word) narrows results by country / region."""
    query = query.strip()
    parts = [x.strip() for x in query.split(",") if x.strip()]
    candidates = []  # (name, hint)
    if len(parts) >= 2:
        candidates.append((parts[0], parts[1]))
    else:
        candidates.append((query, None))
        words = query.split()
        if len(words) >= 2:
            candidates.append((" ".join(words[:-1]), words[-1]))   # "Belgrade Serbia", "Novi Sad Serbia"
    for name, hint in candidates:
        hits = _search(name)
        if not hits:
            continue
        if hint:
            filtered = [h for h in hits if _matches(h, hint)]
            if filtered:
                hits = filtered
            elif len(parts) >= 2:
                continue  # explicit "city, country" with no match: try next / fail
        hit = hits[0]
        return hit["latitude"], hit["longitude"], hit["name"]
    raise LookupError(f"city not found: {query}")


def current_weather(lat, lon):
    data = get_json(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                    "&current=temperature_2m,weather_code,is_day&timezone=auto")
    cur = data["current"]
    return cur["temperature_2m"], int(cur["weather_code"]), not bool(cur.get("is_day", 1))


class Weather(Widget):
    action = "com.goran.widgetrunner.weather"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._loc = None       # (lat, lon, name) for self.city
        self._loc_city = None
        self._task = None

    @property
    def city(self):
        return (self.settings.get("city") or DEFAULT_CITY).strip() or DEFAULT_CITY

    async def on_appear(self):
        self._task = self.every(RETRY_SECONDS, self.tick)

    async def on_settings(self, settings):
        await super().on_settings(settings)
        self._next_fetch = 0
        await self.tick(force=True)

    async def on_key_down(self, payload):
        await self.tick(force=True)

    _next_fetch = 0.0

    async def tick(self, force=False):
        loop = asyncio.get_running_loop()
        if not force and loop.time() < self._next_fetch:
            return
        city = self.city
        try:
            if self._loc is None or self._loc_city != city:
                self._loc = await asyncio.to_thread(geocode, city)
                self._loc_city = city
            lat, lon, name = self._loc
            temp, code, night = await asyncio.to_thread(current_weather, lat, lon)
        except LookupError:
            self._loc = None
            await self.set_image(render("", 0, city, "cloud", error="not found"))
            self._next_fetch = loop.time() + REFRESH_SECONDS
            return
        except Exception as e:
            log.warning("weather fetch failed for %s: %s", city, e)
            await self.set_image(render("", 0, city, "cloud", error="offline"))
            self._next_fetch = loop.time() + RETRY_SECONDS
            return

        desc, kind = CODES.get(code, ("Unknown", "cloud"))
        await self.set_image(render(desc, temp, name, kind, night))
        self._next_fetch = loop.time() + REFRESH_SECONDS

"""Shared drawing helpers for widgets (fonts, text fitting, http)."""
import json
import logging
import os
import ssl
import urllib.request

from PIL import ImageFont

# Python 3.11's bundled OpenSSL 1.1.1 fails to build some Let's Encrypt chains from the Windows
# cert store ("certificate has expired"), so prefer certifi's bundle when it is installed.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    SSL_CONTEXT = ssl.create_default_context()

log = logging.getLogger("widgetrunner")

SIZE = 144
_fonts = {}

FONT_FILES = {
    "regular": "C:/Windows/Fonts/segoeui.ttf",
    "bold": "C:/Windows/Fonts/segoeuib.ttf",
    "semibold": "C:/Windows/Fonts/seguisb.ttf",
}


def font(size, weight="regular"):
    key = (size, weight)
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(FONT_FILES.get(weight, FONT_FILES["regular"]), size)
        except OSError:
            _fonts[key] = ImageFont.load_default(size=size)
    return _fonts[key]


def fit_text(draw, text, max_width, size, weight="regular", min_size=10):
    """Return a font small enough that `text` fits in max_width (shrinks down to min_size)."""
    while size > min_size:
        f = font(size, weight)
        if draw.textlength(text, font=f) <= max_width:
            return f
        size -= 1
    return font(min_size, weight)


def get_json(url, headers=None, timeout=15):
    """Blocking HTTP GET -> parsed JSON. Call via asyncio.to_thread."""
    req = urllib.request.Request(url, headers={"User-Agent": "opendeck-widget-runner/0.1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as r:
        return json.loads(r.read().decode("utf-8"))


def open_first(targets):
    """Open the first target Windows can launch: exe / .lnk / URL / URI scheme / shell:AppsFolder entry.
    Blocking (ShellExecute); call via asyncio.to_thread. Returns the target that worked, or None."""
    for t in targets:
        if not t:
            continue
        try:
            os.startfile(os.path.expandvars(t))
            return t
        except OSError as e:
            log.warning("could not open %r: %s", t, e)
    return None

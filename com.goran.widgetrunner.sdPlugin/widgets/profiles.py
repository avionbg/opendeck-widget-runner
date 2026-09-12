"""NOT REGISTERED IN THE MANIFEST: OpenDeck silently drops `switchProfile` from any plugin other than
its starter pack (allowlist in src-tauri/src/events/inbound/mod.rs). Kept for when that changes.

Profile cycler: press switches the device to the next OpenDeck profile (alphabetical, wrapping).
Dial: rotate goes forward/backward through profiles. Put this key on every profile you cycle through,
otherwise the chain stops at a profile without it."""
import logging
import os
import re

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, fit_text, font

log = logging.getLogger("widgetrunner.profiles")

PROFILES_DIR = os.path.expandvars(r"%APPDATA%\opendeck\profiles")

BG = (18, 20, 28)
ACCENT = (150, 110, 240)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)


def list_profiles(device):
    """Profile names for a device, alphabetical (case-insensitive); nested folders as 'folder/name'."""
    base = os.path.join(PROFILES_DIR, device)
    names = []
    for root, _dirs, files in os.walk(base):
        rel = os.path.relpath(root, base).replace("\\", "/")
        for f in files:
            if f.lower().endswith(".json"):
                name = f[:-5]
                names.append(name if rel == "." else f"{rel}/{name}")
    return sorted(names, key=str.lower)


def profile_from_context(context, device):
    """'n3-XXXX.opendeck.Keypad.5.0' -> 'opendeck' (the part between device id and controller)."""
    m = re.match(re.escape(device) + r"\.(.+)\.(Keypad|Encoder)\.\d+\.\d+$", context or "")
    return m.group(1) if m else None


def render(current, index, total) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    # stacked cards glyph
    for i, off in enumerate((16, 8, 0)):
        d.rounded_rectangle((cx - 34 + off, 18 + off // 2, cx + 34 + off, 50 + off // 2), radius=6,
                            fill=ACCENT if i == 2 else (60, 50, 96))
    d.polygon([(cx + 40, 34), (cx + 52, 26), (cx + 52, 42)], fill=TEXT)
    name = current or "?"
    d.text((cx, 90), name, fill=TEXT, font=fit_text(d, name, SIZE - 12, 22, "bold", min_size=12), anchor="mm")
    d.text((cx, 122), f"{index}/{total}" if total else "no profiles", fill=DIM, font=font(15), anchor="mm")
    return img


class Profiles(Widget):
    action = "com.goran.widgetrunner.profiles"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.device = None  # filled in by the runner from the willAppear event

    async def on_appear(self):
        await self.draw()

    def state(self):
        names = list_profiles(self.device) if self.device else []
        current = profile_from_context(self.context, self.device) if self.device else None
        idx = names.index(current) if current in names else -1
        return names, current, idx

    async def draw(self):
        names, current, idx = self.state()
        await self.set_image(render(current, idx + 1 if idx >= 0 else 0, len(names)))

    async def step(self, delta):
        names, current, idx = self.state()
        if not names or not self.device:
            await self.deck.show_alert(self.context)
            return
        target = names[(idx + delta) % len(names)] if idx >= 0 else names[0]
        log.info("switching %s: %s -> %s", self.device, current, target)
        await self.deck.send("switchProfile", device=self.device, profile=target)

    async def on_key_down(self, payload):
        await self.step(+1)

    async def on_dial_rotate(self, payload):
        ticks = int(payload.get("ticks", 0) or 0)
        if ticks:
            await self.step(1 if ticks > 0 else -1)

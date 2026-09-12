"""Docker widget: number of running containers (and stopped ones, small). Press opens Docker Desktop."""
import asyncio
import logging
import shutil
import subprocess

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font, open_first

log = logging.getLogger("widgetrunner.docker")

REFRESH_SECONDS = 10
CREATE_NO_WINDOW = 0x08000000
DOCKER = shutil.which("docker") or r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"
OPEN_ON_PRESS = [r"C:\Program Files\Docker\Docker\Docker Desktop.exe"]

BG = (18, 20, 28)
BLUE = (36, 150, 237)
OFF = (110, 116, 130)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)


def container_states():
    """-> list of container states ('running', 'exited', ...), or None when the daemon is unreachable."""
    try:
        out = subprocess.run([DOCKER, "ps", "-a", "--format", "{{.State}}"], capture_output=True, text=True,
                             timeout=6, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("docker ps failed: %s", e)
        return None
    if out.returncode != 0:
        return None
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def whale(d, cx, cy, color):
    """Tiny container-stack + hull glyph, docker-ish."""
    for (x, y) in [(-16, -18), (-4, -18), (8, -18), (-16, -6), (-4, -6), (8, -6), (-4, -30)]:
        d.rounded_rectangle((cx + x, cy + y, cx + x + 10, cy + y + 10), radius=2, fill=color)
    d.rounded_rectangle((cx - 26, cy + 6, cx + 30, cy + 20), radius=7, fill=color)
    d.ellipse((cx + 22, cy - 2, cx + 40, cy + 12), fill=color)


def render(running, stopped, online=True) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    color = BLUE if online else OFF
    whale(d, cx - 4, 44, color)
    if not online:
        d.text((cx, 96), "off", fill=OFF, font=font(30, "bold"), anchor="mm")
        d.text((cx, 126), "Docker", fill=DIM, font=font(14), anchor="mm")
        return img
    d.text((cx, 96), str(running), fill=TEXT, font=font(40, "bold"), anchor="mm")
    sub = "running" if stopped == 0 else f"running · {stopped} stopped"
    d.text((cx, 128), sub, fill=DIM, font=font(13), anchor="mm")
    return img


class Docker(Widget):
    action = "com.goran.widgetrunner.docker"

    async def on_appear(self):
        self.every(REFRESH_SECONDS, self.tick)

    async def tick(self):
        states = await asyncio.to_thread(container_states)
        if states is None:
            await self.set_image(render(0, 0, online=False))
            return
        running = sum(1 for s in states if s == "running")
        await self.set_image(render(running, len(states) - running))

    async def on_key_down(self, payload):
        if await asyncio.to_thread(open_first, OPEN_ON_PRESS):
            await self.deck.show_ok(self.context)
        else:
            await self.deck.show_alert(self.context)
        await self.tick()

"""System monitor: CPU, RAM and (NVIDIA) GPU load as three bars. Press opens Task Manager.

CPU and RAM come straight from the Windows API via ctypes (no psutil needed); GPU load and
temperature come from nvidia-smi when it is available.
"""
import asyncio
import ctypes
import ctypes.wintypes as wt
import logging
import shutil
import subprocess

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font, open_first

log = logging.getLogger("widgetrunner.monitor")

REFRESH_SECONDS = 2
OPEN_ON_PRESS = ["taskmgr.exe"]
CREATE_NO_WINDOW = 0x08000000
# Task Manager opens on the page chosen in its own Settings > "Default Start Page"; there is no
# command-line switch and its settings.json does not persist the last view, so pick Performance there.

BG = (18, 20, 28)
TRACK = (44, 48, 62)
LABEL = (200, 205, 215)
DIM = (130, 136, 150)
OK = (80, 150, 240)
WARN = (240, 190, 60)
BAD = (230, 80, 80)


# ---------------------------------------------------------------- sampling
class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def ram_percent():
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(st)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return int(st.dwMemoryLoad)


def _filetime(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


class CpuSampler:
    """CPU load between two calls, from GetSystemTimes (idle/kernel/user)."""

    def __init__(self):
        self._last = None

    def percent(self):
        idle, kernel, user = wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        now = (_filetime(idle), _filetime(kernel) + _filetime(user))
        prev, self._last = self._last, now
        if prev is None:
            return None
        d_idle, d_total = now[0] - prev[0], now[1] - prev[1]
        if d_total <= 0:
            return None
        return int(round(100 * (1 - d_idle / d_total)))


NVSMI = shutil.which("nvidia-smi")


def gpu_stats():
    """-> (load_percent, temp_c) or None when nvidia-smi is unavailable/fails."""
    if not NVSMI:
        return None
    try:
        out = subprocess.run([NVSMI, "--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=4, creationflags=CREATE_NO_WINDOW)
        load, temp = (x.strip() for x in out.stdout.strip().splitlines()[0].split(","))
        return int(load), int(temp)
    except Exception as e:
        log.debug("nvidia-smi failed: %s", e)
        return None


# ---------------------------------------------------------------- render
def color_for(pct):
    return BAD if pct >= 90 else WARN if pct >= 70 else OK


def render(rows) -> Image.Image:
    """rows: list of (label, percent or None, extra_text)."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    n = max(1, len(rows))
    step = (SIZE - 12) // n
    y = 8 + (step - 40) // 2
    for label, pct, extra in rows:
        d.text((14, y), label, fill=LABEL, font=font(19, "bold"), anchor="la")
        if extra:
            d.text((14 + d.textlength(label, font=font(19, "bold")) + 6, y + 4), extra, fill=DIM, font=font(13), anchor="la")
        if pct is None:
            d.text((SIZE - 14, y), "n/a", fill=DIM, font=font(19, "bold"), anchor="ra")
            fill = 0
        else:
            pct = max(0, min(100, int(pct)))
            d.text((SIZE - 14, y), f"{pct}%", fill=color_for(pct), font=font(19, "bold"), anchor="ra")
            fill = (SIZE - 28) * pct // 100
        by = y + 25
        d.rounded_rectangle((14, by, SIZE - 14, by + 8), radius=4, fill=TRACK)
        if fill > 0:
            d.rounded_rectangle((14, by, 14 + fill, by + 8), radius=4, fill=color_for(pct))
        y += step
    return img


# ---------------------------------------------------------------- widget
class Monitor(Widget):
    action = "com.goran.widgetrunner.monitor"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._cpu = CpuSampler()
        self._has_gpu = NVSMI is not None

    async def on_appear(self):
        self._cpu.percent()  # prime the CPU delta
        self.every(REFRESH_SECONDS, self.tick)

    def sample(self):
        rows = [("CPU", self._cpu.percent(), ""), ("RAM", ram_percent(), "")]
        if self._has_gpu:
            g = gpu_stats()
            rows.append(("GPU", g[0] if g else None, f"{g[1]}°C" if g else ""))
        return rows

    async def tick(self):
        rows = await asyncio.to_thread(self.sample)
        await self.set_image(render(rows))

    async def on_key_down(self, payload):
        if await asyncio.to_thread(open_first, OPEN_ON_PRESS):
            await self.deck.show_ok(self.context)
        else:
            await self.deck.show_alert(self.context)

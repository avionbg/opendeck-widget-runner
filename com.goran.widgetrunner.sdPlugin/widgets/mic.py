"""Microphone mute widget: shows whether the default capture device is muted, press toggles it.

Talks to Windows Core Audio (IMMDeviceEnumerator -> IAudioEndpointVolume) directly through ctypes,
so no comtypes/pycaw is needed. Every call runs in a worker thread with its own COM init.
"""
import asyncio
import ctypes
import logging
from ctypes import wintypes as wt

from PIL import Image, ImageDraw

from widgetlib import Widget
from widgets._draw import SIZE, font

log = logging.getLogger("widgetrunner.mic")

REFRESH_SECONDS = 2

BG = (18, 20, 28)
LIVE = (70, 190, 120)
MUTED = (230, 80, 80)
TEXT = (235, 238, 245)
DIM = (130, 136, 150)


# ---------------------------------------------------------------- COM plumbing
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_str(cls, s):
        g = cls()
        ctypes.oledll.ole32.CLSIDFromString(s, ctypes.byref(g))
        return g


CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAudioEndpointVolume = "{5CDF2C82-841E-4546-9722-0CF74078229A}"
CLSCTX_ALL = 23
eRender = 0
eCapture = 1
eConsole = 0


def _method(obj, index, restype, *argtypes):
    """Fetch COM vtable method `index` of interface pointer `obj` as a callable."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(obj):
    if obj:
        _method(obj, 2, ctypes.c_ulong)(obj)


def _with_endpoint_volume(fn):
    """Run fn(endpoint_volume_ptr) inside a COM session on the default capture device."""
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    enum = ctypes.c_void_p()
    dev = ctypes.c_void_p()
    vol = ctypes.c_void_p()
    try:
        ole32.CoCreateInstance(ctypes.byref(GUID.from_str(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                               ctypes.byref(GUID.from_str(IID_IMMDeviceEnumerator)), ctypes.byref(enum))
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint (vtable 4)
        hr = _method(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            enum, eCapture, eConsole, ctypes.byref(dev))
        if hr != 0:
            raise OSError(f"GetDefaultAudioEndpoint failed: 0x{hr & 0xFFFFFFFF:08X}")
        # IMMDevice::Activate (vtable 3)
        hr = _method(dev, 3, ctypes.HRESULT, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p,
                     ctypes.POINTER(ctypes.c_void_p))(dev, ctypes.byref(GUID.from_str(IID_IAudioEndpointVolume)),
                                                      CLSCTX_ALL, None, ctypes.byref(vol))
        if hr != 0:
            raise OSError(f"Activate(IAudioEndpointVolume) failed: 0x{hr & 0xFFFFFFFF:08X}")
        return fn(vol)
    finally:
        _release(vol)
        _release(dev)
        _release(enum)
        ole32.CoUninitialize()


def get_mute():
    def fn(vol):
        muted = wt.BOOL()
        _method(vol, 15, ctypes.HRESULT, ctypes.POINTER(wt.BOOL))(vol, ctypes.byref(muted))  # GetMute
        return bool(muted.value)
    return _with_endpoint_volume(fn)


def set_mute(muted: bool):
    def fn(vol):
        _method(vol, 14, ctypes.HRESULT, wt.BOOL, ctypes.c_void_p)(vol, wt.BOOL(muted), None)  # SetMute
        return muted
    return _with_endpoint_volume(fn)


def toggle_mute():
    return set_mute(not get_mute())


# ---------------------------------------------------------------- render
def render(muted, error=False) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    cx = SIZE // 2
    color = DIM if error else (MUTED if muted else LIVE)
    # capsule + stand
    d.rounded_rectangle((cx - 14, 22, cx + 14, 72), radius=14, fill=color)
    d.arc((cx - 26, 40, cx + 26, 92), 0, 180, fill=color, width=5)
    d.line((cx, 92, cx, 104), fill=color, width=5)
    d.line((cx - 16, 106, cx + 16, 106), fill=color, width=5)
    if muted and not error:
        d.line((cx - 34, 96, cx + 34, 18), fill=BG, width=12)
        d.line((cx - 34, 96, cx + 34, 18), fill=color, width=5)
    label = "no mic" if error else ("MUTED" if muted else "MIC ON")
    d.text((cx, 128), label, fill=color, font=font(16, "bold"), anchor="mm")
    return img


class Mic(Widget):
    action = "com.goran.widgetrunner.mic"

    async def on_appear(self):
        self.every(REFRESH_SECONDS, self.tick)

    async def tick(self):
        try:
            muted = await asyncio.to_thread(get_mute)
        except Exception as e:
            log.warning("mic state failed: %s", e)
            await self.set_image(render(False, error=True))
            return
        await self.set_image(render(muted))

    async def on_key_down(self, payload):
        try:
            muted = await asyncio.to_thread(toggle_mute)
        except Exception as e:
            log.warning("mic toggle failed: %s", e)
            await self.deck.show_alert(self.context)
            return
        await self.set_image(render(muted))

"""Shared audio plumbing:
- Loopback: WASAPI loopback capture of the default output into a mono ring buffer (one background
  thread, reference-counted so several widgets can share it).
- per-application audio sessions on the default output: list / volume / mute / peak (Core Audio via
  ctypes, no extra packages).
"""
import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes as wt

import numpy as np

from widgets.mic import GUID, _method, _release, CLSCTX_ALL, CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator, eConsole, eRender

log = logging.getLogger("widgetrunner.audio")

IID_IAudioClient = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAudioCaptureClient = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"
IID_IAudioSessionManager2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAudioSessionControl2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"
IID_IAudioMeterInformation = "{C02216F6-8C67-4B5B-9D00-D008E73E0064}"
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
RING = 8192


class _WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD), ("nAvgBytesPerSec", wt.DWORD),
                ("nBlockAlign", wt.WORD), ("wBitsPerSample", wt.WORD), ("cbSize", wt.WORD)]


# ---------------------------------------------------------------- loopback capture
class Loopback:
    """Background WASAPI loopback capture into a mono float ring buffer. acquire()/release() by owner."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ring = np.zeros(RING, dtype=np.float32)
        self.rate = 44100
        self.last_data = 0.0      # monotonic time of the last non-silent packet
        self._thread = None
        self._stop = threading.Event()
        self._users = set()

    def acquire(self, owner):
        self._users.add(owner)
        self.start()

    def release(self, owner):
        self._users.discard(owner)
        if not self._users:
            self.stop()

    def start(self):
        self._stop.clear()   # cancel a pending stop first (rapid disappear/appear must not kill capture)
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="loopback", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def active(self, within=0.5):
        return (time.monotonic() - self.last_data) < within

    def latest(self, n):
        with self.lock:
            return self.ring[-n:].copy()

    def _push(self, mono):
        n = len(mono)
        if n == 0:
            return
        with self.lock:
            if n >= RING:
                self.ring[:] = mono[-RING:]
            else:
                self.ring[:-n] = self.ring[n:]
                self.ring[-n:] = mono

    def _run(self):
        while not self._stop.is_set():
            try:
                self._capture()
            except Exception as e:
                log.warning("loopback capture stopped: %s; retrying", e)
                time.sleep(2)

    def _capture(self):
        ole32 = ctypes.oledll.ole32
        ole32.CoInitialize(None)
        enum, dev, client, cap = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        pfmt = ctypes.POINTER(_WAVEFORMATEX)()
        try:
            ole32.CoCreateInstance(ctypes.byref(GUID.from_str(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                                   ctypes.byref(GUID.from_str(IID_IMMDeviceEnumerator)), ctypes.byref(enum))
            _method(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
                enum, eRender, eConsole, ctypes.byref(dev))
            _method(dev, 3, ctypes.HRESULT, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                dev, ctypes.byref(GUID.from_str(IID_IAudioClient)), CLSCTX_ALL, None, ctypes.byref(client))
            _method(client, 8, ctypes.HRESULT, ctypes.POINTER(ctypes.POINTER(_WAVEFORMATEX)))(client, ctypes.byref(pfmt))
            fmt = pfmt.contents
            ch, bits, self.rate = fmt.nChannels, fmt.wBitsPerSample, fmt.nSamplesPerSec
            _method(client, 3, ctypes.HRESULT, wt.DWORD, wt.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p,
                    ctypes.c_void_p)(client, 0, AUDCLNT_STREAMFLAGS_LOOPBACK, 2_000_000, 0, ctypes.cast(pfmt, ctypes.c_void_p), None)
            _method(client, 14, ctypes.HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                client, ctypes.byref(GUID.from_str(IID_IAudioCaptureClient)), ctypes.byref(cap))
            _method(client, 10, ctypes.HRESULT)(client)  # Start
            log.info("loopback capture: %d Hz, %d ch, %d bit", self.rate, ch, bits)
            get_next = _method(cap, 5, ctypes.HRESULT, ctypes.POINTER(wt.UINT))
            get_buf = _method(cap, 3, ctypes.HRESULT, ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)), ctypes.POINTER(wt.UINT),
                              ctypes.POINTER(wt.DWORD), ctypes.c_void_p, ctypes.c_void_p)
            release = _method(cap, 4, ctypes.HRESULT, wt.UINT)
            dtype = np.float32 if bits == 32 else np.int16
            while not self._stop.is_set():
                n = wt.UINT()
                get_next(cap, ctypes.byref(n))
                if n.value == 0:
                    time.sleep(0.01)
                    continue
                data, frames, flags = ctypes.POINTER(ctypes.c_ubyte)(), wt.UINT(), wt.DWORD()
                get_buf(cap, ctypes.byref(data), ctypes.byref(frames), ctypes.byref(flags), None, None)
                try:
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT or not frames.value:
                        self._push(np.zeros(frames.value, dtype=np.float32))
                    else:
                        raw = ctypes.string_at(data, frames.value * ch * (bits // 8))
                        arr = np.frombuffer(raw, dtype=dtype).astype(np.float32)
                        if dtype is np.int16:
                            arr /= 32768.0
                        mono = arr.reshape(-1, ch).mean(axis=1)
                        if np.abs(mono).max() > 1e-4:
                            self.last_data = time.monotonic()
                        self._push(mono)
                finally:
                    release(cap, frames.value)
            _method(client, 11, ctypes.HRESULT)(client)  # Stop
        finally:
            if pfmt:
                ctypes.oledll.ole32.CoTaskMemFree(pfmt)
            _release(cap)
            _release(client)
            _release(dev)
            _release(enum)
            ole32.CoUninitialize()


loopback = Loopback()   # shared by every widget that needs the output signal


# ---------------------------------------------------------------- per-app sessions
def _process_name(pid):
    if not pid:
        return ""
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def _qi(obj, iid):
    p = ctypes.c_void_p()
    hr = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
        ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0][0])(obj, ctypes.byref(GUID.from_str(iid)), ctypes.byref(p))
    return p if hr == 0 else None


def _with_sessions(fn):
    """Run fn(list_of_dicts) inside a COM session on the default render device. Each dict has
    pid, name (exe), system (bool), ctrl/vol/meter pointers valid only inside fn."""
    ole32 = ctypes.oledll.ole32
    ole32.CoInitialize(None)
    enum, dev, mgr, se = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    handles = []
    try:
        ole32.CoCreateInstance(ctypes.byref(GUID.from_str(CLSID_MMDeviceEnumerator)), None, CLSCTX_ALL,
                               ctypes.byref(GUID.from_str(IID_IMMDeviceEnumerator)), ctypes.byref(enum))
        _method(enum, 4, ctypes.HRESULT, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(enum, eRender, eConsole, ctypes.byref(dev))
        _method(dev, 3, ctypes.HRESULT, ctypes.POINTER(GUID), wt.DWORD, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            dev, ctypes.byref(GUID.from_str(IID_IAudioSessionManager2)), CLSCTX_ALL, None, ctypes.byref(mgr))
        _method(mgr, 5, ctypes.HRESULT, ctypes.POINTER(ctypes.c_void_p))(mgr, ctypes.byref(se))
        n = ctypes.c_int()
        _method(se, 3, ctypes.HRESULT, ctypes.POINTER(ctypes.c_int))(se, ctypes.byref(n))
        sessions = []
        for i in range(n.value):
            sc = ctypes.c_void_p()
            _method(se, 4, ctypes.HRESULT, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(se, i, ctypes.byref(sc))
            handles.append(sc)
            sc2, vol, meter = _qi(sc, IID_IAudioSessionControl2), _qi(sc, IID_ISimpleAudioVolume), _qi(sc, IID_IAudioMeterInformation)
            handles += [h for h in (sc2, vol, meter) if h]
            if not sc2 or not vol:
                continue
            pid = wt.DWORD()
            _method(sc2, 14, ctypes.HRESULT, ctypes.POINTER(wt.DWORD))(sc2, ctypes.byref(pid))
            system = _method(sc2, 15, ctypes.HRESULT)(sc2) == 0
            sessions.append({"pid": pid.value, "name": "system sounds" if system else _process_name(pid.value),
                             "system": system, "vol": vol, "meter": meter})
        return fn(sessions)
    finally:
        for h in handles:
            _release(h)
        _release(se)
        _release(mgr)
        _release(dev)
        _release(enum)
        ole32.CoUninitialize()


def _read(sess):
    v, m, pk = ctypes.c_float(), wt.BOOL(), ctypes.c_float()
    _method(sess["vol"], 4, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(sess["vol"], ctypes.byref(v))
    _method(sess["vol"], 6, ctypes.HRESULT, ctypes.POINTER(wt.BOOL))(sess["vol"], ctypes.byref(m))
    if sess["meter"]:
        _method(sess["meter"], 3, ctypes.HRESULT, ctypes.POINTER(ctypes.c_float))(sess["meter"], ctypes.byref(pk))
    return {"pid": sess["pid"], "name": sess["name"], "system": sess["system"],
            "volume": int(round(v.value * 100)), "muted": bool(m.value), "peak": pk.value}


def app_sessions():
    """-> list of {pid, name, system, volume (0..100), muted, peak (0..1)} for the default output."""
    return _with_sessions(lambda ss: [_read(s) for s in ss])


def set_app_volume(names, percent):
    percent = max(0, min(100, int(percent)))
    def fn(ss):
        for s in ss:
            if s["name"] in names:
                _method(s["vol"], 3, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p)(s["vol"], ctypes.c_float(percent / 100), None)
        return percent
    return _with_sessions(fn)


def set_app_mute(names, muted):
    def fn(ss):
        for s in ss:
            if s["name"] in names:
                _method(s["vol"], 5, ctypes.HRESULT, wt.BOOL, ctypes.c_void_p)(s["vol"], wt.BOOL(muted), None)
        return muted
    return _with_sessions(fn)

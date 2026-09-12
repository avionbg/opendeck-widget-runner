"""Lyrics overlay: a per-pixel-alpha (layered), always-on-top, click-through window on the primary
monitor, rendered with Pillow and pushed with UpdateLayeredWindow. No tkinter, no extra packages.

Started by the Lyrics widget as a separate process (pythonw overlay.py) and driven over stdin with
one JSON object per line:
  {"lines": ["...", ...], "index": 12}   replace the lyrics and/or move the highlighted line
  {"index": 13}                          move the highlight (smooth scroll)
  {"show": true|false}                   show / hide
  {"config": {...}}                      see DEFAULTS; applied live
  {"card": {"title", "artist", "rows": [[label, value], ...], "image": base64 png|null}}  info card mode
  {"quit": true}
"""
import base64
import ctypes
import io
import json
import queue
import sys
import threading
import time
from ctypes import wintypes as wt

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DEFAULTS = {
    "width_pct": 100.0,      # % of primary monitor width
    "height_pct": 55.0,      # % of primary monitor height
    "position": "middle",    # top | middle | bottom
    "font_pct": 2.8,         # current line height, % of screen height
    "text_color": "#ffffff",
    "bg_color": "#0b0d14",
    "bg_opacity": 80,        # 0..100
    "radius_pct": 1.5,       # panel corner radius, % of screen height
    "align": "center",       # left | center | right (horizontal placement)
}
LINE_SPACING = 1.65
ANIM_SECONDS = 0.22
VISIBLE = 4                  # lines above / below the current one
FONT_REG = "C:/Windows/Fonts/segoeui.ttf"
FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"

user32, gdi32, kernel32 = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x8, 0x80, 0x08000000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA = 2, 0, 1
PM_REMOVE, WM_DESTROY, WM_QUIT = 1, 0x0002, 0x0012

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte), ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


# 64-bit handles must not go through ctypes' default c_int conversion: declare every signature we use.
LRESULT = ctypes.c_ssize_t
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                       ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


def hex_rgb(s, default):
    try:
        s = s.strip().lstrip("#")
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return default


class Overlay:
    def __init__(self):
        try:
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
        self.sw, self.sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)   # primary monitor, physical px
        self.cfg = dict(DEFAULTS)
        self.lines, self.index = [], -1
        self.card = None          # when set, the overlay shows an info card instead of lyrics
        self.menu = None          # when set, the overlay shows a selectable list
        self.visible = False
        self.anim_from, self.anim_t0 = 0.0, None
        self._wndproc = WNDPROC(self._proc)
        self._fonts = {}
        self._dirty = True
        self._create_window()
        self._apply_config()
        self.queue = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()

    # --- window ------------------------------------------------------------
    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW(0, self._wndproc, 0, 0, hinst, None, None, None, None, "WidgetRunnerLyricsOverlay")
        user32.RegisterClassW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "WidgetRunnerLyricsOverlay", "Lyrics", WS_POPUP, 0, 0, 10, 10, None, None, hinst, None)

    def _apply_config(self, cfg=None):
        if cfg:
            self.cfg.update({k: v for k, v in cfg.items() if v is not None and v != ""})
        c = self.cfg
        wp = max(20.0, min(100.0, float(c["width_pct"])))
        hp = max(10.0, min(100.0, float(c["height_pct"])))
        self.w, self.h = max(200, int(self.sw * wp / 100)), max(100, int(self.sh * hp / 100))
        pos = str(c["position"]).lower()
        align = str(c.get("align") or "center").lower()
        margin = int(self.sh * 0.02)
        self.x = margin if align == "left" else (self.sw - self.w - margin if align == "right" else (self.sw - self.w) // 2)
        self.y = 0 if pos == "top" else (self.sh - self.h if pos == "bottom" else (self.sh - self.h) // 2)
        self.font_px = max(12, int(self.sh * max(1.0, min(8.0, float(c["font_pct"]))) / 100))
        self.line_h = int(self.font_px * LINE_SPACING)
        self.text_rgb = hex_rgb(str(c["text_color"]), (255, 255, 255))
        self.bg_rgb = hex_rgb(str(c["bg_color"]), (11, 13, 20))
        self.bg_a = int(255 * max(0, min(100, float(c["bg_opacity"]))) / 100)
        self.radius = int(self.sh * float(c["radius_pct"]) / 100)
        self._fonts.clear()
        self._dirty = True

    def font(self, px, bold):
        key = (px, bold)
        if key not in self._fonts:
            try:
                self._fonts[key] = ImageFont.truetype(FONT_BOLD if bold else FONT_REG, px)
            except OSError:
                self._fonts[key] = ImageFont.load_default(size=px)
        return self._fonts[key]

    # --- stdin -------------------------------------------------------------
    def _reader(self):
        # the runner writes UTF-8; read the raw byte stream so Windows' locale codec never touches it
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self.queue.put(line)
        self.queue.put('{"quit": true}')

    def _handle(self, msg):
        if msg.get("quit"):
            user32.DestroyWindow(self.hwnd)
            return
        if "config" in msg:
            self._apply_config(msg["config"])
        if "menu" in msg:
            self.menu = msg["menu"]
            self.card = None
            self._dirty = True
        if "card" in msg:
            c = dict(msg["card"] or {})
            img = None
            if c.get("image"):
                try:
                    img = Image.open(io.BytesIO(base64.b64decode(c["image"]))).convert("RGB")
                except Exception:
                    img = None
            c["image"] = img
            self.card = c
            self.menu = None
            self._dirty = True
        if "lines" in msg:
            self.lines = list(msg["lines"])
            self.index = int(msg.get("index", -1))
            self.card = None
            self.menu = None
            self.anim_t0 = None
            self._dirty = True
        elif "index" in msg:
            new = int(msg["index"])
            if new != self.index:
                self.anim_from = self.offset() + (new - self.index)
                self.index = new
                self.anim_t0 = time.monotonic()
                self._dirty = True
        if "show" in msg:
            self.visible = bool(msg["show"])
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if self.visible else SW_HIDE)
            self._dirty = True

    # --- rendering ---------------------------------------------------------
    def offset(self):
        """Animated displacement (in lines) of the current line from the centre."""
        if self.anim_t0 is None:
            return 0.0
        t = (time.monotonic() - self.anim_t0) / ANIM_SECONDS
        if t >= 1:
            self.anim_t0 = None
            return 0.0
        return self.anim_from * (1 - t) ** 3

    def render_card(self, img, d, pad):
        c = self.card
        inner = self.h - 2 * pad
        x = pad
        if c.get("image") is not None:
            side = inner
            art = c["image"]
            w, h = art.size
            scale = side / max(w, h)
            art = art.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            mask = Image.new("L", art.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, art.width - 1, art.height - 1), radius=int(self.radius * 0.6), fill=255)
            img.paste(art, (x + (side - art.width) // 2, pad + (side - art.height) // 2), mask)
            x += side + pad
        main = self.font(self.font_px, True)
        side_f = self.font(int(self.font_px * 0.75), False)
        label_f = self.font(int(self.font_px * 0.6), True)
        maxw = self.w - x - pad
        def clip(t, f):
            while d.textlength(t, font=f) > maxw and len(t) > 4:
                t = t[:-2].rstrip() + "…"
            return t
        y = pad + self.font_px * 0.6
        d.text((x, y), clip(c.get("title") or "", main), fill=(*self.text_rgb, 255), font=main, anchor="lm")
        y += self.font_px * 1.15
        if c.get("artist"):
            d.text((x, y), clip(c["artist"], side_f), fill=(*self.text_rgb, 200), font=side_f, anchor="lm")
            y += self.font_px * 1.2
        for label, value in c.get("rows") or []:
            if y > self.h - pad - self.font_px * 0.5:
                break
            lx = x
            if label:
                d.text((lx, y), label.upper(), fill=(*self.text_rgb, 140), font=label_f, anchor="lm")
                lx += self.font_px * 4.2
            d.text((lx, y), clip(str(value), side_f), fill=(*self.text_rgb, 235), font=side_f, anchor="lm")
            y += self.font_px * 0.95
        return img

    @staticmethod
    def _star(d, cx, cy, r, color):
        import math
        pts = []
        for i in range(10):
            a = math.radians(-90 + i * 36)
            rr = r if i % 2 == 0 else r * 0.45
            pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
        d.polygon(pts, fill=color)

    def render_menu(self, img, d, pad):
        m = self.menu
        items, sel = m.get("items") or [], int(m.get("index", 0))
        title_f = self.font(int(self.font_px * 0.9), True)
        item_f = self.font(int(self.font_px * 0.8), False)
        item_b = self.font(int(self.font_px * 0.8), True)
        row_h = int(self.font_px * 1.15)
        y = pad
        d.text((pad, y), m.get("title") or "", fill=(*self.text_rgb, 255), font=title_f, anchor="la")
        y += int(self.font_px * 1.3)
        d.line((pad, y, self.w - pad, y), fill=(*self.text_rgb, 60), width=1)
        y += pad // 2
        visible = max(1, int((self.h - y - pad) // row_h))
        # keep the selection inside the window; actions (non-station rows) stay at the end of the list
        first = max(0, min(sel - visible // 2, len(items) - visible))
        for it in items[first:first + visible]:
            idx = items.index(it)
            selected = idx == sel
            if selected:
                d.rounded_rectangle((pad // 2, y - 2, self.w - pad // 2, y + row_h - 4), radius=6, fill=(*self.text_rgb, 40))
            x = pad + int(self.font_px * 0.2)
            if it.get("playing"):
                d.polygon([(x, y + row_h * 0.25), (x, y + row_h * 0.75), (x + row_h * 0.3, y + row_h * 0.5)], fill=(70, 190, 120, 255))
            x += int(self.font_px * 0.9)
            text = it.get("text") or ""
            maxw = self.w - x - pad - (self.font_px if it.get("star") else 0)
            while d.textlength(text, font=item_b if selected else item_f) > maxw and len(text) > 4:
                text = text[:-2].rstrip() + "…"
            kind = it.get("kind")
            color = (*self.text_rgb, 255 if selected else (200 if kind == "station" else 170))
            d.text((x, y + row_h * 0.5 - 2), text, fill=color, font=item_b if selected else item_f, anchor="lm")
            if it.get("star"):
                self._star(d, self.w - pad - row_h * 0.3, y + row_h * 0.5 - 2, row_h * 0.28, (240, 190, 60, 255))
            y += row_h
        if len(items) > visible:
            d.text((self.w - pad, self.h - pad * 0.6), f"{sel + 1}/{len(items)}", fill=(*self.text_rgb, 120),
                   font=self.font(int(self.font_px * 0.6), False), anchor="rm")
        return img

    def render(self):
        img = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pad = int(self.font_px * 0.4)
        d.rounded_rectangle((0, 0, self.w - 1, self.h - 1), radius=self.radius, fill=(*self.bg_rgb, self.bg_a))
        if self.menu is not None:
            return self.render_menu(img, d, pad)
        if self.card is not None:
            return self.render_card(img, d, pad)
        if not self.lines:
            d.text((self.w / 2, self.h / 2), "no synced lyrics", fill=(*self.text_rgb, 160),
                   font=self.font(int(self.font_px * 0.7), False), anchor="mm")
            return img
        off = self.offset()
        cy = self.h / 2
        main, side = self.font(self.font_px, True), self.font(int(self.font_px * 0.8), False)
        for k in range(-VISIBLE - 1, VISIBLE + 2):
            i = self.index + k
            if i < 0 or i >= len(self.lines):
                continue
            y = cy + (k + off) * self.line_h
            if y < pad or y > self.h - pad:
                continue
            dist = abs(k + off)
            if dist < 0.5:
                fill, fnt = (*self.text_rgb, 255), main
            else:
                a = max(70, int(230 - dist * 45))
                fill, fnt = (*self.text_rgb, a), side
            text = self.lines[i] or "♪"
            while d.textlength(text, font=fnt) > self.w - 2 * pad and len(text) > 4:
                text = text[:-2].rstrip() + "…"
            d.text((self.w / 2, y), text, fill=fill, font=fnt, anchor="mm")
        return img

    def push(self, img):
        arr = np.asarray(img, dtype=np.uint16)
        a = arr[..., 3:4]
        bgra = np.empty((self.h, self.w, 4), dtype=np.uint8)
        bgra[..., 0] = (arr[..., 2] * a[..., 0] // 255)   # B premultiplied
        bgra[..., 1] = (arr[..., 1] * a[..., 0] // 255)   # G
        bgra[..., 2] = (arr[..., 0] * a[..., 0] // 255)   # R
        bgra[..., 3] = a[..., 0]
        data = bgra.tobytes()
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), self.w, -self.h, 1, 32, 0, 0, 0, 0, 0, 0)
        hdc_screen = user32.GetDC(None)
        hdc = gdi32.CreateCompatibleDC(hdc_screen)
        bits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(hdc, hbmp)
        ctypes.memmove(bits, data, len(data))
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(wt.POINT(self.x, self.y)), ctypes.byref(wt.SIZE(self.w, self.h)),
                                   hdc, ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc)
        user32.ReleaseDC(None, hdc_screen)

    # --- main loop ---------------------------------------------------------
    def run(self):
        msg = wt.MSG()
        while True:
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            try:
                while True:
                    self._handle(json.loads(self.queue.get_nowait()))
            except queue.Empty:
                pass
            except Exception as e:
                print("overlay error:", e, file=sys.stderr)
            if self.visible and (self._dirty or self.anim_t0 is not None):
                self.push(self.render())
                self._dirty = self.anim_t0 is not None
            time.sleep(0.016)


if __name__ == "__main__":
    import os
    try:
        Overlay().run()
    except Exception as e:  # pragma: no cover
        print("overlay fatal:", repr(e), file=sys.stderr)
        os._exit(1)
    os._exit(0)   # skip interpreter finalisation: the window callback + reader thread make it noisy

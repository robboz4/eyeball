#!/usr/bin/env python3
"""
eyeball.py - an animated eyeball for a round GC9A01A LCD (240x240) on a
Raspberry Pi Zero, with a built-in web control page.

  python3 eyeball.py            run on the Pi (web page on port 8080)
  python3 eyeball.py --fps      also print a timing breakdown every 5 s
  python3 eyeball.py --no-web   run without the web server
  python3 eyeball.py --nohw     no display attached (test the web page on any PC)
  python3 eyeball.py --preview  write eye_preview.png and exit

Open  http://<hostname>.local:8080  (the exact URLs are printed at start-up).

Everything the web page does is also a plain URL, so other programs (a sensor
script, Home Assistant, curl...) can drive the eye:

  /api/state
  /api/mode?m=auto|manual|sleepy|alert
  /api/gaze?x=0.5&y=-0.2          (-1..1, +y is down; switches to manual mode)
  /api/blink
  /api/pupil?v=0..1   or  v=auto
  /api/lids?v=0..1                (1 = fully open)
  /api/style?color=amber|blue|green|red|violet|ice|%23rrggbb&shape=round|slit

Sprites are pre-rendered once and cached as PNGs in ./eye_cache, so only the
very first start-up is slow.
"""

import hashlib
import json
import math
import os
import random
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

# ----------------------------------------------------------------------------
# CONFIG  -  check the pin block against adsb_radar_display.py
# ----------------------------------------------------------------------------
DC_PIN = "D25"        # data/command
CS_PIN = "CE0"        # chip select
RST_PIN = "D27"       # reset
BL_PIN = "D18"        # backlight (set to None if it is wired to 3V3)
SPI_HZ = int(os.environ.get("EYE_SPI_HZ", 32_000_000))   # lower it if you see noise

WEB_PORT = int(os.environ.get("EYE_PORT", 8080))
TARGET_FPS = 25

DEFAULT_COLOR = "amber"       # a preset name below, or "#rrggbb"
DEFAULT_SHAPE = "round"       # "round" or "slit"
LID_COLOR = (0, 0, 0)         # black is invisible in a Pepper's Ghost setup

PRESETS = {
    "amber": (205, 130, 20),
    "blue": (60, 140, 210),
    "green": (70, 150, 80),
    "red": (200, 25, 25),
    "violet": (140, 70, 200),
    "ice": (150, 210, 230),
}

W = H = 240
CX = CY = 120
IRIS_R = 70                   # iris radius in pixels
MAX_OFFSET = 44               # how far the iris can travel from centre
PUPIL_MIN, PUPIL_MAX = 0.28, 0.55   # fraction of iris radius
PUPIL_STEPS = 8
LID_STEPS = 24
SS = 3                        # supersampling factor for sprite rendering

CACHE_VERSION = 2
CACHE_DIR = os.environ.get(
    "EYE_CACHE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "eye_cache"))

rng = random.Random()         # behaviour randomness


# ----------------------------------------------------------------------------
# COLOURS
# ----------------------------------------------------------------------------
def resolve_color(key):
    """-> (rgb tuple, is_preset). Accepts a preset name or '#rrggbb'."""
    k = str(key).strip().lower()
    if k in PRESETS:
        return PRESETS[k], True
    h = k[1:] if k.startswith("#") else k
    if len(h) == 6 and all(c in "0123456789abcdef" for c in h):
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)), False
    raise ValueError("unknown colour: %r" % key)


def norm_color(key):
    k = str(key).strip().lower()
    if k in PRESETS:
        return k
    rgb, _ = resolve_color(k)
    return "#%02x%02x%02x" % rgb


# ----------------------------------------------------------------------------
# SPRITE GENERATION (cached on disk)
# ----------------------------------------------------------------------------
def _cache_tag():
    key = repr((CACHE_VERSION, W, IRIS_R, PUPIL_MIN, PUPIL_MAX, PUPIL_STEPS, LID_STEPS, SS))
    return hashlib.md5(key.encode()).hexdigest()[:8]


_TAG = _cache_tag()


def cached(name, builder, persist=True):
    path = os.path.join(CACHE_DIR, _TAG, name + ".png")
    if persist:
        try:
            im = Image.open(path)
            im.load()
            return im
        except (OSError, ValueError):
            pass
    im = builder()
    if persist:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            im.save(tmp, format="PNG")
            os.replace(tmp, path)
        except OSError:
            pass
    return im


def make_sclera():
    vr = random.Random(7)                       # fixed seed: same veins every time
    y, x = np.mgrid[0:H, 0:W]
    d = np.hypot(x - CX + 0.5, y - CY + 0.5) / (W / 2)
    base = np.array([240, 234, 226], dtype=np.float32)
    shade = 1.0 - 0.50 * np.clip(d, 0, 1) ** 2.4
    img = base[None, None, :] * shade[..., None]
    img[..., 1] *= 1.0 - 0.10 * np.clip(d, 0, 1) ** 3
    img[..., 2] *= 1.0 - 0.14 * np.clip(d, 0, 1) ** 3
    img[d > 1.0] = 0
    sclera = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))

    layer = Image.new("RGBA", (W * 2, H * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    def vein(x, y, ang, steps, depth):
        for _ in range(steps):
            ang += vr.uniform(-0.35, 0.35)
            nx, ny = x + math.cos(ang) * 3, y + math.sin(ang) * 3
            ld.line([x, y, nx, ny], fill=(175, 40, 45, 120), width=2)
            x, y = nx, ny
            if depth < 1 and vr.random() < 0.06:
                vein(x, y, ang + vr.choice((-0.7, 0.7)), steps // 2, depth + 1)

    for _ in range(16):
        a = vr.uniform(0, math.tau)
        sx, sy = W + math.cos(a) * W * 0.98, H + math.sin(a) * H * 0.98
        vein(sx, sy, a + math.pi, vr.randint(22, 42), 0)
    layer = layer.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.6))
    sclera.paste(layer, (0, 0), layer)
    return sclera


def make_iris(pupil_frac, color, shape):
    R = IRIS_R
    size = 2 * R + 2
    S = size * SS
    c = S / 2.0
    y, x = np.mgrid[0:S, 0:S]
    dx, dy = x - c + 0.5, y - c + 0.5
    r = np.hypot(dx, dy) / (R * SS)
    ang = np.arctan2(dy, dx)
    rc = np.minimum(r, 1.0)

    fib = 0.5 * np.sin(ang * 45 + 2.5 * np.sin(ang * 7)) \
        + 0.5 * np.sin(ang * 97 + 1.7 * np.sin(ang * 13))
    shade = 0.78 + 0.22 * fib
    ramp = np.clip(1.30 - 0.65 * rc, 0.45, 1.30)              # lighter near pupil
    limbal = 0.30 + 0.70 * np.clip((1.0 - rc) / 0.22, 0, 1)   # dark outer ring
    k = (shade * ramp * limbal)[..., None]
    rgb = np.clip(np.array(color, np.float32)[None, None, :] * k, 0, 255)

    alpha = np.where(r <= 1.0, 255, 0).astype(np.uint8)
    img = Image.fromarray(np.dstack([rgb.astype(np.uint8), alpha]))

    d = ImageDraw.Draw(img)
    pr = pupil_frac * R * SS
    if shape == "slit":
        pw, ph = pr * 0.32, min(pr * 2.2, R * SS * 0.95)
    else:
        pw = ph = pr
    d.ellipse([c - pw, c - ph, c + pw, c + ph], fill=(3, 3, 5, 255))
    return img.resize((size, size), Image.LANCZOS)


def make_highlight():
    S = 48 * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([6 * SS, 6 * SS, 26 * SS, 22 * SS], fill=(255, 255, 255, 235))
    d.ellipse([30 * SS, 30 * SS, 38 * SS, 36 * SS], fill=(255, 255, 255, 140))
    return img.resize((48, 48), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.8))


def make_vignette():
    y, x = np.mgrid[0:H, 0:W]
    d = np.hypot(x - CX + 0.5, y - CY + 0.5) / (W / 2)
    a = np.clip(d, 0, 1) ** 3.2 * 150
    arr = np.zeros((H, W, 4), np.uint8)
    arr[..., 3] = a.astype(np.uint8)
    return Image.fromarray(arr)


def make_lid_mask(openness):
    """'L' mask, 255 where the eyelid covers the eye, 0 where the eye shows."""
    S = W * SS
    m = Image.new("L", (S, S), 255)
    d = ImageDraw.Draw(m)
    rad = W / 2.0
    y_closed = CY + 22            # where the lids meet when shut
    top_full = CY - 150           # arc peaks that are fully open (off the display)
    bot_full = CY + 150
    y_t = y_closed + (top_full - y_closed) * openness
    y_b = y_closed + (bot_full - y_closed) * openness
    xs = [CX - rad + (2 * rad) * i / 60 for i in range(61)]
    top, bot = [], []
    for xv in xs:
        t = (xv - CX) / rad
        s = math.sqrt(max(0.0, 1 - t * t))
        top.append((xv * SS, (y_closed + (y_t - y_closed) * s) * SS))
        bot.append((xv * SS, (y_closed + (y_b - y_closed) * s) * SS))
    d.polygon(top + bot[::-1], fill=0)
    return m.resize((W, H), Image.LANCZOS)


def build_iris_set(color_key, shape):
    rgb, persist = resolve_color(color_key)
    sprites = []
    for i in range(PUPIL_STEPS):
        frac = PUPIL_MIN + (PUPIL_MAX - PUPIL_MIN) * i / (PUPIL_STEPS - 1)
        sprites.append(cached("iris_%s_%s_%d" % (color_key.lstrip("#"), shape, i),
                              lambda f=frac: make_iris(f, rgb, shape), persist))
    return sprites


def build_lids():
    """Eyelid mask + edge-darkening vignette merged into one mask per lid level."""
    vig = [None]
    lids = []
    for i in range(LID_STEPS):
        def make(i=i):
            if vig[0] is None:
                vig[0] = np.asarray(make_vignette().getchannel("A"), dtype=np.uint16)
            lm = np.asarray(make_lid_mask(i / (LID_STEPS - 1)), dtype=np.uint16)
            comb = 255 - ((255 - lm) * (255 - vig[0])) // 255
            return Image.fromarray(comb.astype(np.uint8))
        lids.append(cached("lid_%d" % i, make))
    return lids


# ----------------------------------------------------------------------------
# CONTROL (shared between the web server, sensors and the eye)
# ----------------------------------------------------------------------------
MODES = ("auto", "manual", "sleepy", "alert")
SHAPES = ("round", "slit")


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class Control:
    """Thread-safe settings. Anything (web page, PIR sensor...) can call these."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mode = "auto"
        self.gx = self.gy = 0.0          # manual gaze target
        self.pupil = None                # None = automatic, else 0..1
        self.lid = 1.0                   # user eyelid opening 0..1
        self.color = norm_color(DEFAULT_COLOR)
        self.shape = DEFAULT_SHAPE
        self._blink = False
        # written by the eye:
        self.live = (0.0, 0.0)
        self.building = False
        self.applied = (self.color, self.shape)

    def set_mode(self, m):
        if m not in MODES:
            raise ValueError("mode must be one of: " + ", ".join(MODES))
        with self.lock:
            self.mode = m

    def set_gaze(self, x, y):
        x, y = float(x), float(y)
        m = math.hypot(x, y)
        if m > 1:
            x, y = x / m, y / m
        with self.lock:
            self.gx, self.gy = x, y
            self.mode = "manual"

    def request_blink(self):
        with self.lock:
            self._blink = True

    def set_pupil(self, v):
        with self.lock:
            self.pupil = None if str(v).lower() == "auto" else _clamp(float(v), 0.0, 1.0)

    def set_lid(self, v):
        with self.lock:
            self.lid = _clamp(float(v), 0.0, 1.0)

    def set_style(self, color=None, shape=None):
        if color is not None:
            color = norm_color(color)
        if shape is not None and shape not in SHAPES:
            raise ValueError("shape must be round or slit")
        with self.lock:
            if color is not None:
                self.color = color
            if shape is not None:
                self.shape = shape

    def poll(self):
        """Used by the eye once per frame."""
        with self.lock:
            blink, self._blink = self._blink, False
            return (self.mode, self.gx, self.gy, self.pupil, self.lid,
                    blink, self.color, self.shape)

    def state(self):
        with self.lock:
            return {
                "mode": self.mode, "gaze": [self.gx, self.gy],
                "live": [round(self.live[0], 3), round(self.live[1], 3)],
                "pupil": self.pupil, "lids": self.lid,
                "color": self.color, "shape": self.shape,
                "building": self.building,
                "presets": {k: "#%02x%02x%02x" % v for k, v in PRESETS.items()},
            }


# ----------------------------------------------------------------------------
# EYE STATE + RENDERER
# ----------------------------------------------------------------------------
MODE_CFG = {
    #            lid cap  pupil  glance interval   blink interval  gaze speed  blink slowdown
    "auto":   dict(cap=0.96, pupil=None, look=(0.6, 3.2), blink=(2.0, 6.0), gaze_k=16, blink_scale=1.0),
    "manual": dict(cap=0.96, pupil=None, look=None,       blink=(2.0, 6.0), gaze_k=16, blink_scale=1.0),
    "sleepy": dict(cap=0.42, pupil=0.12, look=(2.5, 6.0), blink=(3.0, 7.0), gaze_k=6,  blink_scale=2.2),
    "alert":  dict(cap=1.00, pupil=1.00, look=(1.5, 3.5), blink=(5.0, 10.0), gaze_k=12, blink_scale=1.0),
}


class Eye:
    def __init__(self, ctl):
        self.ctl = ctl
        self.sclera = cached("sclera", make_sclera)
        self.highlight = cached("highlight", make_highlight)
        self.lids = build_lids()
        self.style = (ctl.color, ctl.shape)
        self.pupils = build_iris_set(*self.style)
        ctl.applied = self.style
        self.building = False

        self.mode = "auto"
        self.gx = self.gy = 0.0            # current gaze (unit disk)
        self.tx = self.ty = 0.0            # target gaze
        self.pup = 0.75                    # current pupil level 0..1
        self.cap = MODE_CFG["auto"]["cap"]
        self.lid_user = 1.0
        self.next_look = 1.0
        self.next_blink = 2.5
        self.blink_t = None                # None, or seconds into a blink
        self.blink_scale = 1.0
        self.blinks_left = 0
        self.blink_v = 1.0
        self.t = 0.0

    # -- behaviour ---------------------------------------------------------
    def _enter(self, mode):
        self.mode = mode
        cfg = MODE_CFG[mode]
        self.next_look = self.t
        self.next_blink = min(self.next_blink, self.t + rng.uniform(*cfg["blink"]))

    def _pick_target(self):
        cfg = MODE_CFG[self.mode]
        if self.mode == "auto":
            r = rng.random()
            if r < 0.15:
                self.tx = self.ty = 0.0
            elif r < 0.65:
                a, m = rng.uniform(0, math.tau), rng.uniform(0.1, 0.45)
                self.tx, self.ty = math.cos(a) * m, math.sin(a) * m
            else:
                a, m = rng.uniform(0, math.tau), rng.uniform(0.6, 1.0)
                self.tx, self.ty = math.cos(a) * m, math.sin(a) * m
                if rng.random() < 0.5:             # big glance often triggers a blink
                    self.next_blink = min(self.next_blink, self.t + 0.15)
        elif self.mode == "sleepy":
            a, m = rng.uniform(0, math.tau), rng.uniform(0.0, 0.25)
            self.tx, self.ty = math.cos(a) * m, math.sin(a) * m * 0.5 + 0.15
        elif self.mode == "alert":                 # staring straight ahead
            a, m = rng.uniform(0, math.tau), rng.uniform(0.0, 0.05)
            self.tx, self.ty = math.cos(a) * m, math.sin(a) * m
        self.next_look = self.t + rng.uniform(*cfg["look"])

    def _start_blink(self):
        self.blink_t = 0.0
        self.blink_scale = MODE_CFG[self.mode]["blink_scale"]

    def _blink_openness(self):
        if self.blink_t is None:
            return 1.0
        close, opn = 0.07 * self.blink_scale, 0.13 * self.blink_scale
        b = self.blink_t
        if b < close:
            return 1 - b / close
        if b < close + opn:
            v = (b - close) / opn
            return v * v * (3 - 2 * v)
        self.blink_t = None
        if self.blinks_left > 0:
            self.blinks_left -= 1
            self._start_blink()
        else:
            self.next_blink = self.t + rng.uniform(*MODE_CFG[self.mode]["blink"])
        return 1.0

    # -- style (iris colour / pupil shape) rebuilt in the background --------
    def _start_build(self, color, shape):
        self.building = True
        self.ctl.building = True
        threading.Thread(target=self._build_style, args=(color, shape), daemon=True).start()

    def _build_style(self, color, shape):
        try:
            self.pupils = build_iris_set(color, shape)
        except Exception as exc:                   # keep the old sprites on failure
            print("style build failed:", exc)
        self.style = (color, shape)
        self.ctl.applied = self.style
        self.building = False
        self.ctl.building = False

    # -- per-frame update --------------------------------------------------
    def update(self, dt):
        self.t += dt
        mode, mgx, mgy, pupil_user, lid_user, want_blink, color, shape = self.ctl.poll()
        if mode != self.mode:
            self._enter(mode)
        cfg = MODE_CFG[mode]

        if not self.building and (color, shape) != self.style:
            self._start_build(color, shape)

        # gaze
        if mode == "manual":
            self.tx, self.ty = mgx, mgy
        elif self.t >= self.next_look:
            self._pick_target()
        k = 1 - math.exp(-dt * cfg["gaze_k"])
        self.gx += (self.tx - self.gx) * k
        self.gy += (self.ty - self.gy) * k
        self.ctl.live = (self.gx, self.gy)

        # pupil size / lid opening ease toward their targets
        ks = 1 - math.exp(-dt * 4.0)
        if pupil_user is not None:
            p_target = pupil_user
        elif cfg["pupil"] is not None:
            p_target = cfg["pupil"]
        else:
            p_target = 0.75 + 0.175 * math.sin(self.t * 0.6) + 0.075 * math.sin(self.t * 1.7)
        self.pup += (p_target - self.pup) * ks
        self.cap += (cfg["cap"] - self.cap) * ks
        self.lid_user += (lid_user - self.lid_user) * min(1.0, ks * 2)

        # blinking
        if want_blink and self.blink_t is None:
            self._start_blink()
        elif self.blink_t is None and self.t >= self.next_blink:
            self._start_blink()
            if mode == "auto" and rng.random() < 0.15:
                self.blinks_left = 1               # occasional double blink
        elif self.blink_t is not None:
            self.blink_t += dt
        self.blink_v = self._blink_openness()

    # -- drawing -----------------------------------------------------------
    def render(self):
        gx, gy = self.gx, self.gy
        m = math.hypot(gx, gy)
        if m > 1:
            gx, gy = gx / m, gy / m
        ix = int(round(CX + gx * MAX_OFFSET))
        iy = int(round(CY + gy * MAX_OFFSET))

        pupils = self.pupils
        idx = int(_clamp(round(self.pup * (PUPIL_STEPS - 1)), 0, PUPIL_STEPS - 1))
        iris = pupils[idx]

        frame = self.sclera.copy()
        frame.paste(iris, (ix - iris.width // 2, iy - iris.height // 2), iris)
        hx = int(CX - 40 + gx * MAX_OFFSET * 0.35)
        hy = int(CY - 46 + gy * MAX_OFFSET * 0.35)
        frame.paste(self.highlight, (hx - 24, hy - 24), self.highlight)

        openness = self.cap * (1 - 0.125 * max(0.0, gy)) * self.lid_user * self.blink_v
        li = int(_clamp(round(openness * (LID_STEPS - 1)), 0, LID_STEPS - 1))
        frame.paste(LID_COLOR, (0, 0, W, H), self.lids[li])      # lids + vignette
        return frame


# ----------------------------------------------------------------------------
# RGB888 -> RGB565 (big-endian, as the GC9A01A wants) using only Pillow's C
# routines: much cheaper on a Pi Zero than doing the bit-twiddling in numpy.
# ----------------------------------------------------------------------------
_LUT_R = [v & 0xF8 for v in range(256)]
_LUT_GH = [v >> 5 for v in range(256)]
_LUT_GL = [(v << 3) & 0xE0 for v in range(256)]
_LUT_B = [v >> 3 for v in range(256)]


def to_rgb565(img):
    r, g, b = img.split()
    hi = ImageChops.add(r.point(_LUT_R), g.point(_LUT_GH))
    lo = ImageChops.add(g.point(_LUT_GL), b.point(_LUT_B))
    return Image.merge("LA", (hi, lo)).tobytes()


# ----------------------------------------------------------------------------
# PANELS
# ----------------------------------------------------------------------------
class NullPanel:
    """Stand-in when no display is attached (--nohw)."""

    def __init__(self):
        self.t_conv = self.t_spi = 0.0

    def show(self, img, box=None):
        if box is None:
            box = (0, 0, W, H)
        t0 = time.monotonic()
        to_rgb565(img.crop(box))
        self.t_conv += time.monotonic() - t0
        return (box[2] - box[0]) * (box[3] - box[1])


class Panel(NullPanel):
    """Blinka / adafruit_gc9a01a, same init as the radar program."""

    def __init__(self):
        super().__init__()
        import board
        import displayio
        import fourwire
        import adafruit_gc9a01a

        displayio.release_displays()
        spi = board.SPI()
        self.bus = fourwire.FourWire(
            spi,
            command=getattr(board, DC_PIN),
            chip_select=getattr(board, CS_PIN),
            reset=getattr(board, RST_PIN),
            baudrate=SPI_HZ,
        )
        self.display = adafruit_gc9a01a.GC9A01A(
            self.bus, width=W, height=H, auto_refresh=False
        )
        self._bl = None
        if BL_PIN:
            import digitalio
            self._bl = digitalio.DigitalInOut(getattr(board, BL_PIN))
            self._bl.switch_to_output(value=True)

    def show(self, img, box=None):
        """Send img (or just the sub-rectangle box=(x0,y0,x1,y1)) to the panel."""
        if box is None:
            box = (0, 0, W, H)
        x0, y0, x1, y1 = box
        t0 = time.monotonic()
        buf = to_rgb565(img.crop(box))
        t1 = time.monotonic()
        self.bus.send(0x2A, bytes([x0 >> 8, x0 & 255, (x1 - 1) >> 8, (x1 - 1) & 255]))
        self.bus.send(0x2B, bytes([y0 >> 8, y0 & 255, (y1 - 1) >> 8, (y1 - 1) & 255]))
        self.bus.send(0x2C, buf)                   # RAMWR
        t2 = time.monotonic()
        self.t_conv += t1 - t0
        self.t_spi += t2 - t1
        return (x1 - x0) * (y1 - y0)


# ----------------------------------------------------------------------------
# WEB SERVER (standard library only)
# ----------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Eyeball</title>
<style>
:root{color-scheme:dark}
body{margin:0 auto;padding:16px;max-width:480px;font:16px system-ui,sans-serif;background:#111;color:#eee}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.1em;color:#999;margin:22px 0 8px}
.row{display:flex;gap:8px;flex-wrap:wrap}
button{flex:1;min-width:70px;padding:12px 8px;border:1px solid #444;border-radius:8px;background:#222;color:#eee;font-size:15px}
button.on{background:#8a5a12;border-color:#d99a2b;color:#fff}
button.sw{flex:0 0 46px;min-width:46px;height:46px;padding:0;border-radius:50%;border:3px solid #444}
button.sw.on{border-color:#fff}
#pad{position:relative;width:100%;aspect-ratio:1;border-radius:50%;background:radial-gradient(#2c2c2c,#151515);
     border:2px solid #444;touch-action:none;margin:8px 0;cursor:crosshair}
#dot{position:absolute;width:22%;height:22%;border-radius:50%;background:#d99a2b;left:50%;top:50%;
     transform:translate(-50%,-50%);pointer-events:none;box-shadow:0 0 0 5px #000a}
input[type=range]{width:100%}
input[type=color]{width:46px;height:46px;border:0;background:none;padding:0}
#status{color:#999;font-size:13px;margin-top:18px;min-height:1.3em}
</style></head><body>
<h1>Eyeball</h1>
<h2>Mode</h2>
<div class="row" id="modes">
  <button data-m="auto">Auto</button><button data-m="manual">Manual</button>
  <button data-m="sleepy">Sleepy</button><button data-m="alert">Alert</button>
</div>
<h2>Look (drag on the circle)</h2>
<div id="pad"><div id="dot"></div></div>
<div class="row"><button id="center">Center</button><button id="blink">Blink</button></div>
<h2>Pupil size</h2>
<div class="row"><input id="pupil" type="range" min="0" max="100" value="50" style="flex:1 1 60%">
<button id="pauto" style="flex:0 0 80px">Auto</button></div>
<h2>Eyelids</h2>
<input id="lids" type="range" min="0" max="100" value="100">
<h2>Iris colour</h2>
<div class="row" id="colors"></div>
<div class="row" style="margin-top:8px;align-items:center">
  <input id="custom" type="color" value="#cd8214"><span style="color:#999;font-size:13px">custom colour</span></div>
<h2>Pupil shape</h2>
<div class="row"><button data-s="round">Round</button><button data-s="slit">Slit</button></div>
<div id="status"></div>
<script>
const $=id=>document.getElementById(id);
const api=async p=>{try{const r=await fetch('/api/'+p);return await r.json()}catch(e){return null}};
const throttled=(fn,ms)=>{let last=0,t=null;return(...a)=>{const n=Date.now();clearTimeout(t);
  if(n-last>=ms){last=n;fn(...a)}else t=setTimeout(()=>{last=Date.now();fn(...a)},ms-(n-last))}};
let dragging=false,busy={pupil:false,lids:false},made=false;

function render(s){
  if(!s)return;
  document.querySelectorAll('#modes button').forEach(b=>b.classList.toggle('on',b.dataset.m===s.mode));
  document.querySelectorAll('[data-s]').forEach(b=>b.classList.toggle('on',b.dataset.s===s.shape));
  if(!made){made=true;const c=$('colors');
    for(const [n,hex] of Object.entries(s.presets)){const b=document.createElement('button');
      b.className='sw';b.dataset.c=n;b.style.background=hex;b.title=n;
      b.onclick=()=>api('style?color='+n).then(render);c.appendChild(b)}}
  document.querySelectorAll('#colors button').forEach(b=>b.classList.toggle('on',b.dataset.c===s.color));
  $('pauto').classList.toggle('on',s.pupil===null);
  if(!busy.pupil&&s.pupil!==null)$('pupil').value=Math.round(s.pupil*100);
  if(!busy.lids)$('lids').value=Math.round(s.lids*100);
  if(!dragging){const g=s.mode==='manual'?s.gaze:s.live;
    $('dot').style.left=(50+g[0]*39)+'%';$('dot').style.top=(50+g[1]*39)+'%'}
  $('status').textContent=s.building?'Building new eye style... (the animation may stutter for a few seconds)':'';
}
const refresh=()=>api('state').then(render);
setInterval(refresh,800);refresh();

document.querySelectorAll('#modes button').forEach(b=>b.onclick=()=>api('mode?m='+b.dataset.m).then(render));
document.querySelectorAll('[data-s]').forEach(b=>b.onclick=()=>api('style?shape='+b.dataset.s).then(render));
$('blink').onclick=()=>api('blink');
$('center').onclick=()=>api('gaze?x=0&y=0').then(render);
$('pauto').onclick=()=>api('pupil?v=auto').then(render);
$('custom').onchange=e=>api('style?color='+encodeURIComponent(e.target.value)).then(render);

const sendPupil=throttled(v=>api('pupil?v='+v),80), sendLids=throttled(v=>api('lids?v='+v),80);
$('pupil').oninput=e=>{busy.pupil=true;sendPupil(e.target.value/100)};
$('pupil').onchange=()=>{setTimeout(()=>busy.pupil=false,400)};
$('lids').oninput=e=>{busy.lids=true;sendLids(e.target.value/100)};
$('lids').onchange=()=>{setTimeout(()=>busy.lids=false,400)};

const pad=$('pad'),sendGaze=throttled((x,y)=>api('gaze?x='+x.toFixed(3)+'&y='+y.toFixed(3)),70);
function move(e){const r=pad.getBoundingClientRect();
  let x=((e.clientX-r.left)/r.width)*2-1,y=((e.clientY-r.top)/r.height)*2-1;
  const m=Math.hypot(x,y);if(m>1){x/=m;y/=m}
  $('dot').style.left=(50+x*39)+'%';$('dot').style.top=(50+y*39)+'%';sendGaze(x,y)}
pad.onpointerdown=e=>{dragging=true;pad.setPointerCapture(e.pointerId);move(e)};
pad.onpointermove=e=>{if(dragging)move(e)};
pad.onpointerup=pad.onpointercancel=()=>{setTimeout(()=>dragging=false,400)};
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    ctl = None

    def log_message(self, *args):          # keep the console quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ctl = self.ctl
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            if u.path == "/api/state":
                pass
            elif u.path == "/api/mode":
                ctl.set_mode(q.get("m", ""))
            elif u.path == "/api/gaze":
                ctl.set_gaze(q["x"], q["y"])
            elif u.path == "/api/blink":
                ctl.request_blink()
            elif u.path == "/api/pupil":
                ctl.set_pupil(q["v"])
            elif u.path == "/api/lids":
                ctl.set_lid(q["v"])
            elif u.path == "/api/style":
                ctl.set_style(q.get("color"), q.get("shape"))
            else:
                return self._send(404, '{"error": "not found"}')
            self._send(200, json.dumps(ctl.state()))
        except (ValueError, KeyError) as exc:
            self._send(400, json.dumps({"error": "bad request: %s" % exc}))
        except (BrokenPipeError, ConnectionResetError):
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def start_web(ctl, port):
    handler = type("BoundHandler", (Handler,), {"ctl": ctl})
    try:
        srv = _Server(("0.0.0.0", port), handler)
    except OSError as exc:
        print("Web server could not start on port %d: %s" % (port, exc))
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("Web control:  http://%s.local:%d   or   http://%s:%d"
          % (socket.gethostname(), port, local_ip(), port))
    return srv


# ----------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    preview = "--preview" in args
    show_fps = "--fps" in args

    print("Loading eye sprites (first run builds them and can take ~15 s)...")
    t0 = time.time()
    ctl = Control()
    eye = Eye(ctl)
    print("  ready in %.1fs" % (time.time() - t0))

    if preview:
        sheet = Image.new("RGB", (W * 4, H * 2), (30, 30, 30))
        for i, (gx, gy) in enumerate([(0.0, 0.0), (0.7, -0.3), (-0.8, 0.2), (0.0, 0.9)]):
            eye.gx, eye.gy = gx, gy
            sheet.paste(eye.render(), (i * W, 0))
        eye.gx = eye.gy = 0.0
        for i, o in enumerate([0.75, 0.45, 0.2, 0.05]):
            li = int(round(o * (LID_STEPS - 1)))
            f = eye.sclera.copy()
            iris = eye.pupils[3]
            f.paste(iris, (CX - iris.width // 2, CY - iris.height // 2), iris)
            f.paste(LID_COLOR, (0, 0, W, H), eye.lids[li])
            sheet.paste(f, (i * W, H))
        sheet.save("eye_preview.png")
        print("wrote eye_preview.png")
        return

    if "--no-web" not in args:
        start_web(ctl, WEB_PORT)

    panel = NullPanel() if "--nohw" in args else Panel()
    frame_time = 1.0 / TARGET_FPS
    last = time.monotonic()
    prev = None
    n = sent = 0
    t_upd = t_diff = 0.0
    px = 0
    stat_t = last
    try:
        while True:
            now = time.monotonic()
            dt = min(now - last, 0.1)
            last = now

            a = time.monotonic()
            eye.update(dt)
            frame = eye.render()
            b = time.monotonic()
            box = None if prev is None else ImageChops.difference(prev, frame).getbbox()
            c = time.monotonic()
            if prev is None or box is not None:
                px += panel.show(frame, box)
                sent += 1
            prev = frame
            d = time.monotonic()

            n += 1
            t_upd += b - a
            t_diff += c - b
            if show_fps and d - stat_t >= 5:
                k = max(sent, 1)
                print("%4.1f loops/s | drawn %d/%d | render %.0f ms  diff %.0f ms | per sent frame: "
                      "convert %.0f ms  spi %.0f ms  (%d px)"
                      % (n / (d - stat_t), sent, n, 1000 * t_upd / n, 1000 * t_diff / n,
                         1000 * panel.t_conv / k, 1000 * panel.t_spi / k, px / k))
                n = sent = px = 0
                t_upd = t_diff = 0.0
                panel.t_conv = panel.t_spi = 0.0
                stat_t = d
            spare = frame_time - (time.monotonic() - now)
            if spare > 0:
                time.sleep(spare)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            panel.show(Image.new("RGB", (W, H), (0, 0, 0)))
        except Exception:
            pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
eyeball.py - a single animated eyeball for a round GC9A01A LCD (240x240)
on a Raspberry Pi Zero / Zero W.

Uses the same stack as the ADS-B radar (Blinka + adafruit_gc9a01a + fourwire)
for display init, but pushes finished frames straight to the panel instead of
going through displayio's slow software renderer.

Every sprite (sclera, iris, pupils, eyelid masks) is pre-rendered once at
start-up, so each frame is just a few fast Pillow pastes + one SPI transfer.

Run:      python3 eyeball.py
Preview:  python3 eyeball.py --preview      (writes eye_preview.png, no hardware)
FPS info: python3 eyeball.py --fps
"""

import math
import random
import sys
import time

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

# ----------------------------------------------------------------------------
# CONFIG  -  check the pin block against adsb_radar_display.py
# ----------------------------------------------------------------------------
DC_PIN = "D25"        # data/command
CS_PIN = "CE0"        # chip select
RST_PIN = "D27"       # reset
BL_PIN = "D18"        # backlight (set to None if it is wired to 3V3)
SPI_HZ = 32_000_000   # drop to 24_000_000 if you see noise/garbage

TARGET_FPS = 25

IRIS_COLOR = (205, 130, 20)   # amber. Try (60,140,200) blue, (70,150,80) green, (200,20,20) red
PUPIL_SHAPE = "round"         # "round" or "slit" (cat/demon pupil)
LID_COLOR = (0, 0, 0)         # black is invisible in a Pepper's Ghost setup

W = H = 240
CX = CY = 120
IRIS_R = 70                   # iris radius in pixels
MAX_OFFSET = 44               # how far the iris can travel from centre
PUPIL_MIN, PUPIL_MAX = 0.28, 0.55   # fraction of iris radius
PUPIL_STEPS = 8
LID_STEPS = 24
SS = 3                        # supersampling factor for sprite rendering

rng = random.Random(7)        # fixed seed: same veins/iris texture every run


# ----------------------------------------------------------------------------
# SPRITE GENERATION (runs once)
# ----------------------------------------------------------------------------
def make_sclera():
    y, x = np.mgrid[0:H, 0:W]
    d = np.hypot(x - CX + 0.5, y - CY + 0.5) / (W / 2)
    base = np.array([240, 234, 226], dtype=np.float32)
    shade = 1.0 - 0.50 * np.clip(d, 0, 1) ** 2.4
    img = base[None, None, :] * shade[..., None]
    # slight pink/red tint toward the edge
    img[..., 1] *= 1.0 - 0.10 * np.clip(d, 0, 1) ** 3
    img[..., 2] *= 1.0 - 0.14 * np.clip(d, 0, 1) ** 3
    img[d > 1.0] = 0
    sclera = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")

    # blood vessels
    layer = Image.new("RGBA", (W * 2, H * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    def vein(x, y, ang, steps, depth):
        for _ in range(steps):
            ang += rng.uniform(-0.35, 0.35)
            nx, ny = x + math.cos(ang) * 3, y + math.sin(ang) * 3
            ld.line([x, y, nx, ny], fill=(175, 40, 45, 120), width=2)
            x, y = nx, ny
            if depth < 1 and rng.random() < 0.06:
                vein(x, y, ang + rng.choice((-0.7, 0.7)), steps // 2, depth + 1)

    for _ in range(16):
        a = rng.uniform(0, math.tau)
        sx, sy = W + math.cos(a) * W * 0.98, H + math.sin(a) * H * 0.98
        vein(sx, sy, a + math.pi, rng.randint(22, 42), 0)
    layer = layer.resize((W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.6))
    sclera.paste(layer, (0, 0), layer)
    return sclera


def make_iris(pupil_frac):
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
    rgb = np.clip(np.array(IRIS_COLOR, np.float32)[None, None, :] * k, 0, 255)

    alpha = np.where(r <= 1.0, 255, 0).astype(np.uint8)
    arr = np.dstack([rgb.astype(np.uint8), alpha])
    img = Image.fromarray(arr, "RGBA")

    d = ImageDraw.Draw(img)
    pr = pupil_frac * R * SS
    if PUPIL_SHAPE == "slit":
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
    img = img.resize((48, 48), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.8))
    return img


def make_vignette():
    y, x = np.mgrid[0:H, 0:W]
    d = np.hypot(x - CX + 0.5, y - CY + 0.5) / (W / 2)
    a = np.clip(d, 0, 1) ** 3.2 * 150
    arr = np.zeros((H, W, 4), np.uint8)
    arr[..., 3] = a.astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


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


# ----------------------------------------------------------------------------
# EYE STATE + RENDERER
# ----------------------------------------------------------------------------
class Eye:
    def __init__(self):
        self.sclera = make_sclera()
        self.pupils = [make_iris(PUPIL_MIN + (PUPIL_MAX - PUPIL_MIN) * i / (PUPIL_STEPS - 1))
                       for i in range(PUPIL_STEPS)]
        self.highlight = make_highlight()
        # eyelid mask + edge-darkening vignette merged into one mask per lid level
        vig = np.asarray(make_vignette().getchannel("A"), dtype=np.uint16)
        self.lids = []
        for i in range(LID_STEPS):
            lm = np.asarray(make_lid_mask(i / (LID_STEPS - 1)), dtype=np.uint16)
            comb = 255 - ((255 - lm) * (255 - vig)) // 255
            self.lids.append(Image.fromarray(comb.astype(np.uint8), "L"))

        self.gx = self.gy = 0.0            # current gaze (unit disk)
        self.tx = self.ty = 0.0            # target gaze
        self.next_look = 1.0
        self.next_blink = 2.5
        self.blink_t = None                # None, or seconds into a blink
        self.blinks_left = 0
        self.t = 0.0

    # -- behaviour ---------------------------------------------------------
    def _pick_target(self):
        r = rng.random()
        if r < 0.15:
            self.tx = self.ty = 0.0
        elif r < 0.65:
            a, m = rng.uniform(0, math.tau), rng.uniform(0.1, 0.45)
            self.tx, self.ty = math.cos(a) * m, math.sin(a) * m
        else:
            a, m = rng.uniform(0, math.tau), rng.uniform(0.6, 1.0)
            self.tx, self.ty = math.cos(a) * m, math.sin(a) * m
            if rng.random() < 0.5:                 # big glance often triggers a blink
                self.next_blink = min(self.next_blink, self.t + 0.15)
        self.next_look = self.t + rng.uniform(0.6, 3.2)

    def _blink_openness(self):
        if self.blink_t is None:
            return 1.0
        close, opn = 0.07, 0.13
        b = self.blink_t
        if b < close:
            v = 1 - b / close
        elif b < close + opn:
            v = (b - close) / opn
        else:
            self.blink_t = None
            if self.blinks_left > 0:
                self.blinks_left -= 1
                self.blink_t = 0.0
            else:
                self.next_blink = self.t + rng.uniform(2.0, 6.0)
            return 1.0
        return v * v * (3 - 2 * v) if b >= close else v

    def update(self, dt):
        self.t += dt
        if self.t >= self.next_look:
            self._pick_target()
        # fast, eased saccade
        k = 1 - math.exp(-dt * 16.0)
        self.gx += (self.tx - self.gx) * k
        self.gy += (self.ty - self.gy) * k

        if self.blink_t is None and self.t >= self.next_blink:
            self.blink_t = 0.0
            if rng.random() < 0.15:
                self.blinks_left = 1               # occasional double blink
        elif self.blink_t is not None:
            self.blink_t += dt

    # -- drawing -----------------------------------------------------------
    def render(self):
        t = self.t
        gx, gy = self.gx, self.gy
        m = math.hypot(gx, gy)
        if m > 1:
            gx, gy = gx / m, gy / m
        ix = int(round(CX + gx * MAX_OFFSET))
        iy = int(round(CY + gy * MAX_OFFSET))

        pf = 0.5 + 0.35 * math.sin(t * 0.6) + 0.15 * math.sin(t * 1.7)   # -1..1ish
        idx = int(np.clip((pf * 0.5 + 0.5) * (PUPIL_STEPS - 1), 0, PUPIL_STEPS - 1))
        iris = self.pupils[idx]

        frame = self.sclera.copy()
        frame.paste(iris, (ix - iris.width // 2, iy - iris.height // 2), iris)
        hx = int(CX - 40 + gx * MAX_OFFSET * 0.35)
        hy = int(CY - 46 + gy * MAX_OFFSET * 0.35)
        frame.paste(self.highlight, (hx - 24, hy - 24), self.highlight)

        openness = 0.96 - 0.12 * max(0.0, gy)               # lids droop when looking down
        openness *= self._blink_openness()
        li = int(np.clip(round(openness * (LID_STEPS - 1)), 0, LID_STEPS - 1))
        frame.paste(LID_COLOR, (0, 0, W, H), self.lids[li])   # lids + vignette
        return frame


# RGB888 -> RGB565 (big-endian, as the GC9A01A wants) using only Pillow's C
# routines: much cheaper on a Pi Zero than doing the bit-twiddling in numpy.
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
# PANEL (Blinka / adafruit_gc9a01a, same init as the radar program)
# ----------------------------------------------------------------------------
class Panel:
    def __init__(self):
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
        self.t_conv = self.t_spi = 0.0
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
def main():
    preview = "--preview" in sys.argv
    show_fps = "--fps" in sys.argv

    print("Building eye sprites...")
    t0 = time.time()
    eye = Eye()
    print("  done in %.1fs" % (time.time() - t0))

    if preview:
        sheet = Image.new("RGB", (W * 4, H * 2), (30, 30, 30))
        steps = [(0.0, 0.0), (0.7, -0.3), (-0.8, 0.2), (0.0, 0.9)]
        for i, (gx, gy) in enumerate(steps):
            eye.gx, eye.gy = gx, gy
            eye.blink_t = None
            sheet.paste(eye.render(), (i * W, 0))
        for i, o in enumerate([0.75, 0.45, 0.2, 0.05]):
            eye.gx = eye.gy = 0.1
            eye.blink_t = None
            li = int(round(o * (LID_STEPS - 1)))
            f2 = eye.sclera.copy()
            iris = eye.pupils[3]
            f2.paste(iris, (CX - iris.width // 2, CY - iris.height // 2), iris)
            f2.paste(LID_COLOR, (0, 0, W, H), eye.lids[li])
            sheet.paste(f2, (i * W, H))
        sheet.save("eye_preview.png")
        print("wrote eye_preview.png")
        return

    panel = Panel()
    frame_time = 1.0 / TARGET_FPS
    last = time.monotonic()
    prev = None
    n = sent = 0
    t_upd = t_diff = t_send = 0.0
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
            t_send += d - c
            if show_fps and d - stat_t >= 5:
                k = max(sent, 1)
                print("%4.1f loops/s | drawn %d/%d | render %.0f ms  diff %.0f ms | per sent frame: "
                      "convert %.0f ms  spi %.0f ms  (%d px = %d KB, %.1f MB/s)"
                      % (n / (d - stat_t), sent, n, 1000 * t_upd / n, 1000 * t_diff / n,
                         1000 * panel.t_conv / k, 1000 * panel.t_spi / k, px / k, px * 2 / k / 1024,
                         (px * 2 / 1e6) / max(panel.t_spi, 1e-9)))
                n = sent = px = 0
                t_upd = t_diff = t_send = 0.0
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

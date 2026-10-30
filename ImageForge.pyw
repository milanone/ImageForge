"""ImageForge - a general-purpose image editor whose tools are small plug-in functions.

Open an image, optionally drag a rectangle to limit an operation to it, pick a tool in the list, tune its
parameters (the result is previewed live) and press Apply. Every step goes on an Undo/Redo history.

A tool is a function registered with the `@tool` decorator that takes an RGB uint8 array and returns one.
The built-in ones are below; more can be dropped as .py files into the `plugins` folder next to this file
(see plugins/example_threshold.py) without touching this file.

The core (registry, tools, history, load/save) does not need a display, so the tests can import it.
"""
import importlib.util
import math
import os
import sys
import traceback

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

APP_NAME = "ImageForge"
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, "plugins")
HISTORY_BYTES = 1 << 30            # oldest undo steps are dropped beyond this (at least 2 states are always kept)
OPEN_TYPES = [("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif"), ("All files", "*.*")]
SAVE_TYPES = [("PNG", "*.png"), ("JPEG", "*.jpg"), ("TIFF", "*.tif"), ("BMP", "*.bmp"), ("WebP", "*.webp")]

# ---------------------------------------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------------------------------------
TOOLS = {}                          # name -> Tool, in registration order (which is the order of the tool list)


class Param:
    """One adjustable value of a tool. kind: 'int', 'float', 'bool', 'choice' (then `choices` lists the
    options and `default` is one of them), 'colors' (text with hex colours such as '#ff0000 #00ff00') or 'text'.
    `slider` adds a slider next to the number box. `pick` adds a button that fills the value by clicking on
    the image: 'color' (appends the colour clicked), 'color1' (a single colour, also with a colour dialog) or
    'angle' (two clicks on a line that should be level)."""

    def __init__(self, key, label, kind, default, lo=None, hi=None, step=1, choices=(), slider=False, pick=None):
        self.key, self.label, self.kind, self.default = key, label, kind, default
        self.lo, self.hi, self.step, self.choices, self.slider = lo, hi, step, tuple(choices), slider
        self.pick = pick

    @property
    def decimals(self):
        s = ("%f" % self.step).rstrip("0")
        return len(s.split(".")[1]) if "." in s else 0

    def coerce(self, value):
        """The value as the tool wants it: right type, inside [lo, hi]; an unusable value gives the default."""
        try:
            if self.kind == 'bool':
                return bool(value)
            if self.kind == 'choice':
                return value if value in self.choices else self.default
            if self.kind in ('colors', 'text'):
                return str(value)
            v = float(value)
            if not math.isfinite(v):
                return self.default
            if self.lo is not None:
                v = max(self.lo, v)
            if self.hi is not None:
                v = min(self.hi, v)
            return int(round(v)) if self.kind == 'int' else v
        except (TypeError, ValueError):
            return self.default


class Tool:
    """`geometry` tools change the whole image (size, orientation) and ignore the selection; the others are
    computed on the whole image and only the selected rectangle is taken from the result. An `interactive` tool
    ('brush') is not applied with the Apply button: the user paints on the image and the app does the work. A
    `place` tool has int parameters 'x' and 'y' that a click or drag on the image sets (where the thing goes)."""

    def __init__(self, name, group, func, params, geometry, preview, doc, interactive=None, place=False):
        self.name, self.group, self.func, self.params = name, group, func, tuple(params)
        self.geometry, self.preview, self.doc, self.interactive, self.place = geometry, preview, doc, interactive, place


def tool(name, group, params=(), geometry=False, preview=True, doc="", interactive=None, place=False):
    """Register `func(img, **params) -> img` as a tool. `img` is an (H, W, 3) uint8 array that must not be
    modified in place (it belongs to the Undo history); return a new array, or `img` itself for 'no change'.
    The keys of `params` are the keyword arguments."""
    def deco(func):
        TOOLS[name] = Tool(name, group, func, params, geometry, preview, doc or (func.__doc__ or "").strip(),
                           interactive, place)
        return func
    return deco


def apply_tool(name, img, params=None, region=None):
    """Run a registered tool. `region` = (x0, y0, x1, y1) limits a non-geometry tool to that rectangle."""
    t = TOOLS[name]
    values = {p.key: p.default for p in t.params}
    for p in t.params:
        if params and p.key in params:
            values[p.key] = p.coerce(params[p.key])
    out = t.func(img, **values)
    if region is not None and not t.geometry and out is not img:
        x0, y0, x1, y1 = region
        merged = img.copy()
        merged[y0:y1, x0:x1] = out[y0:y1, x0:x1]
        return merged
    return out


def load_plugins(folder=PLUGIN_DIR):
    """Execute every .py file of `folder`; each registers tools with `tool`. The names tool, Param, np, cv2 and
    Image are available in them without an import. Returns [(file name, error text or None)]: a broken plug-in is
    reported and skipped, it never stops the program."""
    report = []
    if not os.path.isdir(folder):
        return report
    for fn in sorted(os.listdir(folder)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        before = set(TOOLS)
        try:
            spec = importlib.util.spec_from_file_location("imageforge_plugin_" + fn[:-3], os.path.join(folder, fn))
            mod = importlib.util.module_from_spec(spec)
            mod.tool, mod.Param, mod.np, mod.cv2, mod.Image = tool, Param, np, cv2, Image
            spec.loader.exec_module(mod)
            report.append((fn, None))
        except Exception:
            for name in set(TOOLS) - before:        # a half-registered plug-in is dropped whole
                del TOOLS[name]
            report.append((fn, traceback.format_exc(limit=3)))
    return report


# ---------------------------------------------------------------------------------------------------------
# Built-in tools
# ---------------------------------------------------------------------------------------------------------
def _to_u8(f):
    return np.clip(np.rint(f), 0, 255).astype(np.uint8)


@tool("Brightness / contrast", "Tone", [
    Param('brightness', "Brightness", 'int', 0, -100, 100, slider=True),
    Param('contrast', "Contrast", 'int', 0, -100, 99, slider=True)])
def brightness_contrast(img, brightness, contrast):
    """Shift the brightness and stretch the contrast about mid-grey."""
    if brightness == 0 and contrast == 0:
        return img
    k = math.tan((contrast + 100) * math.pi / 400)          # 1 at 0, towards infinity at 100
    return _to_u8((img.astype(np.float32) - 128.0) * k + 128.0 + brightness * 2.55)


@tool("Grayscale", "Tone", [
    Param('method', "Method", 'choice', 'luminance', choices=('luminance', 'average')),
    Param('strength', "Strength (%)", 'int', 100, 0, 100, slider=True)])
def grayscale(img, method, strength):
    """Remove the colour (the image stays 3-channel). Strength below 100 only desaturates."""
    if strength == 0:
        return img
    if method == 'average':
        g = img.astype(np.float32).mean(axis=2)
    else:
        g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float32)
    f = img.astype(np.float32)
    s = strength / 100.0
    return _to_u8(f * (1 - s) + g[:, :, None] * s)


@tool("Invert", "Tone")
def invert(img):
    """Negative: every value v becomes 255 - v."""
    return 255 - img


@tool("Sharpen (unsharp mask)", "Filters", [
    Param('amount', "Amount (%)", 'int', 100, 0, 500, slider=True),
    Param('radius', "Radius (px)", 'float', 1.5, 0.1, 30.0, step=0.1, slider=True),
    Param('threshold', "Threshold", 'int', 0, 0, 255, slider=True)])
def sharpen(img, amount, radius, threshold):
    """Add `amount` of the difference between the image and a blurred copy; differences under `threshold` are
    left alone (keeps flat areas and noise untouched)."""
    if amount == 0:
        return img
    f = img.astype(np.float32)
    diff = f - cv2.GaussianBlur(f, (0, 0), radius)
    if threshold > 0:
        diff = np.where(np.abs(diff) >= threshold, diff, 0.0)
    return _to_u8(f + amount / 100.0 * diff)


@tool("Blur (Gaussian)", "Filters", [Param('radius', "Radius (px)", 'float', 2.0, 0.1, 50.0, step=0.1, slider=True)])
def blur(img, radius):
    """Gaussian blur."""
    return cv2.GaussianBlur(img, (0, 0), radius)


@tool("Rotate 90 / 180", "Geometry", [
    Param('turn', "Turn", 'choice', '90 clockwise', choices=('90 clockwise', '90 counter-clockwise', '180'))],
    geometry=True)
def rotate_quarter(img, turn):
    """Exact quarter turns: no resampling, no loss."""
    k = {'90 clockwise': -1, '90 counter-clockwise': 1, '180': 2}[turn]
    return np.ascontiguousarray(np.rot90(img, k))


@tool("Rotate (any angle)", "Geometry", [
    Param('angle', "Angle (deg, counter-clockwise)", 'float', 0.0, -180.0, 180.0, step=0.1, slider=True,
          pick='angle'),
    Param('expand', "Enlarge canvas to fit", 'bool', True),
    Param('fill', "Fill the corners with", 'choice', 'white', choices=('white', 'black', 'edge'))],
    geometry=True)
def rotate_free(img, angle, expand, fill):
    """Rotate about the centre (bicubic). With 'Enlarge canvas' nothing is cut off; the new corners are filled."""
    if angle == 0:
        return img
    h, w = img.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    nw, nh = w, h
    if expand:
        c, s = abs(m[0, 0]), abs(m[0, 1])
        nw, nh = int(math.ceil(h * s + w * c - 1e-6)), int(math.ceil(h * c + w * s - 1e-6))
        m[0, 2] += nw / 2.0 - cx
        m[1, 2] += nh / 2.0 - cy
    border = {'white': (cv2.BORDER_CONSTANT, (255, 255, 255)), 'black': (cv2.BORDER_CONSTANT, (0, 0, 0)),
              'edge': (cv2.BORDER_REPLICATE, 0)}[fill]
    return cv2.warpAffine(img, m, (nw, nh), flags=cv2.INTER_CUBIC, borderMode=border[0], borderValue=border[1])


@tool("Flip", "Geometry", [Param('axis', "Axis", 'choice', 'horizontal', choices=('horizontal', 'vertical'))],
      geometry=True)
def flip(img, axis):
    """Mirror the image left-right (horizontal) or top-bottom (vertical)."""
    return np.ascontiguousarray(img[:, ::-1] if axis == 'horizontal' else img[::-1])


INTERPOLATIONS = {'nearest': cv2.INTER_NEAREST, 'linear': cv2.INTER_LINEAR, 'cubic': cv2.INTER_CUBIC,
                  'lanczos': cv2.INTER_LANCZOS4, 'area': cv2.INTER_AREA}


@tool("Resize", "Geometry", [
    Param('percent', "Scale (%)", 'float', 100.0, 1.0, 1000.0, step=1.0, slider=True),
    Param('width', "Width (px, 0 = use scale)", 'int', 0, 0, 100000),
    Param('height', "Height (px, 0 = use scale)", 'int', 0, 0, 100000),
    Param('method', "Interpolation", 'choice', 'auto', choices=('auto',) + tuple(INTERPOLATIONS))],
    geometry=True, preview=False)
def resize(img, percent, width, height, method):
    """Scale by a percentage, or set the width and/or height in pixels (a 0 keeps the aspect ratio). 'auto' uses
    area averaging to shrink and bicubic to enlarge."""
    h, w = img.shape[:2]
    if width > 0 and height > 0:
        nw, nh = width, height
    elif width > 0:
        nw, nh = width, max(1, round(h * width / w))
    elif height > 0:
        nw, nh = max(1, round(w * height / h)), height
    else:
        nw, nh = max(1, round(w * percent / 100.0)), max(1, round(h * percent / 100.0))
    if (nw, nh) == (w, h):
        return img
    if method == 'auto':
        flag = cv2.INTER_AREA if nw * nh < w * h else cv2.INTER_CUBIC
    else:
        flag = INTERPOLATIONS[method]
    return cv2.resize(img, (nw, nh), interpolation=flag)


def straighten_angle(p1, p2):
    """Rotation angle (degrees, counter-clockwise, as 'Rotate (any angle)' wants it) that makes the line through the
    image points p1 and p2 level, or vertical when it is closer to vertical."""
    a = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))      # image y grows downward
    if a > 90:
        a -= 180
    elif a <= -90:
        a += 180
    if abs(a) > 45:
        a -= 90 if a > 0 else -90
    return a


# ---- clean-up tools (from the 'Clean image' tab of SpectrumDigitizer) -------------------------------------
def parse_colors(text):
    """[(r, g, b)] from a text holding '#rrggbb' codes separated by spaces or commas; anything else is ignored."""
    out = []
    for tok in str(text).replace(",", " ").split():
        t = tok.lstrip("#")
        if len(t) == 6:
            try:
                out.append(tuple(int(t[i:i + 2], 16) for i in (0, 2, 4)))
            except ValueError:
                pass
    return out


FILLS = {'white': (255, 255, 255), 'black': (0, 0, 0)}


def _distance_from(img, color):
    return np.sqrt(((img.astype(np.float32) - np.asarray(color, np.float32)) ** 2).sum(axis=2))


@tool("Erase / keep colours", "Clean", [
    Param('colors', "Colours (click Pick on the image)", 'colors', '', pick='color'),
    Param('mode', "Mode", 'choice', 'erase', choices=('erase', 'keep only')),
    Param('tolerance', "Tolerance (RGB distance)", 'int', 60, 1, 441, slider=True),
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS))])
def erase_colors(img, colors, mode, tolerance, fill):
    """Paint with the fill colour the pixels within the tolerance of any picked colour ('erase') or of none of them
    ('keep only'). Shades of one ink need several picks, or a larger tolerance."""
    cols = parse_colors(colors)
    if not cols:
        return img
    near = np.zeros(img.shape[:2], bool)
    for c in cols:
        near |= _distance_from(img, c) < tolerance
    hit = ~near if mode == 'keep only' else near
    if not hit.any():
        return img
    out = img.copy()
    out[hit] = FILLS[fill]
    return out


@tool("Despeckle", "Clean", [
    Param('min_size', "Remove specks under (px)", 'int', 6, 1, 500, slider=True),
    Param('tolerance', "Ink = further from background than", 'int', 60, 1, 441, slider=True),
    Param('background', "Background", 'choice', 'white', choices=tuple(FILLS))])
def despeckle(img, min_size, tolerance, background):
    """Remove isolated specks of a scan: connected groups (8-neighbours) of ink pixels smaller than the given number
    of pixels become background. A line is one big group and stays; raise the size to remove larger dust."""
    bg = FILLS[background]
    ink = (_distance_from(img, bg) > tolerance).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    small = stats[:, cv2.CC_STAT_AREA] < min_size
    small[0] = False
    hit = small[labels]
    if not hit.any():
        return img
    out = img.copy()
    out[hit] = bg
    return out


@tool("Flatten ink to one colour", "Clean", [
    Param('tolerance', "Ink = further from background than", 'int', 60, 1, 441, slider=True),
    Param('background', "Background", 'choice', 'white', choices=tuple(FILLS))])
def flatten_ink(img, tolerance, background):
    """Turn every pixel that is not background into pure black (on white) or white (on black): a line drawn in
    several shades becomes one colour. Paler pixels closer to the background than the tolerance are left alone."""
    bg = FILLS[background]
    ink_color = FILLS['black' if background == 'white' else 'white']
    hit = _distance_from(img, bg) > tolerance
    hit &= (img != np.asarray(ink_color, np.uint8)).any(axis=2)
    if not hit.any():
        return img
    out = img.copy()
    out[hit] = ink_color
    return out


@tool("Eraser brush", "Clean", [
    Param('size', "Diameter (px)", 'int', 15, 1, 300, slider=True),
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS))],
    preview=False, interactive='brush',
    doc="Drag on the image to paint over it (limited to the selection if there is one: draw the selection with another tool first). One stroke is one Undo step.")
def eraser_brush(img, size, fill):
    return img


def paint_segment(img, p0, p1, size, color, region=None):
    """Paint, in place, a round-ended stroke of diameter `size` from p0 to p1 (image pixels) on `img`; with a
    `region` (x0, y0, x1, y1) only the pixels inside it change. Works on the small box around the stroke."""
    h, w = img.shape[:2]
    r = int(size) // 2 + 2
    x0, y0 = max(0, min(p0[0], p1[0]) - r), max(0, min(p0[1], p1[1]) - r)
    x1, y1 = min(w, max(p0[0], p1[0]) + r + 1), min(h, max(p0[1], p1[1]) + r + 1)
    if x1 <= x0 or y1 <= y0:
        return
    sub = img[y0:y1, x0:x1]
    keep = sub.copy() if region is not None else None
    cv2.line(sub, (p0[0] - x0, p0[1] - y0), (p1[0] - x0, p1[1] - y0), tuple(int(c) for c in color),
             thickness=max(1, int(size)), lineType=cv2.LINE_8)
    if region is not None:
        rx0, ry0, rx1, ry1 = region
        inside = np.zeros(sub.shape[:2], bool)
        inside[max(0, ry0 - y0):max(0, ry1 - y0), max(0, rx0 - x0):max(0, rx1 - x0)] = True
        sub[~inside] = keep[~inside]


HANDLE_CURSORS = {'nw': 'size_nw_se', 'se': 'size_nw_se', 'ne': 'size_ne_sw', 'sw': 'size_ne_sw',
                  'n': 'size_ns', 's': 'size_ns', 'e': 'size_we', 'w': 'size_we', 'move': 'fleur'}


def handle_points(rect):
    """The eight handles of a selection rectangle, as {name: (x, y)} in the rectangle's own coordinates."""
    x0, y0, x1, y1 = rect
    xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return {'nw': (x0, y0), 'ne': (x1, y0), 'se': (x1, y1), 'sw': (x0, y1),
            'n': (xm, y0), 's': (xm, y1), 'e': (x1, ym), 'w': (x0, ym)}


def hit_handle(rect, zoom, ox, oy, sx, sy, tol=7):
    """What a click at screen point (sx, sy) grabs on the selection `rect` (image pixels, drawn at x*zoom + ox):
    a handle name (corners before edge midpoints), 'move' inside the rectangle, or None."""
    for name, (hx, hy) in handle_points(rect).items():
        if abs(hx * zoom + ox - sx) <= tol and abs(hy * zoom + oy - sy) <= tol:
            return name
    x0, y0, x1, y1 = rect
    if x0 * zoom + ox <= sx <= x1 * zoom + ox and y0 * zoom + oy <= sy <= y1 * zoom + oy:
        return 'move'
    return None


def drag_rect(rect, handle, dx, dy, bounds):
    """The selection after dragging `handle` by (dx, dy) image pixels. 'move' keeps the size and stops at the
    image border; an edge never crosses the opposite one (the rectangle stays at least 1 px) nor leaves the
    image. `bounds` = (width, height) of the image."""
    x0, y0, x1, y1 = rect
    w, h = bounds
    if handle == 'move':
        dx, dy = max(-x0, min(w - x1, dx)), max(-y0, min(h - y1, dy))
        return (x0 + dx, y0 + dy, x1 + dx, y1 + dy)
    if 'w' in handle:
        x0 = max(0, min(x0 + dx, x1 - 1))
    if 'e' in handle:
        x1 = min(w, max(x1 + dx, x0 + 1))
    if 'n' in handle:
        y0 = max(0, min(y0 + dy, y1 - 1))
    if 's' in handle:
        y1 = min(h, max(y1 + dy, y0 + 1))
    return (x0, y0, x1, y1)


HERMITE_TENSION = 0.6                # length of the end tangents of a join, in fractions of the gap


def join_dashes(img, background, tolerance, max_gap, max_turn):
    """Make dashed lines continuous; returns (new image, number of joins) or (img, 0). The ink (further from the
    background than `tolerance`) is split into connected groups; each group has two ends (the extremes along its main
    axis) and, at each end, the local direction the dash is heading (from the pixels within 8 px of the tip, so a dash
    bent over a peak keeps its bend). Two ends of different groups at most `max_gap` pixels apart that look at each
    other (the line between them leaves each end within `max_turn` degrees of its direction) are joined, best pairs
    first, each end used once and two groups joined at most once. The join is a smooth curve (cubic Hermite: it leaves
    each end along its direction), so a peak whose top fell in the gap is rounded, not cut flat. It has the colour (the
    most contrasted shades) and thickness of the dash. Dashes touching another line are one group with it: erase that
    line first."""
    bg = FILLS[background]
    dist = _distance_from(img, bg)
    n, lab, stats, _ = cv2.connectedComponentsWithStats((dist > tolerance).astype(np.uint8), connectivity=8)
    ends, look = [], []                              # ends: (group, end point, outward unit vector); look: (colour, width)
    for i in range(1, n):
        x0, y0, bw, bh, area = stats[i]
        if area < 4:
            continue
        ys, xs = np.nonzero(lab[y0:y0 + bh, x0:x0 + bw] == i)
        ys, xs = ys + y0, xs + x0
        pts = np.c_[xs, ys].astype(float)
        c = pts.mean(axis=0)
        axis = np.linalg.svd(pts - c, full_matrices=False)[2][0]
        t = (pts - c) @ axis
        g = len(look)
        for sign in (-1, 1):
            tip = t.max() if sign > 0 else t.min()
            sel = (t >= tip - 1.5) if sign > 0 else (t <= tip + 1.5)
            end = pts[sel].mean(axis=0)
            near = pts[np.hypot(*(pts - end).T) <= 8.0].mean(axis=0)
            d = end - near
            norm = float(np.hypot(*d))
            ends.append((g, end, d / norm if norm > 0.5 else sign * axis))
        shade = img[ys, xs]
        strong = shade[np.argsort(-dist[ys, xs])[:max(1, len(xs) // 3)]]
        span = max(np.ptp(xs), np.ptp(ys)) + 1
        look.append((tuple(int(v) for v in np.median(strong, axis=0)), max(1, int(round(len(xs) / span)))))
    if len(ends) < 2:
        return img, 0
    G = np.array([e[0] for e in ends])
    P = np.array([e[1] for e in ends])
    D = np.array([e[2] for e in ends])
    cos_min = math.cos(math.radians(max_turn))
    cand = []
    for s in range(0, len(ends), 256):               # in blocks: a page of text has thousands of ends
        a = np.arange(s, min(len(ends), s + 256))
        v = P[None, :, :] - P[a][:, None, :]
        d = np.hypot(v[..., 0], v[..., 1])
        ok = (d <= max_gap) & (G[None, :] != G[a][:, None]) & (np.arange(len(ends))[None, :] > a[:, None])
        ia, ib = np.nonzero(ok)
        if ia.size == 0:
            continue
        vv, dd = v[ia, ib], d[ia, ib]
        safe = np.maximum(dd, 1e-9)
        ca = np.where(dd > 1e-9, (vv * D[a[ia]]).sum(axis=1) / safe, 1.0)
        cb = np.where(dd > 1e-9, -(vv * D[ib]).sum(axis=1) / safe, 1.0)
        good = (ca >= cos_min) & (cb >= cos_min)
        for k in np.nonzero(good)[0]:
            # nearest first, but a dash straight ahead beats a nearer one off to the side (the other flank of a peak)
            cand.append((dd[k] * (3.0 - ca[k] - cb[k]), dd[k], int(a[ia[k]]), int(ib[k])))
    cand.sort()
    pim = Image.fromarray(img)
    draw = ImageDraw.Draw(pim)
    used, pairs, joins = set(), set(), 0
    for _, d, a, b in cand:
        ga, gb = ends[a][0], ends[b][0]
        if a in used or b in used or (min(ga, gb), max(ga, gb)) in pairs:
            continue
        used.update((a, b))
        pairs.add((min(ga, gb), max(ga, gb)))
        p0, p1 = ends[a][1], ends[b][1]
        m0, m1 = ends[a][2] * d * HERMITE_TENSION, -ends[b][2] * d * HERMITE_TENSION
        u = np.linspace(0.0, 1.0, max(int(d), 4) + 1)[:, None]
        curve = ((2 * u**3 - 3 * u**2 + 1) * p0 + (u**3 - 2 * u**2 + u) * m0
                 + (-2 * u**3 + 3 * u**2) * p1 + (u**3 - u**2) * m1)
        draw.line([tuple(q) for q in curve], fill=look[ga][0], width=max(look[ga][1], look[gb][1]), joint='curve')
        joins += 1
    return (np.asarray(pim), joins) if joins else (img, 0)


@tool("Join dashed lines", "Clean", [
    Param('max_gap', "Largest gap to join (px)", 'int', 15, 1, 200, slider=True),
    Param('max_turn', "Largest turn at a join (deg)", 'int', 50, 10, 89, slider=True),
    Param('tolerance', "Ink = further from background than", 'int', 60, 1, 441, slider=True),
    Param('background', "Background", 'choice', 'white', choices=tuple(FILLS))])
def join_dashed(img, max_gap, max_turn, tolerance, background):
    """Close the gaps of a dashed or dotted line with smooth strokes that follow the direction of the dashes. Only
    ends that face each other are joined, so neighbouring lines and text are mostly left alone; select the line
    first to keep other marks out of it. A dashed line that touches a continuous one is a single group: erase the
    continuous one first (the gap it leaves is joined too)."""
    return join_dashes(img, background, tolerance, max_gap, max_turn)[0]


# ---- text -------------------------------------------------------------------------------------------------
FONT_DIRS = [r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental"]
FONT_FILES = {'Arial': ('arial.ttf', 'Arial.ttf', 'DejaVuSans.ttf'), 'Arial Bold': ('arialbd.ttf', 'Arial Bold.ttf'),
              'Arial Italic': ('ariali.ttf', 'Arial Italic.ttf'), 'Times New Roman': ('times.ttf', 'Times New Roman.ttf'),
              'Courier New': ('cour.ttf', 'Courier New.ttf'), 'Calibri': ('calibri.ttf',), 'Segoe UI': ('segoeui.ttf',),
              'Verdana': ('verdana.ttf', 'Verdana.ttf'), 'Consolas': ('consola.ttf',)}


def find_font(name):
    """Path of the TrueType file of a font name from FONT_FILES, or None when this computer does not have it."""
    for d in FONT_DIRS:
        for fn in FONT_FILES.get(name, ()):
            path = os.path.join(d, fn)
            if os.path.isfile(path):
                return path
    return None


FONT_CHOICES = tuple(n for n in FONT_FILES if find_font(n)) + ('default',)


def load_font(name, size):
    path = find_font(name)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


@tool("Add text", "Draw", [
    Param('text', "Text (type \\n for a new line)", 'text', "Text"),
    Param('size', "Size (px)", 'int', 32, 4, 2000, slider=True),
    Param('font', "Font", 'choice', FONT_CHOICES[0], choices=FONT_CHOICES),
    Param('color', "Colour", 'colors', '#000000', pick='color1'),
    Param('x', "X of the top-left corner (px)", 'int', 20, -100000, 100000),
    Param('y', "Y of the top-left corner (px)", 'int', 20, -100000, 100000),
    Param('angle', "Rotate (deg, counter-clockwise)", 'float', 0.0, -180.0, 180.0, step=0.5, slider=True),
    Param('opacity', "Opacity (%)", 'int', 100, 0, 100, slider=True)],
    place=True,
    doc="Click or drag on the image to place the text, then Apply. Inside a selection the text is clipped to it.")
def add_text(img, text, size, font, color, x, y, angle, opacity):
    """Anti-aliased text in a font of this computer, optionally rotated and semi-transparent."""
    text = text.replace("\\n", "\n")                  # a typed backslash-n is a line break (the box is one line)
    if not text.strip() or opacity == 0:
        return img
    cols = parse_colors(color)
    rgb = np.asarray(cols[0] if cols else (0, 0, 0), np.float32)
    f = load_font(font, size)
    probe = ImageDraw.Draw(Image.new('L', (1, 1)))
    l, t, r, b = probe.multiline_textbbox((0, 0), text, font=f)
    pad = 1
    layer = Image.new('L', (r - l + 2 * pad, b - t + 2 * pad), 0)
    ImageDraw.Draw(layer).multiline_text((pad - l, pad - t), text, font=f, fill=255)
    if angle:
        layer = layer.rotate(angle, expand=True, resample=Image.BICUBIC)
    mask = np.asarray(layer, np.float32) / 255.0 * (opacity / 100.0)
    h, w = img.shape[:2]
    mh, mw = mask.shape
    dx0, dy0, dx1, dy1 = max(0, x), max(0, y), min(w, x + mw), min(h, y + mh)
    if dx1 <= dx0 or dy1 <= dy0:
        return img
    m = mask[dy0 - y:dy1 - y, dx0 - x:dx1 - x][..., None]
    out = img.copy()
    out[dy0:dy1, dx0:dx1] = _to_u8(out[dy0:dy1, dx0:dx1].astype(np.float32) * (1 - m) + rgb * m)
    return out


def move_region(img, rect, dx, dy, fill, copy=False):
    """Move (or copy) the pixels of `rect` by (dx, dy): returns (new image, new rect). The rectangle stops at the
    image border (nothing is cut off). A move leaves `fill` (r, g, b) where the object was; a copy leaves it as is."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = rect
    nx0, ny0, nx1, ny1 = drag_rect(rect, 'move', dx, dy, (w, h))
    if (nx0, ny0) == (x0, y0):
        return img, rect
    patch = img[y0:y1, x0:x1].copy()
    out = img.copy()
    if not copy:
        out[y0:y1, x0:x1] = fill
    out[ny0:ny1, nx0:nx1] = patch
    return out, (nx0, ny0, nx1, ny1)


def border_color(img):
    """The colour of the image frame: the per-channel median of its outermost pixel ring."""
    ring = np.concatenate([img[0], img[-1], img[:, 0], img[:, -1]])
    return tuple(int(v) for v in np.median(ring, axis=0))


@tool("Auto-crop borders", "Geometry", [
    Param('background', "Border colour", 'choice', 'auto', choices=('auto',) + tuple(FILLS)),
    Param('tolerance', "Content = further from it than", 'int', 30, 1, 441, slider=True),
    Param('margin', "Keep a margin (px)", 'int', 0, 0, 500)],
    geometry=True, preview=False)
def autocrop(img, background, tolerance, margin):
    """Cut off the uniform border around the content: the smallest rectangle holding every pixel that differs
    from the border colour ('auto' = the colour of the image frame) by more than the tolerance, plus a margin.
    A speck in the border widens it: run Despeckle first. An image that is all border is left alone."""
    bg = border_color(img) if background == 'auto' else FILLS[background]
    rows, cols = np.nonzero(_distance_from(img, bg) > tolerance)
    if rows.size == 0:
        return img
    h, w = img.shape[:2]
    region = (max(0, cols.min() - margin), max(0, rows.min() - margin),
              min(w, cols.max() + 1 + margin), min(h, rows.max() + 1 + margin))
    if region == (0, 0, w, h):
        return img
    return crop(img, region)


def crop(img, region):
    """The (x0, y0, x1, y1) rectangle of `img` as a new array."""
    x0, y0, x1, y1 = region
    return np.ascontiguousarray(img[y0:y1, x0:x1])


# ---------------------------------------------------------------------------------------------------------
# History and files
# ---------------------------------------------------------------------------------------------------------
class History:
    """Undo/Redo stack of (label, array). The arrays are never modified in place, so a state is just kept."""

    def __init__(self, img, limit_bytes=HISTORY_BYTES):
        self.states = [("Original", img)]
        self.index = 0
        self.limit = limit_bytes

    @property
    def current(self):
        return self.states[self.index][1]

    def push(self, label, img):
        del self.states[self.index + 1:]
        self.states.append((label, img))
        self.index += 1
        while len(self.states) > 2 and sum(a.nbytes for _, a in self.states) > self.limit:
            del self.states[0]
            self.index -= 1

    def can_undo(self):
        return self.index > 0

    def can_redo(self):
        return self.index < len(self.states) - 1

    def undo(self):
        if self.can_undo():
            self.index -= 1
        return self.current

    def redo(self):
        if self.can_redo():
            self.index += 1
        return self.current

    def undo_label(self):
        return self.states[self.index][0] if self.can_undo() else None

    def redo_label(self):
        return self.states[self.index + 1][0] if self.can_redo() else None


def load_image(path):
    """(RGB uint8 array, dpi or None). The EXIF orientation is applied; transparency is flattened onto white and
    16-bit / palette / grey / CMYK images become 8-bit RGB (the editor works on 8-bit RGB only)."""
    with Image.open(path) as im:
        im.load()
        dpi = im.info.get('dpi')
        im = ImageOps.exif_transpose(im)
        if im.mode in ('I;16', 'I;16L', 'I;16B', 'I'):
            a = np.asarray(im).astype(np.float32)
            top = 65535.0 if a.max() > 255 else 255.0
            im = Image.fromarray(np.clip(a / top * 255.0, 0, 255).astype(np.uint8))
        elif im.mode == 'F':
            a = np.asarray(im)
            im = Image.fromarray(np.clip(a * 255.0, 0, 255).astype(np.uint8))
        if im.mode in ('RGBA', 'LA', 'PA') or (im.mode == 'P' and 'transparency' in im.info):
            im = im.convert('RGBA')
            bg = Image.new('RGBA', im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im)
        arr = np.array(im.convert('RGB'))
    if dpi is not None:
        dpi = (float(dpi[0]), float(dpi[1]))
    return arr, dpi


def save_image(img, path, dpi=None, jpeg_quality=95):
    """Write `img` in the format of the extension (PNG, JPEG, TIFF, BMP, WebP)."""
    ext = os.path.splitext(path)[1].lower()
    im = Image.fromarray(img)
    kw = {}
    if dpi and ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff'):
        kw['dpi'] = dpi
    if ext in ('.jpg', '.jpeg'):
        kw.update(quality=jpeg_quality, subsampling=0)
    elif ext == '.png':
        kw['compress_level'] = 9
    elif ext in ('.tif', '.tiff'):
        kw['compression'] = 'tiff_lzw'
    elif ext == '.webp':
        kw['quality'] = jpeg_quality
    im.save(path, **kw)


# ---------------------------------------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------------------------------------
class ImageForge:
    ZOOM_MIN, ZOOM_MAX = 0.02, 64.0

    def __init__(self, root):
        self.root = root
        self.hist = None
        self.path = None                 # file the image came from
        self.save_path = None            # where Save writes (None until the first Save As: never the original)
        self.dpi = None
        self.sel = None                  # (x0, y0, x1, y1) in image pixels, or None
        self.preview = None              # array shown instead of the image while a tool's parameters are tuned
        self.tool = None
        self.pvars = {}
        self.zoom, self.ox, self.oy = 1.0, 0.0, 0.0
        self.dirty = False
        self._photo = None
        self._after = None
        self._press = None
        self._drag_sel = None
        self._pan = None
        self._params_ready = False
        self.mode = None                 # None, 'color' or 'angle': what a click on the image means (Esc cancels)
        self._pick_key = None            # the parameter a pick fills
        self._pick_pts = []
        self._mv = None                  # (press point, selection, copy?, original image) while moving the content
        self._placing = False            # a click/drag is setting the x, y of a 'place' tool
        self._grab = None                # (handle, press point in image pixels, selection at press) while dragging it
        self._stroke = None              # image being painted by the eraser brush, and the last brush point
        self._last = None
        root.title(APP_NAME)
        root.geometry("1250x820")
        self._build_menu()
        self._build_ui()
        self._bind_keys()
        self.plugin_report = load_plugins()
        self._fill_tool_tree()
        self._update_state()
        bad = [(f, e) for f, e in self.plugin_report if e]
        if bad:
            self._status("Plug-in %s did not load (see the console)." % bad[0][0])
            for f, e in bad:
                print("Plug-in %s:\n%s" % (f, e), file=sys.stderr)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- construction ----------------------------------------------------------------------------------
    def _build_menu(self):
        mb = tk.Menu(self.root)
        self.root.config(menu=mb)
        self.m_file, self.m_edit, self.m_view = tk.Menu(mb, tearoff=0), tk.Menu(mb, tearoff=0), tk.Menu(mb, tearoff=0)
        self.m_image = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="File", menu=self.m_file)
        mb.add_cascade(label="Edit", menu=self.m_edit)
        mb.add_cascade(label="View", menu=self.m_view)
        mb.add_cascade(label="Image", menu=self.m_image)
        f = self.m_file
        f.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_dialog)
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        f.add_command(label="Save As...", accelerator="Ctrl+Shift+S", command=self.save_as)
        f.add_separator()
        f.add_command(label="Exit", command=self.on_close)
        e = self.m_edit
        e.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        e.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        e.add_separator()
        e.add_command(label="Select all", accelerator="Ctrl+A", command=self.select_all)
        e.add_command(label="Deselect", accelerator="Ctrl+D", command=self.deselect)
        e.add_command(label="Crop to selection", accelerator="Ctrl+Shift+X", command=self.crop_to_selection)
        v = self.m_view
        v.add_command(label="Zoom in", accelerator="Ctrl++", command=lambda: self._zoom_step(1.25))
        v.add_command(label="Zoom out", accelerator="Ctrl+-", command=lambda: self._zoom_step(0.8))
        v.add_command(label="Fit to window", accelerator="Ctrl+0", command=self.fit_view)
        v.add_command(label="Actual size (100%)", accelerator="Ctrl+1", command=self.actual_size)

    def _build_ui(self):
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=5, bg='#bbbbbb')
        paned.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(paned, bg='#3a3a3a', highlightthickness=0, cursor='crosshair')
        paned.add(self.canvas, stretch='always')
        panel = tk.Frame(paned, bg='#f0f4f7', width=330)
        panel.pack_propagate(False)
        paned.add(panel, stretch='never')
        self.status = tk.Label(self.root, text="", anchor='w', bg='#fff3c4', font=('Arial', 9), relief=tk.GROOVE,
                               padx=6, pady=3)
        self.status.pack(fill=tk.X, side=tk.BOTTOM)
        self.info = tk.Label(self.root, text="", anchor='w', bg='#e8e8e8', font=('Consolas', 9), padx=6)
        self.info.pack(fill=tk.X, side=tk.BOTTOM)

        bg = '#f0f4f7'
        # Packed first, at the bottom: Apply must stay visible whatever the window height or the number of parameters.
        self.v_preview = tk.BooleanVar(value=True)
        row = tk.Frame(panel, bg=bg)
        row.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(2, 8))
        self.c_preview = tk.Checkbutton(row, text="Preview", variable=self.v_preview, bg=bg,
                                        command=self._params_changed)
        self.c_preview.pack(side=tk.LEFT)
        tk.Button(row, text="Reset", command=self.reset_params).pack(side=tk.LEFT, padx=6)
        self.b_apply = tk.Button(row, text="Apply", command=self.apply, bg='#2ecc71', fg='white',
                                 font=('Arial', 10, 'bold'))
        self.b_apply.pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=(10, 2))

        row = tk.Frame(panel, bg=bg)
        row.pack(fill=tk.X, padx=8, pady=(8, 2))
        self.b_undo = tk.Button(row, text="Undo", command=self.undo)
        self.b_undo.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.b_redo = tk.Button(row, text="Redo", command=self.redo)
        self.b_redo.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.b_crop = tk.Button(row, text="Crop to selection", command=self.crop_to_selection)
        self.b_crop.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        row = tk.Frame(panel, bg=bg)
        row.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.v_content = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="Drag moves the content", variable=self.v_content, bg=bg).pack(side=tk.LEFT)
        tk.Label(row, text="leaves", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(6, 2))
        self.v_fill = tk.StringVar(value='white')
        ttk.Combobox(row, textvariable=self.v_fill, values=('white', 'black', 'background'), state='readonly',
                     width=10).pack(side=tk.LEFT)
        tk.Label(panel, text="Inside the selection: Ctrl does the opposite of the box, Shift copies.", bg=bg,
                 fg='#555555', font=('Arial', 8), anchor='w', justify='left', wraplength=300).pack(fill=tk.X, padx=10)

        tk.Label(panel, text="TOOLS", font=('Arial', 10, 'bold'), bg=bg, anchor='w').pack(fill=tk.X, padx=10, pady=(8, 0))
        tf = tk.Frame(panel, bg=bg)
        tf.pack(fill=tk.X, padx=8, pady=2)
        self.tree = ttk.Treeview(tf, show='tree', selectmode='browse', height=9)
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._on_tree_select())

        self.l_doc = tk.Label(panel, text="", bg=bg, fg='#555555', font=('Arial', 8), wraplength=300, justify='left',
                              anchor='w')
        self.l_doc.pack(fill=tk.X, padx=10, pady=(4, 0))
        self.params_frame = tk.Frame(panel, bg=bg)
        self.params_frame.pack(fill=tk.X, padx=8, pady=2)
        c = self.canvas
        c.bind('<Configure>', lambda e: self._redraw())
        c.bind('<ButtonPress-1>', self._on_press)
        c.bind('<B1-Motion>', self._on_drag)
        c.bind('<ButtonRelease-1>', self._on_release)
        for b in (2, 3):
            c.bind('<ButtonPress-%d>' % b, self._on_pan_start)
            c.bind('<B%d-Motion>' % b, self._on_pan)
        c.bind('<MouseWheel>', self._on_wheel)
        c.bind('<ButtonPress-1>', lambda e: c.focus_set(), add='+')      # arrows nudge the selection once the canvas has focus
        for key, (dx, dy) in {'Left': (-1, 0), 'Right': (1, 0), 'Up': (0, -1), 'Down': (0, 1)}.items():
            c.bind('<%s>' % key, lambda e, dx=dx, dy=dy: self.nudge(dx, dy))
            c.bind('<Shift-%s>' % key, lambda e, dx=dx, dy=dy: self.nudge(dx * 10, dy * 10))
        c.bind('<Motion>', self._on_motion)
        c.bind('<Leave>', lambda e: (self._set_info(""), c.delete('brush')))
        if HAS_DND:
            for w in (self.canvas, panel):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self._on_drop)

    def _bind_keys(self):
        r = self.root
        for seq, fn in (("<Control-o>", self.open_dialog), ("<Control-s>", self.save),
                        ("<Control-S>", self.save_as), ("<Control-z>", self.undo), ("<Control-y>", self.redo),
                        ("<Control-a>", self.select_all), ("<Control-d>", self.deselect),
                        ("<Control-X>", self.crop_to_selection), ("<Control-0>", self.fit_view),
                        ("<Control-1>", self.actual_size), ("<Control-plus>", lambda: self._zoom_step(1.25)),
                        ("<Control-equal>", lambda: self._zoom_step(1.25)),
                        ("<Control-minus>", lambda: self._zoom_step(0.8)),
                        ("<Control-Return>", self.apply), ("<Escape>", self.escape)):
            r.bind(seq, lambda e, fn=fn: self._key(fn, e))

    def _key(self, fn, event):
        """Keyboard shortcuts must not steal Ctrl+A / Ctrl+Z from a number box being edited."""
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox)) and \
                event.keysym.lower() in ('a', 'z', 'y', 'd'):
            return None
        fn()
        return "break"

    def _fill_tool_tree(self):
        """Fill the tool list (built-ins and plug-ins) and the Image menu, grouped."""
        self.tree.delete(*self.tree.get_children())
        self.m_image.delete(0, tk.END)
        groups = {}
        for t in TOOLS.values():
            groups.setdefault(t.group, []).append(t)
        for g, ts in groups.items():
            self.tree.insert('', tk.END, iid='g:' + g, text=g, open=True)
            sub = tk.Menu(self.m_image, tearoff=0)
            self.m_image.add_cascade(label=g, menu=sub)
            for t in ts:
                self.tree.insert('g:' + g, tk.END, iid=t.name, text=t.name)
                sub.add_command(label=t.name, command=lambda n=t.name: self.tree.selection_set(n))

    # ---- tools and parameters --------------------------------------------------------------------------
    def _on_tree_select(self):
        sel = self.tree.selection()
        if sel and sel[0] in TOOLS:
            self.select_tool(sel[0])

    def select_tool(self, name):
        self.cancel_mode()
        self.tool = TOOLS[name]
        self.canvas.delete('brush')
        self.l_doc.config(text=self.tool.doc)
        self._build_params()
        self.preview = None
        self._params_changed()

    def _build_params(self):
        self._params_ready = False
        for w in self.params_frame.winfo_children():
            w.destroy()
        self.pvars = {}
        bg = '#f0f4f7'
        for p in self.tool.params:
            if p.kind == 'bool':
                var = tk.BooleanVar(value=p.default)
                tk.Checkbutton(self.params_frame, text=p.label, variable=var, bg=bg, anchor='w').pack(fill=tk.X)
            elif p.kind == 'choice':
                var = tk.StringVar(value=p.default)
                tk.Label(self.params_frame, text=p.label, bg=bg, anchor='w', font=('Arial', 9)).pack(fill=tk.X)
                ttk.Combobox(self.params_frame, textvariable=var, values=p.choices, state='readonly').pack(fill=tk.X)
            elif p.kind in ('colors', 'text'):
                var = tk.StringVar(value=p.default)
                tk.Label(self.params_frame, text=p.label, bg=bg, anchor='w', font=('Arial', 9)).pack(fill=tk.X)
                r = tk.Frame(self.params_frame, bg=bg)
                r.pack(fill=tk.X)
                ttk.Entry(r, textvariable=var).pack(side=tk.LEFT, expand=True, fill=tk.X)
                if p.pick == 'color1':
                    tk.Button(r, text="...", command=lambda k=p.key: self.choose_color(k)).pack(side=tk.RIGHT, padx=(4, 0))
                    tk.Button(r, text="Pick", command=lambda k=p.key: self.start_pick('color1', k)).pack(side=tk.RIGHT)
                elif p.pick == 'color':
                    tk.Button(r, text="Clear", command=lambda v=var: v.set("")).pack(side=tk.RIGHT, padx=(4, 0))
                    tk.Button(r, text="Pick", command=lambda k=p.key: self.start_pick('color', k)).pack(side=tk.RIGHT)
            else:
                var = tk.DoubleVar(value=p.default)
                tk.Label(self.params_frame, text=p.label, bg=bg, anchor='w', font=('Arial', 9)).pack(fill=tk.X)
                r = tk.Frame(self.params_frame, bg=bg)
                r.pack(fill=tk.X)
                lo = p.lo if p.lo is not None else -1e9
                hi = p.hi if p.hi is not None else 1e9
                if p.slider and p.lo is not None and p.hi is not None:
                    ttk.Scale(r, from_=lo, to=hi, variable=var, orient='horizontal',
                              command=lambda v, p=p, var=var: var.set(p.coerce(round(float(v), p.decimals)))
                              ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))
                ttk.Spinbox(r, from_=lo, to=hi, increment=p.step, textvariable=var, width=8,
                            format='%.' + str(p.decimals) + 'f').pack(side=tk.RIGHT)
                if p.pick == 'angle':
                    tk.Button(self.params_frame, text="Measure: click two points of a line to level",
                              command=lambda k=p.key: self.start_pick('angle', k)).pack(fill=tk.X, pady=(2, 0))
            var.trace_add('write', lambda *a: self._params_changed())
            self.pvars[p.key] = var
        self._params_ready = True

    def _read_params(self):
        """Current parameter values, or None while a number box holds something unreadable (mid-typing)."""
        out = {}
        for p in self.tool.params:
            try:
                out[p.key] = p.coerce(self.pvars[p.key].get())
            except tk.TclError:
                return None
        return out

    def reset_params(self):
        if self.tool is None:
            return
        for p in self.tool.params:
            self.pvars[p.key].set(p.default)

    def _params_changed(self):
        if not self._params_ready or self.tool is None:
            return
        if self._after is not None:
            self.root.after_cancel(self._after)
        self._after = self.root.after(60, self._update_preview)

    def _update_preview(self):
        self._after = None
        if self.hist is None or self.tool is None or self._stroke is not None or self._mv is not None:
            return
        if not (self.v_preview.get() and self.tool.preview):
            if self.preview is not None:
                self.preview = None
                self._redraw()
            return
        params = self._read_params()
        if params is None:
            return
        try:
            out = apply_tool(self.tool.name, self.hist.current, params, None if self.tool.geometry else self.sel)
        except Exception as ex:
            self._status("%s: %s" % (self.tool.name, ex))
            return
        self.preview = None if out is self.hist.current else out
        self._redraw()

    def apply(self):
        if not self._need_image() or self.tool is None:
            if self.tool is None and self.hist is not None:
                self._status("Pick a tool in the list first.")
            return
        if self.tool.interactive:
            self._status("%s works by dragging on the image: there is nothing to apply." % self.tool.name)
            return
        params = self._read_params()
        if params is None:
            self._status("A parameter is not a valid number.")
            return
        cur = self.hist.current
        region = None if self.tool.geometry else self.sel
        try:
            out = apply_tool(self.tool.name, cur, params, region)
        except Exception as ex:
            self._status("%s failed: %s" % (self.tool.name, ex))
            return
        if out is cur or (out.shape == cur.shape and np.array_equal(out, cur)):
            self.preview = None
            self._redraw()
            self._status("%s: no change with these parameters." % self.tool.name)
            return
        self._commit(self.tool.name, out)
        extra = " (applied to the whole image: this tool ignores the selection)" if self.sel and self.tool.geometry else ""
        self._status("%s applied%s." % (self.tool.name, extra), ok=True)

    def _commit(self, label, img):
        """Put a new image on the history. Size changes drop the selection and refit the view."""
        resized = img.shape[:2] != self.hist.current.shape[:2]
        self.hist.push(label, img)
        self.preview = None
        self.dirty = True
        if resized:
            self.sel = None
            self.fit_view()
        self._update_state()
        self._redraw()

    # ---- files -----------------------------------------------------------------------------------------
    def _confirm_discard(self):
        if self.hist is None or not self.dirty:
            return True
        ans = messagebox.askyesnocancel(APP_NAME, "The image has unsaved changes. Save them?")
        if ans is None:
            return False
        if ans:
            self.save()
            return not self.dirty
        return True

    def open_dialog(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=OPEN_TYPES)
        if path:
            self.open_path(path)

    def open_path(self, path):
        if not self._confirm_discard():
            return False
        try:
            img, dpi = load_image(path)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot open %s:\n%s" % (path, ex))
            return False
        self.hist, self.path, self.save_path, self.dpi = History(img), path, None, dpi
        self.sel, self.preview, self.dirty = None, None, False
        self.root.update_idletasks()
        self.fit_view()
        self._update_state()
        self._redraw()
        self._status("Opened %s." % os.path.basename(path), ok=True)
        return True

    def _on_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        if files:
            self.open_path(files[0])

    def save(self):
        if not self._need_image():
            return
        if self.save_path is None:
            return self.save_as()
        self._write(self.save_path)

    def save_as(self):
        if not self._need_image():
            return
        base = os.path.splitext(os.path.basename(self.path or "image"))[0]
        path = filedialog.asksaveasfilename(parent=self.root, filetypes=SAVE_TYPES, defaultextension=".png",
                                            initialfile=base + "_edited.png")
        if path:
            self._write(path)

    def _write(self, path):
        try:
            save_image(self.hist.current, path, self.dpi)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot save %s:\n%s" % (path, ex))
            return
        self.save_path, self.dirty = path, False
        self._update_state()
        self._status("Saved %s." % path, ok=True)

    def on_close(self):
        if self._confirm_discard():
            self.root.destroy()

    # ---- history / selection ---------------------------------------------------------------------------
    def undo(self):
        if self.hist is not None and self.hist.can_undo():
            self._step(self.hist.undo, "Undone")

    def redo(self):
        if self.hist is not None and self.hist.can_redo():
            self._step(self.hist.redo, "Redone")

    def _step(self, fn, word):
        before = self.hist.current.shape[:2]
        label = self.hist.undo_label() if fn == self.hist.undo else self.hist.redo_label()
        fn()
        self.preview, self.dirty = None, True
        if self.hist.current.shape[:2] != before:
            self.sel = None
            self.fit_view()
        self._update_state()
        self._redraw()
        self._status("%s: %s." % (word, label), ok=True)

    def select_all(self):
        if self._need_image():
            h, w = self.hist.current.shape[:2]
            self.sel = (0, 0, w, h)
            self._after_selection()

    def escape(self):
        """Esc: end a pick in progress, else drop the selection."""
        if self.mode:
            self.cancel_mode()
            self._status("Cancelled.")
        else:
            self.deselect()

    def deselect(self):
        if self.sel is not None:
            self.sel = None
            self._after_selection()

    def _after_selection(self):
        self._draw_selection()
        self._update_state()
        self._params_changed()

    def crop_to_selection(self):
        if not self._need_image():
            return
        if self.sel is None:
            self._status("Drag a rectangle on the image first.")
            return
        self._commit("Crop", crop(self.hist.current, self.sel))

    # ---- canvas ----------------------------------------------------------------------------------------
    def _shown(self):
        return self.preview if self.preview is not None else (self.hist.current if self.hist else None)

    def fit_view(self):
        if self.hist is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        h, w = self._shown().shape[:2]
        self.zoom = min(max(cw - 20, 10) / w, max(ch - 20, 10) / h)
        self.ox, self.oy = (cw - w * self.zoom) / 2.0, (ch - h * self.zoom) / 2.0
        self._redraw()

    def actual_size(self):
        self._zoom_at(1.0 / self.zoom, self.canvas.winfo_width() / 2.0, self.canvas.winfo_height() / 2.0)

    def _zoom_at(self, factor, sx, sy):
        if self.hist is None:
            return
        z = min(max(self.zoom * factor, self.ZOOM_MIN), self.ZOOM_MAX)
        f = z / self.zoom
        self.ox, self.oy = sx - (sx - self.ox) * f, sy - (sy - self.oy) * f
        self.zoom = z
        self._redraw()

    def _zoom_step(self, factor):
        self._zoom_at(factor, self.canvas.winfo_width() / 2.0, self.canvas.winfo_height() / 2.0)

    def _redraw(self):
        c = self.canvas
        c.delete('img')
        img = self._shown()
        if img is None:
            c.delete('hint')
            c.create_text(c.winfo_width() / 2, c.winfo_height() / 2, tags='hint', fill='#bbbbbb', font=('Arial', 13),
                          text="Open an image (Ctrl+O) or drop one here")
            return
        c.delete('hint')
        cw, ch = c.winfo_width(), c.winfo_height()
        h, w = img.shape[:2]
        z = self.zoom
        x0, y0 = max(0, int(math.floor(-self.ox / z))), max(0, int(math.floor(-self.oy / z)))
        x1, y1 = min(w, int(math.ceil((cw - self.ox) / z))), min(h, int(math.ceil((ch - self.oy) / z)))
        if x1 > x0 and y1 > y0:
            sub = Image.fromarray(np.ascontiguousarray(img[y0:y1, x0:x1]))
            size = (max(1, round((x1 - x0) * z)), max(1, round((y1 - y0) * z)))
            if size != sub.size:
                sub = sub.resize(size, Image.NEAREST if z >= 1 else Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(sub)
            c.create_image(round(x0 * z + self.ox), round(y0 * z + self.oy), anchor='nw', image=self._photo, tags='img')
            c.tag_lower('img')
        self._draw_selection()
        self._set_info()

    def _draw_selection(self, rect=None):
        c = self.canvas
        c.delete('sel')
        rect = rect or self.sel
        if rect is None or self.hist is None:
            return
        x0, y0, x1, y1 = rect
        z = self.zoom
        pts = (x0 * z + self.ox, y0 * z + self.oy, x1 * z + self.ox, y1 * z + self.oy)
        c.create_rectangle(*pts, outline='white', tags='sel')
        c.create_rectangle(*pts, outline='black', dash=(4, 4), tags='sel')
        if rect is self.sel:                      # handles only on a settled selection, not on a new drag
            for hx, hy in handle_points(rect).values():
                sx, sy = hx * z + self.ox, hy * z + self.oy
                c.create_rectangle(sx - 4, sy - 4, sx + 4, sy + 4, fill='white', outline='black', tags='sel')

    def _img_xy(self, sx, sy):
        return (sx - self.ox) / self.zoom, (sy - self.oy) / self.zoom

    def _rect_from(self, a, b):
        """Image-pixel rectangle covering both screen points, clamped to the image; None when empty."""
        h, w = self.hist.current.shape[:2]
        (xa, ya), (xb, yb) = self._img_xy(*a), self._img_xy(*b)
        x0, x1 = int(math.floor(min(xa, xb))), int(math.ceil(max(xa, xb)))
        y0, y1 = int(math.floor(min(ya, yb))), int(math.ceil(max(ya, yb)))
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None

    # ---- picks (colour, angle) and the eraser brush ----------------------------------------------------
    def start_pick(self, what, key):
        if not self._need_image():
            return
        self.cancel_mode()
        self.mode, self._pick_key, self._pick_pts = what, key, []
        self.canvas.config(cursor='tcross')
        self._status({'color': "Click a colour on the image (as many as needed), Esc to finish.",
                      'color1': "Click the colour to use on the image. Esc cancels.",
                      'angle': "Click two points of a line that should be level (or vertical). Esc cancels."}[what])

    def choose_color(self, key):
        var = self.pvars[key]
        first = parse_colors(var.get())
        start = "#%02x%02x%02x" % first[0] if first else "#000000"
        _, hexv = colorchooser.askcolor(color=start, parent=self.root)
        if hexv:
            var.set(hexv)

    def cancel_mode(self):
        self.mode, self._pick_pts = None, []
        self.canvas.delete('pick')
        self.canvas.config(cursor='crosshair')

    def _pick_click(self, e):
        x, y = self._img_xy(e.x, e.y)
        img = self.hist.current
        h, w = img.shape[:2]
        ix, iy = int(math.floor(x)), int(math.floor(y))
        if not (0 <= ix < w and 0 <= iy < h):
            return
        if self.mode in ('color', 'color1'):
            patch = img[max(0, iy - 1):iy + 2, max(0, ix - 1):ix + 2].reshape(-1, 3)
            r, g, b = (int(v) for v in np.median(patch, axis=0))             # median of 3x3: robust to a stray pixel
            var = self.pvars[self._pick_key]
            if self.mode == 'color1':
                var.set("#%02x%02x%02x" % (r, g, b))
                self.cancel_mode()
                self._status("Colour #%02x%02x%02x set." % (r, g, b), ok=True)
            else:
                var.set((var.get().strip() + " #%02x%02x%02x" % (r, g, b)).strip())
                self._status("Added #%02x%02x%02x. Click more colours, or Esc to finish." % (r, g, b), ok=True)
        else:
            self._pick_pts.append((x, y))
            sx, sy = x * self.zoom + self.ox, y * self.zoom + self.oy
            self.canvas.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, outline='yellow', width=2, tags='pick')
            if len(self._pick_pts) == 2:
                (x1, y1), (x2, y2) = self._pick_pts
                if math.hypot(x2 - x1, y2 - y1) < 3:
                    self._pick_pts = self._pick_pts[:1]
                    self._status("The two points are too close: click the second further away.")
                    return
                a = straighten_angle(self._pick_pts[0], self._pick_pts[1])
                self.pvars[self._pick_key].set(round(a, 2))
                self.cancel_mode()
                self._status("Angle %.2f deg set; check the preview and press Apply." % a, ok=True)

    def _is_brush(self):
        return self.tool is not None and self.tool.interactive == 'brush'

    def _brush_params(self):
        p = self._read_params() or {}
        return int(p.get('size', 15)), FILLS[p.get('fill', 'white')]

    def _brush_start(self, e):
        self._stroke = self.hist.current.copy()
        self._last = None
        self._brush_to(e)

    def _brush_to(self, e):
        size, color = self._brush_params()
        x, y = self._img_xy(e.x, e.y)
        pt = (int(math.floor(x)), int(math.floor(y)))
        paint_segment(self._stroke, self._last or pt, pt, size, color, self.sel)
        self._last = pt
        self.preview = self._stroke
        self._redraw()
        self._draw_brush_circle(e)

    def _brush_end(self):
        stroke, self._stroke, self._last = self._stroke, None, None
        self.preview = None
        if not np.array_equal(stroke, self.hist.current):
            self._commit(self.tool.name, stroke)
        else:
            self._redraw()

    def _draw_brush_circle(self, e):
        self.canvas.delete('brush')
        if self._is_brush() and self.hist is not None:
            r = self._brush_params()[0] / 2.0 * self.zoom
            self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline='#ff3366', tags='brush')

    def _on_press(self, e):
        if self.hist is None:
            self._press = None
            return
        if self.mode:
            self._press = None
            self._pick_click(e)
        elif self._is_brush():
            self._press = None
            self._brush_start(e)
        elif self.tool is not None and self.tool.place and 'x' in self.pvars:
            self._press, self._placing = None, True
            self._place_at(e)
        else:
            grab = hit_handle(self.sel, self.zoom, self.ox, self.oy, e.x, e.y) if self.sel else None
            if grab == 'move' and self._moves_content(e):
                self._press = None
                self._mv = (self._img_xy(e.x, e.y), self.sel, bool(e.state & 0x1), self.hist.current)
            elif grab:
                self._press = None
                self._grab = (grab, self._img_xy(e.x, e.y), self.sel)
            else:
                self._press, self._drag_sel = (e.x, e.y), None

    def _place_at(self, e):
        """A 'place' tool (text): the pixel under the pointer becomes its x, y."""
        x, y = self._img_xy(e.x, e.y)
        self.pvars['x'].set(int(math.floor(x)))
        self.pvars['y'].set(int(math.floor(y)))

    def _moves_content(self, e):
        return self.v_content.get() != bool(e.state & 0x4)            # the box, flipped by Ctrl

    def _fill_color(self, img):
        f = self.v_fill.get()
        return border_color(img) if f == 'background' else FILLS[f]

    def _mv_to(self, e):
        """Show the selected pixels moved by the distance dragged so far (one history step is made on release)."""
        (x, y), rect, copy, orig = self._mv
        nx, ny = self._img_xy(e.x, e.y)
        out, new = move_region(orig, rect, int(round(nx - x)), int(round(ny - y)), self._fill_color(orig), copy)
        self.preview = None if out is orig else out
        self.sel = new
        self._redraw()
        x0, y0, x1, y1 = new
        self._set_info("%s %d x %d  at (%d, %d)" % ("copy" if copy else "move", x1 - x0, y1 - y0, x0, y0))

    def _mv_end(self):
        _, rect, copy, orig = self._mv
        self._mv = None
        out, self.preview = self.preview, None
        if out is not None:
            self._commit("Copy content" if copy else "Move content", out)
        self._after_selection()
        self._redraw()

    def _grab_to(self, e):
        """Move or resize the selection by the distance dragged since the press."""
        handle, (x, y), start = self._grab
        nx, ny = self._img_xy(e.x, e.y)
        h, w = self.hist.current.shape[:2]
        self.sel = drag_rect(start, handle, int(round(nx - x)), int(round(ny - y)), (w, h))
        self._draw_selection()
        x0, y0, x1, y1 = self.sel
        self._set_info("selection %d x %d  at (%d, %d)" % (x1 - x0, y1 - y0, x0, y0))

    def nudge(self, dx, dy):
        if self.sel and self.hist is not None and not self.mode and not self._is_brush():
            h, w = self.hist.current.shape[:2]
            self.sel = drag_rect(self.sel, 'move', dx, dy, (w, h))
            self._after_selection()

    def _update_cursor(self, e):
        if self.mode or self._is_brush():
            return
        if self.tool is not None and self.tool.place:
            self.canvas.config(cursor='crosshair')
            return
        grab = hit_handle(self.sel, self.zoom, self.ox, self.oy, e.x, e.y) if self.sel else None
        cursor = HANDLE_CURSORS[grab] if grab else 'crosshair'
        if grab == 'move' and self._moves_content(e):
            cursor = 'hand2'
        self.canvas.config(cursor=cursor)

    def _on_drag(self, e):
        if self._stroke is not None:
            self._brush_to(e)
            return
        if self._placing:
            self._place_at(e)
            return
        if self._mv is not None:
            self._mv_to(e)
            return
        if self._grab is not None:
            self._grab_to(e)
            return
        if self._press is None or (self._drag_sel is None and math.hypot(e.x - self._press[0], e.y - self._press[1]) < 4):
            return
        self._drag_sel = self._rect_from(self._press, (e.x, e.y))
        self._draw_selection(self._drag_sel)
        if self._drag_sel:
            x0, y0, x1, y1 = self._drag_sel
            self._set_info("selection %d x %d  at (%d, %d)" % (x1 - x0, y1 - y0, x0, y0))

    def _on_release(self, e):
        if self._stroke is not None:
            self._brush_end()
            return
        if self._placing:
            self._placing = False
            return
        if self._mv is not None:
            self._mv_end()
            return
        if self._grab is not None:
            self._grab = None
            self._after_selection()
            return
        if self._press is None:
            return
        dragged = math.hypot(e.x - self._press[0], e.y - self._press[1]) >= 4
        self.sel = self._drag_sel if dragged else None          # a plain click clears the selection
        self._press = self._drag_sel = None
        self._after_selection()

    def _on_pan_start(self, e):
        self._pan = (e.x, e.y, self.ox, self.oy)

    def _on_pan(self, e):
        if self._pan and self.hist is not None:
            self.ox, self.oy = self._pan[2] + e.x - self._pan[0], self._pan[3] + e.y - self._pan[1]
            self._redraw()

    def _on_wheel(self, e):
        self._zoom_at(1.25 ** max(-4.0, min(4.0, e.delta / 120.0)), e.x, e.y)

    def _on_motion(self, e):
        self._draw_brush_circle(e)
        if self.hist is None or self._press is not None:
            return
        self._update_cursor(e)
        x, y = self._img_xy(e.x, e.y)
        ix, iy = int(math.floor(x)), int(math.floor(y))
        h, w = self.hist.current.shape[:2]
        if 0 <= ix < w and 0 <= iy < h:
            r, g, b = self.hist.current[iy, ix]
            self._set_info("x=%d y=%d   RGB=(%d, %d, %d)" % (ix, iy, r, g, b))
        else:
            self._set_info()

    # ---- status ----------------------------------------------------------------------------------------
    def _need_image(self):
        if self.hist is None:
            self._status("Open an image first (Ctrl+O).")
            return False
        return True

    def _status(self, text, ok=False):
        self.status.config(text=text, bg='#d4edda' if ok else '#fff3c4')

    def _set_info(self, text=None):
        if text is None:
            if self.hist is None:
                text = ""
            else:
                h, w = self._shown().shape[:2]
                text = "%d x %d px   zoom %d%%" % (w, h, round(self.zoom * 100))
                if self.sel:
                    x0, y0, x1, y1 = self.sel
                    text += "   selection %d x %d" % (x1 - x0, y1 - y0)
        self.info.config(text=text)

    def _update_state(self):
        h = self.hist
        self.b_undo.config(state=tk.NORMAL if h and h.can_undo() else tk.DISABLED)
        self.b_redo.config(state=tk.NORMAL if h and h.can_redo() else tk.DISABLED)
        self.b_crop.config(state=tk.NORMAL if h and self.sel else tk.DISABLED)
        self.m_edit.entryconfig(0, label="Undo" + (" " + h.undo_label() if h and h.can_undo() else ""),
                                state=tk.NORMAL if h and h.can_undo() else tk.DISABLED)
        self.m_edit.entryconfig(1, label="Redo" + (" " + h.redo_label() if h and h.can_redo() else ""),
                                state=tk.NORMAL if h and h.can_redo() else tk.DISABLED)
        name = os.path.basename(self.save_path or self.path) if self.path else ""
        self.root.title("%s%s - %s" % (name, "*" if self.dirty else "", APP_NAME) if name else APP_NAME)
        self._set_info()


def main():
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = ImageForge(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(100, lambda: app.open_path(sys.argv[1]))
    root.mainloop()


if __name__ == '__main__':
    main()

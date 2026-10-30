"""ImageForge - a general-purpose image editor whose tools are small plug-in functions.

Open an image, optionally drag a rectangle to limit an operation to it, pick a tool in the list, tune its
parameters (the result is previewed live) and press Apply. Every step goes on an Undo/Redo history.

A tool is a function registered with the `@tool` decorator that takes an RGB uint8 array and returns one.
The built-in ones are below; more can be dropped as .py files into the `plugins` folder next to this file
(see plugins/example_threshold.py) without touching this file.

The core (registry, tools, history, load/save) does not need a display, so the tests can import it.
"""
import dataclasses
import functools
import importlib.util
import io
import json
import math
import os
import sys
import traceback
import zipfile

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
IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif"), ("All files", "*.*")]
OPEN_TYPES = [("Images and projects", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif *.ifl"), ("All files", "*.*")]
SAVE_TYPES = [("PNG", "*.png"), ("JPEG", "*.jpg"), ("TIFF", "*.tif"), ("BMP", "*.bmp"), ("WebP", "*.webp")]
PROJECT_TYPE = ("ImageForge project (layers kept)", "*.ifl")

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
    `place` tool has int parameters 'x' and 'y' that a click or drag on the image sets (where the thing goes). A tool
    with `layer` ('text') makes (or, if one is selected, edits) a Layer of that kind instead of changing the pixels."""

    def __init__(self, name, group, func, params, geometry, preview, doc, interactive=None, place=False, layer=None,
                 layer_geometry=None):
        self.name, self.group, self.func, self.params = name, group, func, tuple(params)
        self.geometry, self.preview, self.doc, self.interactive, self.place = geometry, preview, doc, interactive, place
        self.layer = layer
        self.layer_geometry = layer_geometry          # None, or how it may transform one layer: 'exact' / 'resample'


def tool(name, group, params=(), geometry=False, preview=True, doc="", interactive=None, place=False,
         layer=None, layer_geometry=None):
    """Register `func(img, **params) -> img` as a tool. `img` is an (H, W, 3) uint8 array that must not be
    modified in place (it belongs to the Undo history); return a new array, or `img` itself for 'no change'.
    The keys of `params` are the keyword arguments."""
    def deco(func):
        TOOLS[name] = Tool(name, group, func, params, geometry, preview, doc or (func.__doc__ or "").strip(),
                           interactive, place, layer, layer_geometry)
        return func
    return deco


def apply_tool(name, img, params=None, region=None):
    """Run a registered tool. `region` = (x0, y0, x1, y1) limits a non-geometry tool to that rectangle."""
    t = TOOLS[name]
    values = {p.key: p.default for p in t.params}
    for p in t.params:
        if params and p.key in params:
            values[p.key] = p.coerce(params[p.key])
    if values.get('fill') == 'transparent':           # only a layer has transparency: pixels have to be white here
        values['fill'] = 'white'
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
    geometry=True, layer_geometry='exact')
def rotate_quarter(img, turn):
    """Exact quarter turns: no resampling, no loss."""
    k = {'90 clockwise': -1, '90 counter-clockwise': 1, '180': 2}[turn]
    return np.ascontiguousarray(np.rot90(img, k))


@tool("Rotate (any angle)", "Geometry", [
    Param('angle', "Angle (deg, counter-clockwise)", 'float', 0.0, -180.0, 180.0, step=0.1, slider=True,
          pick='angle'),
    Param('expand', "Enlarge canvas to fit", 'bool', True),
    Param('fill', "Fill the corners with", 'choice', 'white', choices=('white', 'black', 'edge'))],
    geometry=True, layer_geometry='resample')
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
      geometry=True, layer_geometry='exact')
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
    geometry=True, preview=False, layer_geometry='resample')
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


def fill_nearest(img, hole, valid=None):
    """Paint the `hole` pixels (bool mask) with the colour of the nearest pixel that is not in the hole (and, when given,
    is in `valid`): erasing a coloured line or text leaves the surrounding colour or gradient instead of a flat patch.
    Returns `img` itself when there is nothing to fill or nothing to copy from."""
    src = ~hole if valid is None else (valid & ~hole)
    if not hole.any() or not src.any():
        return img
    _, labels = cv2.distanceTransformWithLabels(np.where(src, 0, 255).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_5,
                                                labelType=cv2.DIST_LABEL_PIXEL)
    sy, sx = np.nonzero(src)
    lut_y, lut_x = np.zeros(labels.max() + 1, np.intp), np.zeros(labels.max() + 1, np.intp)
    lut_y[labels[sy, sx]], lut_x[labels[sy, sx]] = sy, sx           # label -> the source pixel that owns it
    hy, hx = np.nonzero(hole)
    out = img.copy()
    out[hy, hx] = img[lut_y[labels[hy, hx]], lut_x[labels[hy, hx]]]
    return out


@tool("Erase / keep colours", "Clean", [
    Param('colors', "Colours (click Pick on the image)", 'colors', '', pick='color'),
    Param('mode', "Mode", 'choice', 'erase', choices=('erase', 'keep only')),
    Param('tolerance', "Tolerance (RGB distance)", 'int', 60, 1, 441, slider=True),
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS) + ('transparent', 'nearest colour'))])
def erase_colors(img, colors, mode, tolerance, fill):
    """Paint with the fill colour the pixels within the tolerance of any picked colour ('erase') or of none of them
    ('keep only'). 'nearest colour' copies, for each such pixel, the colour of the closest pixel that was not picked
    (a line over a gradient disappears into the gradient). Shades of one ink need several picks, or a larger tolerance."""
    cols = parse_colors(colors)
    if not cols:
        return img
    near = np.zeros(img.shape[:2], bool)
    for c in cols:
        near |= _distance_from(img, c) < tolerance
    hit = ~near if mode == 'keep only' else near
    if not hit.any():
        return img
    if fill == 'nearest colour':
        return fill_nearest(img, hit)
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
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS) + ('transparent',))],
    preview=False, interactive='brush',
    doc="Drag on the image to paint over it (limited to the selection if there is one: draw the selection with another tool first). "
        "On an image layer 'transparent' erases to see through. One stroke is one Undo step.")
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


@functools.lru_cache(maxsize=64)
def _render_text_cached(text, size, font, color, angle, opacity):
    text = text.replace("\n", "\n")                  # a typed backslash-n is a line break (the box is one line)
    if not text.strip() or opacity == 0:
        return None
    cols = parse_colors(color)
    rgb = cols[0] if cols else (0, 0, 0)
    f = load_font(font, size)
    probe = ImageDraw.Draw(Image.new('L', (1, 1)))
    l, t, r, b = probe.multiline_textbbox((0, 0), text, font=f)
    pad = 1
    mask = Image.new('L', (r - l + 2 * pad, b - t + 2 * pad), 0)
    ImageDraw.Draw(mask).multiline_text((pad - l, pad - t), text, font=f, fill=255)
    if angle:
        mask = mask.rotate(angle, expand=True, resample=Image.BICUBIC)
    a = np.asarray(mask, np.float32) * (opacity / 100.0)
    out = np.empty(a.shape + (4,), np.uint8)
    out[..., :3] = rgb
    out[..., 3] = _to_u8(a)
    out.flags.writeable = False                       # shared by the cache
    return out


def render_text(text, size, font, color, angle=0.0, opacity=100):
    """RGBA array (a tight box round the text, straight alpha) or None for an empty text. Anti-aliased, with the
    font of this computer, optionally rotated (counter-clockwise, degrees) and semi-transparent."""
    return _render_text_cached(str(text), int(size), str(font), str(color), float(angle), int(opacity))


def blend_rgba(dst, rgba, x, y, opacity=1.0):
    """Blend `rgba` (straight alpha) onto the RGB image `dst` IN PLACE with its top-left corner at (x, y), times
    `opacity` (0..1). Whatever falls outside `dst` is dropped. Returns False when nothing was inside."""
    h, w = dst.shape[:2]
    mh, mw = rgba.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(w, x + mw), min(h, y + mh)
    if x1 <= x0 or y1 <= y0:
        return False
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    a = src[..., 3:4].astype(np.float32) * (opacity / 255.0)
    dst[y0:y1, x0:x1] = _to_u8(dst[y0:y1, x0:x1].astype(np.float32) * (1 - a) + src[..., :3].astype(np.float32) * a)
    return True


@tool("Add text", "Draw", [
    Param('text', "Text (type \n for a new line)", 'text', "Text"),
    Param('size', "Size (px)", 'int', 32, 4, 2000, slider=True),
    Param('font', "Font", 'choice', FONT_CHOICES[0], choices=FONT_CHOICES),
    Param('color', "Colour", 'colors', '#000000', pick='color1'),
    Param('x', "X of the top-left corner (px)", 'int', 20, -100000, 100000),
    Param('y', "Y of the top-left corner (px)", 'int', 20, -100000, 100000),
    Param('angle', "Rotate (deg, counter-clockwise)", 'float', 0.0, -180.0, 180.0, step=0.5, slider=True),
    Param('opacity', "Opacity (%)", 'int', 100, 0, 100, slider=True)],
    place=True, layer='text',
    doc="Click or drag on the image to place the text, then press the button: the text becomes a layer, which you can "
        "select in the Layers tab and edit (or move) later. As a plain function it paints the text into the pixels.")
def add_text(img, text, size, font, color, x, y, angle, opacity):
    """Paint the text into the pixels (the app makes a layer instead and bakes it only on Flatten)."""
    rgba = render_text(text, size, font, color, angle, opacity)
    if rgba is None:
        return img
    out = img.copy()
    return out if blend_rgba(out, rgba, x, y) else img


# ---- layers -----------------------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, eq=False)
class Layer:
    """A picture over the background. kind 'image': pixels with alpha (`rgba`, (H, W, 4) uint8, never modified in place);
    kind 'text': the keyword arguments of render_text in `params`, rasterised on demand so the text stays editable.
    (x, y) is the top-left corner on the canvas (may be outside it), `opacity` 0..100. Frozen: change one with replace()
    and put the new object in the history."""
    kind: str
    name: str
    x: int = 0
    y: int = 0
    visible: bool = True
    opacity: int = 100
    rgba: object = None
    params: object = None

    def replace(self, **kw):
        return dataclasses.replace(self, **kw)

    def raster(self):
        """The RGBA pixels of the layer (None for an empty text)."""
        if self.kind == 'text':
            return render_text(**self.params)
        return self.rgba

    @property
    def nbytes(self):
        return self.rgba.nbytes if self.rgba is not None else 0


def composite(base, layers):
    """The picture as seen: `layers` (bottom to top) blended over `base`. Returns `base` itself when no layer shows."""
    out = None
    for L in layers:
        if not L.visible or L.opacity <= 0:
            continue
        rgba = L.raster()
        if rgba is None:
            continue
        if out is None:
            out = base.copy()
        blend_rgba(out, rgba, L.x, L.y, L.opacity / 100.0)
    return base if out is None else out


def shift_layers(layers, dx, dy):
    return tuple(L.replace(x=L.x + dx, y=L.y + dy) for L in layers)


def layer_region(sel, layer):
    """The canvas rectangle `sel` in the layer's own pixel coordinates, clipped to the layer; empty when they do not
    overlap (x1 <= x0 or y1 <= y0). None stays None."""
    if sel is None:
        return None
    h, w = layer.rgba.shape[:2]
    x0, y0, x1, y1 = sel[0] - layer.x, sel[1] - layer.y, sel[2] - layer.x, sel[3] - layer.y
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


def apply_tool_to_layer(name, rgba, params=None, region=None):
    """Run a (non-geometry) tool on the pixels of an image layer: the colour channels go through the tool, the alpha
    channel is kept. A tool with a 'fill' parameter set to 'transparent' (erase colours, eraser) makes the pixels it
    would have painted transparent instead: they are found by running it with white and with black and comparing.
    `region` is in layer coordinates (see layer_region); an empty one changes nothing. Returns `rgba` itself when
    nothing changed."""
    if region is not None and (region[2] <= region[0] or region[3] <= region[1]):
        return rgba
    rgb, alpha = np.ascontiguousarray(rgba[..., :3]), rgba[..., 3]
    params = dict(params or {})
    if params.get('fill') in ('transparent', 'nearest colour'):
        a = apply_tool(name, rgb, dict(params, fill='white'), region)
        b = apply_tool(name, rgb, dict(params, fill='black'), region)
        painted = (a != b).any(axis=2)
        if not painted.any():
            return rgba
        if params['fill'] == 'transparent':
            out_rgb, alpha = a, np.where(painted, 0, alpha).astype(np.uint8)
        else:                                          # copy from visible pixels only: transparent ones carry no colour
            out_rgb = fill_nearest(rgb, painted, alpha > 0)
    else:
        out_rgb = apply_tool(name, rgb, params, region)
        if out_rgb is rgb:
            return rgba
    out = np.empty_like(rgba)
    out[..., :3] = out_rgb
    out[..., 3] = alpha
    return out if not np.array_equal(out, rgba) else rgba


def transform_layer(name, layer, params=None):
    """Run a geometry tool (rotate, flip, resize: those with `layer_geometry`) on an image layer; the layer's centre stays
    where it was. 'exact' tools only move pixels, so they run on the RGBA array as it is. 'resample' tools (free rotation,
    resize) run on premultiplied colour (rgb * alpha) and on the alpha plane, then un-premultiply: invisible pixels often
    carry white, and resampling straight RGB would leave a light halo at the edges. The corners of a free rotation are
    transparent whatever its 'fill' says. Returns the new Layer, or `layer` itself when nothing changed."""
    rgba = layer.rgba
    if TOOLS[name].layer_geometry == 'exact':
        out = apply_tool(name, rgba, params)
        if out is rgba:
            return layer
    else:
        f = rgba.astype(np.float32)
        alpha = f[..., 3:4]
        pm = f[..., :3] * (alpha / 255.0)
        params = dict(params or {})
        if any(p.key == 'fill' for p in TOOLS[name].params):
            params['fill'] = 'black'
        pm_out = apply_tool(name, pm, params)
        if pm_out is pm:
            return layer
        a_out = np.clip(apply_tool(name, np.repeat(alpha, 3, axis=2), params)[..., :1], 0, 255)
        pm_out = np.clip(pm_out, 0, a_out)              # premultiplied colour never exceeds the alpha
        rgb = np.where(a_out > 0, pm_out * 255.0 / np.maximum(a_out, 1e-6), 0)
        out = np.empty(pm_out.shape[:2] + (4,), np.uint8)
        out[..., :3] = np.clip(np.rint(rgb), 0, 255)
        out[..., 3] = np.clip(np.rint(a_out[..., 0]), 0, 255)
    h, w = rgba.shape[:2]
    nh, nw = out.shape[:2]
    return layer.replace(rgba=out, x=layer.x + (w - nw) // 2, y=layer.y + (h - nh) // 2)


def merge_layers(lower, upper, name=None):
    """One image layer holding `upper` over `lower` (each with its own opacity); the result is opaque-opacity 100,
    its box is the union of the two, and a text layer loses its editability."""
    parts = [(L.raster(), L) for L in (lower, upper)]
    parts = [(r, L) for r, L in parts if r is not None and L.visible]
    if not parts:
        return lower.replace(kind='image', rgba=np.zeros((1, 1, 4), np.uint8), params=None, name=name or lower.name)
    x0 = min(L.x for _, L in parts)
    y0 = min(L.y for _, L in parts)
    x1 = max(L.x + r.shape[1] for r, L in parts)
    y1 = max(L.y + r.shape[0] for r, L in parts)
    col = np.zeros((y1 - y0, x1 - x0, 3), np.float32)
    alpha = np.zeros((y1 - y0, x1 - x0), np.float32)
    for r, L in parts:                                # straight-alpha 'over': bottom first
        h, w = r.shape[:2]
        sl = (slice(L.y - y0, L.y - y0 + h), slice(L.x - x0, L.x - x0 + w))
        a_s = r[..., 3].astype(np.float32) / 255.0 * (L.opacity / 100.0)
        a_d = alpha[sl]
        a_o = a_s + a_d * (1 - a_s)
        safe = np.where(a_o > 0, a_o, 1.0)[..., None]
        col[sl] = (r[..., :3].astype(np.float32) * a_s[..., None] + col[sl] * (a_d * (1 - a_s))[..., None]) / safe
        alpha[sl] = a_o
    rgba = np.dstack([_to_u8(col), _to_u8(alpha * 255.0)])
    return Layer('image', name or lower.name, x0, y0, True, 100, rgba=rgba)


def load_rgba(path):
    """RGBA uint8 array of an image file with its transparency kept (EXIF orientation applied)."""
    with Image.open(path) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        return np.array(im.convert('RGBA'))


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


@tool("Move layer", "Layers", [], preview=False, interactive='layer',
      doc="Drag on the image to move the layer selected in the Layers tab (arrow keys nudge it). One drag is one Undo step.")
def move_layer_tool(img):
    return img


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
    """Undo/Redo stack of (label, background array, layers tuple). Arrays and Layer objects are never modified in
    place, so a state just keeps references: a step that only touches the layers shares the background of the
    previous one."""

    def __init__(self, img, limit_bytes=HISTORY_BYTES, layers=()):
        self.states = [("Original", img, tuple(layers))]
        self.index = 0
        self.limit = limit_bytes

    @property
    def current(self):
        return self.states[self.index][1]

    @property
    def layers(self):
        return self.states[self.index][2]

    def _bytes(self):
        seen, total = set(), 0
        for _, img, layers in self.states:
            for a in [img] + [L.rgba for L in layers if L.rgba is not None]:
                if id(a) not in seen:                 # shared between states: count once
                    seen.add(id(a))
                    total += a.nbytes
        return total

    def push(self, label, img, layers=None):
        """New state; `layers` None keeps the current layers."""
        layers = self.layers if layers is None else tuple(layers)
        del self.states[self.index + 1:]
        self.states.append((label, img, layers))
        self.index += 1
        while len(self.states) > 2 and self._bytes() > self.limit:
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


PROJECT_EXT = ".ifl"
PROJECT_VERSION = 1


def _png_bytes(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, 'PNG', compress_level=6)
    return buf.getvalue()


def save_project(path, base, layers, dpi=None):
    """Write a project: a zip with manifest.json (version, size, dpi, one entry per layer: kind, name, position,
    visibility, opacity and, for text, its parameters), background.png and one RGBA layer_N.png per image layer.
    Written to a temporary file first, so a failure never destroys the previous project."""
    entries, files = [], {}
    for i, L in enumerate(layers):
        e = dict(kind=L.kind, name=L.name, x=int(L.x), y=int(L.y), visible=bool(L.visible), opacity=int(L.opacity))
        if L.kind == 'text':
            e['params'] = L.params
        else:
            e['file'] = "layer_%d.png" % i
            files[e['file']] = _png_bytes(L.rgba)
        entries.append(e)
    manifest = dict(format="ImageForge project", version=PROJECT_VERSION, width=int(base.shape[1]),
                    height=int(base.shape[0]), dpi=list(dpi) if dpi else None, layers=entries)
    tmp = path + ".tmp"
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_STORED) as zf:          # PNG is already compressed
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
            zf.writestr("background.png", _png_bytes(base))
            for name, data in files.items():
                zf.writestr(name, data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


TEXT_KEYS = {'text': str, 'size': int, 'font': str, 'color': str, 'angle': float, 'opacity': int}


def load_project(path):
    """(background RGB array, layers tuple, dpi or None) from a project file; ValueError with a plain message when
    the file is not a valid project or comes from a newer ImageForge."""
    try:
        with zipfile.ZipFile(path) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            if manifest.get("format") != "ImageForge project":
                raise ValueError("not an ImageForge project")
            if int(manifest["version"]) > PROJECT_VERSION:
                raise ValueError("saved by a newer ImageForge (project version %s)" % manifest["version"])
            base = np.array(Image.open(io.BytesIO(zf.read("background.png"))).convert('RGB'))
            if base.shape[:2] != (manifest["height"], manifest["width"]):
                raise ValueError("the background does not match the recorded size")
            layers = []
            for e in manifest["layers"]:
                common = dict(name=str(e["name"]), x=int(e["x"]), y=int(e["y"]), visible=bool(e["visible"]),
                              opacity=max(0, min(100, int(e["opacity"]))))
                if e["kind"] == 'text':
                    params = {k: t(e["params"][k]) for k, t in TEXT_KEYS.items()}
                    layers.append(Layer('text', params=params, **common))
                elif e["kind"] == 'image':
                    rgba = np.array(Image.open(io.BytesIO(zf.read(e["file"]))).convert('RGBA'))
                    layers.append(Layer('image', rgba=rgba, **common))
                else:
                    raise ValueError("unknown layer kind %r" % e["kind"])
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, TypeError) as ex:
        raise ValueError("not a valid ImageForge project (%s: %s)" % (type(ex).__name__, ex))
    dpi = manifest.get("dpi")
    return base, tuple(layers), (float(dpi[0]), float(dpi[1])) if dpi else None


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
        self._pan_left = False           # the left button is panning (Hand mode, or Space held)
        self._space = False
        self._params_ready = False
        self.mode = None                 # None, 'color' or 'angle': what a click on the image means (Esc cancels)
        self._pick_key = None            # the parameter a pick fills
        self._pick_pts = []
        self.active = None               # selected layer (index into hist.layers), None = the Background
        self.lprev = None                # layers shown instead of the history's while a layer is edited/dragged
        self._lmove = None               # (press point, layer index, layer) while dragging a layer
        self._lsync = False              # True while the layer boxes are filled from a layer (no change events)
        self._comp = None                # cache of the last composite (base, layers, image)
        self._mv = None                  # (press point, selection, copy?, original image) while moving the content
        self._placing = False            # a click/drag is setting the x, y of a 'place' tool
        self._grab = None                # (handle, press point in image pixels, selection at press) while dragging it
        self._stroke_layer = None        # the layer a brush stroke paints on (None: the Background)
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
        self.m_layer = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="File", menu=self.m_file)
        mb.add_cascade(label="Edit", menu=self.m_edit)
        mb.add_cascade(label="View", menu=self.m_view)
        mb.add_cascade(label="Image", menu=self.m_image)
        mb.add_cascade(label="Layer", menu=self.m_layer)
        f = self.m_file
        f.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_dialog)
        f.add_command(label="Open as layer...", command=self.open_layer_dialog)
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
        e.add_separator()
        e.add_command(label="Copy selection to layer", accelerator="Ctrl+J", command=lambda: self.selection_to_layer(False))
        e.add_command(label="Cut selection to layer", accelerator="Ctrl+Shift+J", command=lambda: self.selection_to_layer(True))
        ly = self.m_layer
        for label, cmd in (("Raise", self.layer_up), ("Lower", self.layer_down), ("Duplicate", self.layer_duplicate),
                           ("Delete", self.layer_delete), ("Merge down", self.layer_merge_down),
                           ("Flatten all", self.flatten_all)):
            ly.add_command(label=label, command=cmd)
        v = self.m_view
        v.add_command(label="Zoom in", accelerator="Ctrl++", command=lambda: self._zoom_step(1.25))
        v.add_command(label="Zoom out", accelerator="Ctrl+-", command=lambda: self._zoom_step(0.8))
        v.add_command(label="Fit to window", accelerator="Ctrl+0", command=self.fit_view)
        v.add_command(label="Center", command=self.center_view)
        v.add_command(label="Actual size (100%)", accelerator="Ctrl+1", command=self.actual_size)

    def _build_ui(self):
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=5, bg='#bbbbbb')
        paned.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(paned)
        paned.add(left, stretch='always')
        bar = tk.Frame(left, bg='#e8e8e8')
        bar.pack(fill=tk.X)
        for text, cmd in (("Center", self.center_view), ("Fit", self.fit_view), ("100%", self.actual_size)):
            tk.Button(bar, text=text, command=cmd, width=7).pack(side=tk.LEFT, padx=(4, 0), pady=2)
        self.v_hand = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="Hand: drag moves the picture", variable=self.v_hand, indicatoron=False, padx=8,
                       command=self._hand_changed).pack(side=tk.LEFT, padx=(10, 0), pady=2)
        tk.Label(bar, text="(or hold Space, or drag with the right button)", bg='#e8e8e8', fg='#666666',
                 font=('Arial', 8)).pack(side=tk.LEFT, padx=6)
        self.canvas = tk.Canvas(left, bg='#3a3a3a', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(fill=tk.BOTH, expand=True)
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

        self.nbk = ttk.Notebook(panel)
        self.nbk.pack(fill=tk.BOTH, expand=True, padx=4, pady=(6, 0))
        outer, tab = self._scrollable(self.nbk, bg)
        self.nbk.add(outer, text="  Tools  ")
        tab_layers = tk.Frame(self.nbk, bg=bg)
        self.nbk.add(tab_layers, text="  Layers  ")
        self._build_layers_tab(tab_layers, bg)
        tf = tk.Frame(tab, bg=bg)
        tf.pack(fill=tk.X, padx=8, pady=2)
        self.tree = ttk.Treeview(tf, show='tree', selectmode='browse', height=21)
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._on_tree_select())

        self.l_doc = tk.Label(tab, text="", bg=bg, fg='#555555', font=('Arial', 8), wraplength=300, justify='left',
                              anchor='w')
        self.l_doc.pack(fill=tk.X, padx=10, pady=(4, 0))
        self.params_frame = tk.Frame(tab, bg=bg)
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
        r.bind('<KeyPress-space>', self._space_down)
        r.bind('<KeyRelease-space>', self._space_up)
        for seq, fn in (("<Control-o>", self.open_dialog), ("<Control-s>", self.save),
                        ("<Control-S>", self.save_as), ("<Control-z>", self.undo), ("<Control-y>", self.redo),
                        ("<Control-a>", self.select_all), ("<Control-d>", self.deselect),
                        ("<Control-X>", self.crop_to_selection), ("<Control-j>", lambda: self.selection_to_layer(False)),
                        ("<Control-J>", lambda: self.selection_to_layer(True)), ("<Control-0>", self.fit_view),
                        ("<Control-1>", self.actual_size), ("<Control-plus>", lambda: self._zoom_step(1.25)),
                        ("<Control-equal>", lambda: self._zoom_step(1.25)),
                        ("<Control-minus>", lambda: self._zoom_step(0.8)),
                        ("<Control-Return>", self.apply), ("<Escape>", self.escape)):
            r.bind(seq, lambda e, fn=fn: self._key(fn, e))

    def _space_down(self, event):
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox, ttk.Treeview)):
            return None                                 # a space typed in a box (or a tree) is not a pan
        if not self._space:
            self._space = True
            self.canvas.config(cursor='hand2')
        return "break"

    def _space_up(self, event):
        if self._space:
            self._space = False
            self.canvas.config(cursor='hand2' if self.v_hand.get() else 'crosshair')

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
        self.preview = self.lprev = None
        ei = self._edit_index()
        if ei is not None:
            self._load_layer_into_tool(self.hist.layers[ei])
        self._update_state()
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
        if (self.hist is None or self.tool is None or self._stroke is not None or self._mv is not None
                or self._lmove is not None):
            return
        if self.tool.layer:
            self.preview, self.lprev = None, None
            params = self._read_params() if self.v_preview.get() else None
            new = None
            if params is not None:
                ei = self._edit_index()
                new = self._layer_from_params(params, self.hist.layers[ei] if ei is not None else None)
                layers = list(self.hist.layers)
                if new is not None:
                    if ei is not None:
                        layers[ei] = new
                    else:
                        layers.append(new)
                    self.lprev = tuple(layers)
            self._redraw()
            return
        if not (self.v_preview.get() and self.tool.preview):
            if self.preview is not None or self.lprev is not None:
                self.preview = self.lprev = None
                self._redraw()
            return
        params = self._read_params()
        if params is None:
            return
        kind, L = self._tool_target()
        if self.tool.geometry and self.tool.layer_geometry and kind != 'base':
            self.preview = self.lprev = None
            if kind == 'layer':
                try:
                    new = transform_layer(self.tool.name, L, params)
                except Exception as ex:
                    self._status("%s: %s" % (self.tool.name, ex))
                    return
                if new is not L:
                    layers = list(self.hist.layers)
                    layers[self.active] = new
                    self.lprev = tuple(layers)
            self._redraw()
            return
        if not self.tool.geometry and kind != 'base':
            self.preview = self.lprev = None
            if kind == 'layer':                               # the tool works on the pixels of the selected layer
                try:
                    out = apply_tool_to_layer(self.tool.name, L.rgba, params, layer_region(self.sel, L))
                except Exception as ex:
                    self._status("%s: %s" % (self.tool.name, ex))
                    return
                if out is not L.rgba:
                    layers = list(self.hist.layers)
                    layers[self.active] = L.replace(rgba=out)
                    self.lprev = tuple(layers)
            self._redraw()
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
        if self.tool.layer:
            self._apply_layer_tool(params)
            return
        kind, L = self._tool_target()
        if kind == 'text' and (not self.tool.geometry or self.tool.layer_geometry):
            self._status(self._TEXT_LAYER_MSG)
            return
        if self.tool.geometry and self.tool.layer_geometry and kind == 'layer':
            try:
                new = transform_layer(self.tool.name, L, params)
            except Exception as ex:
                self._status("%s failed: %s" % (self.tool.name, ex))
                return
            if new is L:
                self._status("%s: no change with these parameters." % self.tool.name)
                return
            layers = list(self.hist.layers)
            layers[self.active] = new
            self._commit_layers("%s (layer)" % self.tool.name, layers)
            self._status("%s applied on layer '%s' (its centre stays put)." % (self.tool.name, L.name), ok=True)
            return
        if not self.tool.geometry and kind == 'layer':
            region = layer_region(self.sel, L)
            try:
                out = apply_tool_to_layer(self.tool.name, L.rgba, params, region)
            except Exception as ex:
                self._status("%s failed: %s" % (self.tool.name, ex))
                return
            if out is L.rgba:
                self.lprev = None
                self._redraw()
                self._status("%s: no change on layer '%s'%s." % (self.tool.name, L.name, " (the selection does not "
                             "touch it)" if region is not None and (region[2] <= region[0] or region[3] <= region[1]) else ""))
                return
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self._commit_layers("%s (layer)" % self.tool.name, layers)
            self._status("%s applied on layer '%s'." % (self.tool.name, L.name), ok=True)
            return
        cur = self.hist.current
        region = None if self.tool.geometry else self.sel
        flatten = bool(self.tool.geometry and self.hist.layers)
        if flatten:
            if not messagebox.askyesno(APP_NAME, "'%s' changes the whole picture, so the layers are merged into the "
                                       "background first. Continue?" % self.tool.name):
                return
            cur = self._flat()
        try:
            out = apply_tool(self.tool.name, cur, params, region)
        except Exception as ex:
            self._status("%s failed: %s" % (self.tool.name, ex))
            return
        if not flatten and (out is cur or (out.shape == cur.shape and np.array_equal(out, cur))):
            self.preview = None
            self._redraw()
            self._status("%s: no change with these parameters." % self.tool.name)
            return
        if flatten:
            self.active = None
        self._commit(self.tool.name, out, () if flatten else None)
        extra = " (applied to the whole image: this tool ignores the selection)" if self.sel and self.tool.geometry else ""
        if self.hist.layers:
            extra += " (on the Background: select a layer to filter it)"
        self._status("%s applied%s." % (self.tool.name, extra), ok=True)

    def _commit(self, label, img, layers=None):
        """Put a new state on the history (`layers` None keeps the current ones). Size changes drop the selection and
        refit the view."""
        resized = img.shape[:2] != self.hist.current.shape[:2]
        self.hist.push(label, img, layers)
        self.preview = self.lprev = None
        self.dirty = True
        if resized:
            self.sel = None
            self.fit_view()
        self._refresh_layers()
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
        project = path.lower().endswith(PROJECT_EXT)
        try:
            if project:
                img, layers, dpi = load_project(path)
            else:
                (img, dpi), layers = load_image(path), ()
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot open %s:\n%s" % (path, ex))
            return False
        self.hist, self.path, self.dpi = History(img, layers=layers), path, dpi
        self.save_path = path if project else None             # a project is saved back to itself, an image never
        self.sel, self.preview, self.dirty = None, None, False
        self.active, self.lprev, self._comp = None, None, None
        self._refresh_layers()
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
        """Ctrl+S: back to the project, or to the image saved before; with layers and no project yet it asks, because a
        plain image file would lose them."""
        if not self._need_image():
            return
        if self.save_path is None or (self.hist.layers and not self.save_path.lower().endswith(PROJECT_EXT)):
            return self.save_as()
        self._write(self.save_path)

    def save_as(self):
        if not self._need_image():
            return
        base = os.path.splitext(os.path.basename(self.path or "image"))[0]
        layered = bool(self.hist.layers)
        types = [PROJECT_TYPE] + SAVE_TYPES if layered else SAVE_TYPES + [PROJECT_TYPE]
        path = filedialog.asksaveasfilename(parent=self.root, filetypes=types,
                                            defaultextension=PROJECT_EXT if layered else ".png",
                                            initialfile=base + ("" if self.path and self.path.lower().endswith(PROJECT_EXT)
                                                                else "_edited") + (PROJECT_EXT if layered else ".png"))
        if path:
            self._write(path)

    def _write(self, path):
        """A .ifl path writes the project (layers kept); any other writes the picture as seen. Exporting a picture that
        has layers does not count as saving it: the work stays 'unsaved' until a project is written."""
        project = path.lower().endswith(PROJECT_EXT)
        try:
            if project:
                save_project(path, self.hist.current, self.hist.layers, self.dpi)
            else:
                save_image(self._flat(), path, self.dpi)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot save %s:\n%s" % (path, ex))
            return
        if project or not self.hist.layers:
            self.save_path, self.dirty = path, False
            self._status("Saved %s." % path, ok=True)
        else:
            self._status("Exported the flattened picture to %s. The layers are NOT saved: use a .ifl project to keep them." % path)
        self._update_state()

    def on_close(self):
        if self._confirm_discard():
            if self._after is not None:
                self.root.after_cancel(self._after)
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
        self.preview, self.lprev, self.dirty = None, None, True
        if self.hist.current.shape[:2] != before:
            self.sel = None
            self.fit_view()
        self._refresh_layers()
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
        x0, y0 = self.sel[:2]
        self._commit("Crop", crop(self.hist.current, self.sel), shift_layers(self.hist.layers, -x0, -y0))

    # ---- layers ----------------------------------------------------------------------------------------
    @staticmethod
    def _scrollable(parent, bg):
        """(outer, inner): `inner` scrolls vertically inside `outer`; the scrollbar shows only when it is needed."""
        outer = tk.Frame(parent, bg=bg)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        cv = tk.Canvas(outer, bg=bg, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient='vertical', command=cv.yview)
        cv.configure(yscrollcommand=sb.set)
        cv.grid(row=0, column=0, sticky='nsew')
        inner = tk.Frame(cv, bg=bg)
        win = cv.create_window(0, 0, window=inner, anchor='nw')

        def fit(_=None):
            cv.configure(scrollregion=cv.bbox('all'))
            cv.itemconfigure(win, width=cv.winfo_width())
            if inner.winfo_reqheight() > cv.winfo_height():
                sb.grid(row=0, column=1, sticky='ns')
            else:
                sb.grid_remove()
                cv.yview_moveto(0)
        inner.bind('<Configure>', fit)
        cv.bind('<Configure>', fit)

        def wheel(e):
            if inner.winfo_reqheight() > cv.winfo_height():
                cv.yview_scroll(-1 if e.delta > 0 else 1, 'units')
        outer.bind('<Enter>', lambda e: outer.bind_all('<MouseWheel>', wheel))
        outer.bind('<Leave>', lambda e: outer.unbind_all('<MouseWheel>'))
        return outer, inner

    def _build_layers_tab(self, tab, bg):
        tk.Label(tab, text="Top of the list = on top of the picture. Double-click a layer to edit or move it.", bg=bg,
                 fg='#555555', font=('Arial', 8), anchor='w', justify='left', wraplength=300).pack(fill=tk.X, padx=8, pady=(6, 0))
        tf = tk.Frame(tab, bg=bg)
        tf.pack(fill=tk.X, padx=6, pady=2)
        self.ltree = ttk.Treeview(tf, show='tree', selectmode='browse', height=7)
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.ltree.yview)
        self.ltree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.ltree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.ltree.bind('<<TreeviewSelect>>', lambda e: self._on_layer_select())
        self.ltree.bind('<Double-1>', lambda e: self._on_layer_double())
        for buttons in ((("Up", self.layer_up), ("Down", self.layer_down), ("Duplicate", self.layer_duplicate),
                         ("Delete", self.layer_delete)),
                        (("Merge down", self.layer_merge_down), ("Flatten all", self.flatten_all),
                         ("Open as layer...", self.open_layer_dialog))):
            row = tk.Frame(tab, bg=bg)
            row.pack(fill=tk.X, padx=6, pady=1)
            for text, cmd in buttons:
                tk.Button(row, text=text, command=cmd).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=1)
        props = tk.LabelFrame(tab, text="Selected layer", bg=bg, font=('Arial', 8))
        props.pack(fill=tk.X, padx=6, pady=(6, 2))
        self.v_lname, self.v_lvis = tk.StringVar(), tk.BooleanVar(value=True)
        self.v_lop, self.v_lx, self.v_ly = tk.DoubleVar(value=100), tk.DoubleVar(), tk.DoubleVar()
        e = ttk.Entry(props, textvariable=self.v_lname)
        e.pack(fill=tk.X, padx=4, pady=(2, 0))
        e.bind('<Return>', lambda ev: self._layer_prop())
        e.bind('<FocusOut>', lambda ev: self._layer_prop())
        tk.Checkbutton(props, text="Visible", variable=self.v_lvis, bg=bg, command=self._layer_prop).pack(anchor='w')
        r = tk.Frame(props, bg=bg)
        r.pack(fill=tk.X, padx=4)
        tk.Label(r, text="Opacity %", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT)
        sc = ttk.Scale(r, from_=0, to=100, variable=self.v_lop, orient='horizontal',
                       command=lambda v: self._layer_live(opacity=int(round(float(v)))))
        sc.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)
        sc.bind('<ButtonRelease-1>', lambda ev: self._layer_prop())
        r = tk.Frame(props, bg=bg)
        r.pack(fill=tk.X, padx=4, pady=(0, 4))
        for text, var in (("X", self.v_lx), ("Y", self.v_ly)):
            tk.Label(r, text=text, bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(4, 1))
            sp = ttk.Spinbox(r, from_=-100000, to=100000, increment=1, textvariable=var, width=7, format='%.0f',
                             command=self._layer_prop)
            sp.pack(side=tk.LEFT)
            sp.bind('<Return>', lambda ev: self._layer_prop())
            sp.bind('<FocusOut>', lambda ev: self._layer_prop())

    def _cur_layers(self):
        return self.hist.layers if self.hist else ()

    _TEXT_LAYER_MSG = ("A text layer is not made of pixels: select the Background or an image layer, or merge the "
                       "text down first. (Its text, font and colour are edited with Add text.)")

    def _tool_target(self):
        """What an ordinary tool works on: ('base', None), ('layer', image layer) or ('text', text layer)."""
        L = self._active_layer()
        if L is None:
            return 'base', None
        return ('text' if L.kind == 'text' else 'layer'), L

    def _active_layer(self):
        layers = self._cur_layers()
        return layers[self.active] if self.active is not None and self.active < len(layers) else None

    def _refresh_layers(self):
        """Rebuild the layer list from the history state and show the properties of the active layer."""
        layers = self._cur_layers()
        if self.active is not None and self.active >= len(layers):
            self.active = None
        self._lsync = True
        try:
            self.ltree.delete(*self.ltree.get_children())
            for i in range(len(layers) - 1, -1, -1):
                L = layers[i]
                self.ltree.insert('', tk.END, iid='L%d' % i,
                                  text=("\u25cf " if L.visible else "\u25cb ") + L.name + ("  [text]" if L.kind == 'text' else ""))
            self.ltree.insert('', tk.END, iid='BG', text="Background")
            self.ltree.selection_set('BG' if self.active is None else 'L%d' % self.active)
            L = self._active_layer()
            self.v_lname.set(L.name if L else "")
            self.v_lvis.set(L.visible if L else True)
            self.v_lop.set(L.opacity if L else 100)
            self.v_lx.set(L.x if L else 0)
            self.v_ly.set(L.y if L else 0)
        finally:
            self._lsync = False

    def _on_layer_select(self):
        if self._lsync or self.hist is None:
            return
        sel = self.ltree.selection()
        if not sel:
            return
        new = None if sel[0] == 'BG' else int(sel[0][1:])
        if new == self.active:                      # also the echo of _refresh_layers' own selection_set (the virtual
            return                                  # event arrives after the _lsync guard is down): no endless loop
        self.active = new
        self._refresh_layers()
        ei = self._edit_index()
        if ei is not None:
            self._load_layer_into_tool(self._cur_layers()[ei])
        self.lprev = None
        self._update_state()
        self._redraw()

    def _on_layer_double(self):
        L = self._active_layer()
        if L is None:
            return
        name = "Add text" if L.kind == 'text' else "Move layer"
        self.nbk.select(0)
        if self.tree.selection() != (name,):
            self.tree.selection_set(name)
        else:
            self.select_tool(name)

    def _layer_live(self, **kw):
        """Show a property change while a slider is being dragged; _layer_prop() records it."""
        L = self._active_layer()
        if self._lsync or L is None:
            return
        layers = list(self.hist.layers)
        layers[self.active] = L.replace(**kw)
        self.lprev = tuple(layers)
        self._redraw()

    def _layer_prop(self):
        """Take name, visibility, opacity and position from the boxes into the active layer (one history step)."""
        L = self._active_layer()
        if self._lsync or L is None:
            return
        try:
            kw = dict(name=self.v_lname.get().strip() or L.name, visible=bool(self.v_lvis.get()),
                      opacity=int(round(self.v_lop.get())), x=int(round(self.v_lx.get())), y=int(round(self.v_ly.get())))
        except tk.TclError:
            return
        self.lprev = None
        if all(getattr(L, k) == v for k, v in kw.items()):
            self._redraw()
            return
        layers = list(self.hist.layers)
        layers[self.active] = L.replace(**kw)
        what = "Layer visibility" if kw['visible'] != L.visible else "Layer opacity" if kw['opacity'] != L.opacity \
            else "Move layer" if (kw['x'], kw['y']) != (L.x, L.y) else "Rename layer"
        self._commit_layers(what, layers)

    def _commit_layers(self, label, layers, active=-1, base=None):
        """A history step that changes the layers (and, for a merge, the background)."""
        if active != -1:
            self.active = active
        self._commit(label, self.hist.current if base is None else base, tuple(layers))

    def layer_up(self):
        i, layers = self.active, list(self._cur_layers())
        if i is not None and i + 1 < len(layers):
            layers[i], layers[i + 1] = layers[i + 1], layers[i]
            self._commit_layers("Raise layer", layers, i + 1)

    def layer_down(self):
        i, layers = self.active, list(self._cur_layers())
        if i is not None and i > 0:
            layers[i], layers[i - 1] = layers[i - 1], layers[i]
            self._commit_layers("Lower layer", layers, i - 1)

    def layer_duplicate(self):
        L = self._active_layer()
        if L is None:
            self._status("Select a layer first (the Background cannot be duplicated here: use Edit > Copy selection to layer).")
            return
        layers = list(self.hist.layers)
        layers.insert(self.active + 1, L.replace(name=L.name + " copy", x=L.x + 10, y=L.y + 10))
        self._commit_layers("Duplicate layer", layers, self.active + 1)

    def layer_delete(self):
        L = self._active_layer()
        if L is None:
            self._status("Select a layer first: the Background cannot be deleted.")
            return
        layers = list(self.hist.layers)
        del layers[self.active]
        self._commit_layers("Delete layer", layers, min(self.active, len(layers) - 1) if layers else None)

    def layer_merge_down(self):
        L, i = self._active_layer(), self.active
        if L is None:
            self._status("Select a layer first.")
            return
        layers = list(self.hist.layers)
        if i == 0:                                              # onto the background
            base = composite(self.hist.current, (L,))
            del layers[0]
            self._commit_layers("Merge down", layers, None, base=base)
        else:
            layers[i - 1:i + 1] = [merge_layers(layers[i - 1], L)]
            self._commit_layers("Merge down", layers, i - 1)

    def flatten_all(self):
        if not self._need_image():
            return
        if not self.hist.layers:
            self._status("There are no layers to flatten.")
            return
        self.active = None
        self._commit("Flatten", self._flat(), ())

    def open_layer_dialog(self):
        if not self._need_image():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=IMAGE_TYPES, title="Open as layer")
        if not path:
            return
        try:
            rgba = load_rgba(path)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot open %s:\n%s" % (path, ex))
            return
        h, w = self.hist.current.shape[:2]
        L = Layer('image', os.path.splitext(os.path.basename(path))[0], (w - rgba.shape[1]) // 2,
                  (h - rgba.shape[0]) // 2, rgba=rgba)
        self._commit_layers("Add layer", list(self.hist.layers) + [L], len(self.hist.layers))
        self.tree.selection_set("Move layer")

    def selection_to_layer(self, cut):
        """The selected pixels of the Background become a new layer at the same place; a cut fills the hole in the
        Background (with the colour of the 'leaves' box), a copy leaves it alone."""
        if not self._need_image():
            return
        if self.sel is None:
            self._status("Drag a rectangle on the image first.")
            return
        x0, y0, x1, y1 = self.sel
        base = self.hist.current
        rgba = np.dstack([base[y0:y1, x0:x1], np.full((y1 - y0, x1 - x0), 255, np.uint8)])
        L = Layer('image', "Selection", x0, y0, rgba=rgba)
        new_base = base
        if cut:
            new_base = base.copy()
            new_base[y0:y1, x0:x1] = self._fill_color(base)
        self._commit_layers("Cut to layer" if cut else "Copy to layer", list(self.hist.layers) + [L],
                            len(self.hist.layers), base=new_base)
        self.tree.selection_set("Move layer")
        self.nbk.select(1)

    # ---- editing a layer with its tool (text) -----------------------------------------------------------
    def _edit_index(self):
        """Index of the selected layer when it is of the kind the current tool edits (then the tool updates it)."""
        if self.hist is None or self.tool is None or not self.tool.layer or self.active is None:
            return None
        layers = self.hist.layers
        if self.active < len(layers) and layers[self.active].kind == self.tool.layer:
            return self.active
        return None

    def _load_layer_into_tool(self, L):
        """Put the settings of a text layer into the parameter boxes of the Add text tool."""
        self._params_ready = False
        for key, val in dict(L.params, x=L.x, y=L.y).items():
            if key in self.pvars:
                self.pvars[key].set(val)
        self._params_ready = True
        self._params_changed()

    def _layer_from_params(self, params, old=None):
        p = dict(params)
        x, y = p.pop('x'), p.pop('y')
        if render_text(**p) is None:
            return None
        first = p['text'].replace("\\n", "\n").strip().split("\n")[0][:24]
        if old is None:
            return Layer('text', "Text: " + first, x, y, params=p)
        name = "Text: " + first if old.name.startswith("Text: ") else old.name
        return old.replace(x=x, y=y, params=p, name=name)

    def _apply_layer_tool(self, params):
        ei = self._edit_index()
        old = self.hist.layers[ei] if ei is not None else None
        new = self._layer_from_params(params, old)
        if new is None:
            self._status("The text is empty: nothing to add.")
            return
        layers = list(self.hist.layers)
        if ei is not None:
            layers[ei] = new
            self._commit_layers("Edit text layer", layers)
            self._status("Text layer updated. Select 'Background' in the Layers tab to add another text.", ok=True)
        else:
            layers.append(new)
            self._commit_layers("Add text layer", layers, len(layers) - 1)
            self._status("Text added as a layer: change the boxes and press Update layer to edit it.", ok=True)

    # ---- moving a layer with the mouse --------------------------------------------------------------------
    def _lmove_start(self, e):
        L = self._active_layer()
        if L is None:
            self._status("Select a layer in the Layers tab first (the Background does not move).")
            return
        self._lmove = (self._img_xy(e.x, e.y), self.active, L)

    def _lmove_to(self, e):
        (x, y), i, L = self._lmove
        nx, ny = self._img_xy(e.x, e.y)
        layers = list(self.hist.layers)
        layers[i] = L.replace(x=L.x + int(round(nx - x)), y=L.y + int(round(ny - y)))
        self.lprev = tuple(layers)
        self._lsync = True
        self.v_lx.set(layers[i].x)
        self.v_ly.set(layers[i].y)
        self._lsync = False
        self._redraw()
        self._set_info("layer at (%d, %d)" % (layers[i].x, layers[i].y))

    def _lmove_end(self):
        _, i, L = self._lmove
        layers, self.lprev, self._lmove = self.lprev, None, None
        if layers is not None and (layers[i].x, layers[i].y) != (L.x, L.y):
            self._commit_layers("Move layer", layers)
        else:
            self._redraw()

    # ---- canvas ----------------------------------------------------------------------------------------
    def _shown(self):
        """The picture as seen: background (or its preview) with the layers (or their preview) blended over it."""
        if self.hist is None:
            return None
        base = self.preview if self.preview is not None else self.hist.current
        return self._composite_cached(base, self.lprev if self.lprev is not None else self.hist.layers)

    def _composite_cached(self, base, layers):
        """composite() once per distinct (background, layers): panning and zooming redraw without recomputing."""
        layers = tuple(layers)
        c = self._comp
        if c and c[0] is base and len(c[1]) == len(layers) and all(a is b for a, b in zip(c[1], layers)):
            return c[2]
        out = composite(base, layers)
        self._comp = (base, layers, out)
        return out

    def _flat(self):
        """The committed picture with the layers merged (what Save writes), not counting any preview."""
        return self._composite_cached(self.hist.current, self.hist.layers)

    def center_view(self):
        """Put the picture in the middle of the window at the current zoom."""
        if self.hist is None:
            return
        cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
        h, w = self._shown().shape[:2]
        self.ox, self.oy = (cw - w * self.zoom) / 2.0, (ch - h * self.zoom) / 2.0
        self._redraw()

    def _hand_changed(self):
        self.canvas.config(cursor='hand2' if self.v_hand.get() else 'crosshair')

    def _hand_active(self):
        return self.v_hand.get() or self._space

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
        img = self._flat()                       # colours are picked from the picture as seen, layers included
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
        return int(p.get('size', 15)), p.get('fill', 'white')

    def _brush_start(self, e):
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        self._stroke_layer = L if kind == 'layer' else None
        self._stroke = (L.rgba if L is not None else self.hist.current).copy()
        self._last = None
        self._brush_to(e)

    def _brush_to(self, e):
        if self._stroke is None:
            return
        size, fill = self._brush_params()
        L = self._stroke_layer
        x, y = self._img_xy(e.x, e.y)
        pt = (int(math.floor(x)), int(math.floor(y)))
        if L is None:
            paint_segment(self._stroke, self._last or pt, pt, size, FILLS.get(fill, FILLS['white']), self.sel)
        else:                                                  # layer coordinates; four channels: colour and alpha
            lp, ll = (pt[0] - L.x, pt[1] - L.y), self._last and (self._last[0] - L.x, self._last[1] - L.y)
            color = (0, 0, 0, 0) if fill == 'transparent' else FILLS[fill] + (255,)
            paint_segment(self._stroke, ll or lp, lp, size, color, layer_region(self.sel, L))
        self._last = pt
        if L is None:
            self.preview = self._stroke
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=self._stroke)
            self.lprev = tuple(layers)
        self._redraw()
        self._draw_brush_circle(e)

    def _brush_end(self):
        stroke, L = self._stroke, self._stroke_layer
        self._stroke = self._stroke_layer = self._last = None
        self.preview = self.lprev = None
        if L is None:
            changed = not np.array_equal(stroke, self.hist.current)
        else:
            changed = not np.array_equal(stroke, L.rgba)
        if not changed:
            self._redraw()
        elif L is None:
            self._commit(self.tool.name, stroke)
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=stroke)
            self._commit_layers("%s (layer)" % self.tool.name, layers)

    def _draw_brush_circle(self, e):
        self.canvas.delete('brush')
        if self._is_brush() and self.hist is not None:
            r = self._brush_params()[0] / 2.0 * self.zoom
            self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline='#ff3366', tags='brush')

    def _on_press(self, e):
        if self.hist is None:
            self._press = None
            return
        if self._hand_active():                        # the left button moves the picture, whatever the tool
            self._press, self._pan_left = None, True
            self._on_pan_start(e)
            return
        if self.mode:
            self._press = None
            self._pick_click(e)
        elif self._is_brush():
            self._press = None
            self._brush_start(e)
        elif self.tool is not None and self.tool.interactive == 'layer':
            self._press = None
            self._lmove_start(e)
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
        if self.hist is not None and self.tool is not None and self.tool.interactive == 'layer':
            L = self._active_layer()
            if L is not None:
                layers = list(self.hist.layers)
                layers[self.active] = L.replace(x=L.x + dx, y=L.y + dy)
                self._commit_layers("Move layer", layers)
            return
        if self.sel and self.hist is not None and not self.mode and not self._is_brush():
            h, w = self.hist.current.shape[:2]
            self.sel = drag_rect(self.sel, 'move', dx, dy, (w, h))
            self._after_selection()

    def _update_cursor(self, e):
        if self._hand_active():
            self.canvas.config(cursor='hand2')
            return
        if self.mode or self._is_brush():
            return
        if self.tool is not None and self.tool.interactive == 'layer':
            self.canvas.config(cursor='fleur')
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
        if self._pan_left:
            self._on_pan(e)
            return
        if self._stroke is not None:
            self._brush_to(e)
            return
        if self._lmove is not None:
            self._lmove_to(e)
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
        if self._pan_left:
            self._pan_left = False
            return
        if self._stroke is not None:
            self._brush_end()
            return
        if self._lmove is not None:
            self._lmove_end()
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
            r, g, b = self._shown()[iy, ix]
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
        self.b_apply.config(text="Update layer" if self._edit_index() is not None else
                            "Add as layer" if self.tool is not None and self.tool.layer else "Apply")
        self._set_info()


def main():
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = ImageForge(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(100, lambda: app.open_path(sys.argv[1]))
    root.mainloop()


if __name__ == '__main__':
    main()

"""ImageForge - a general-purpose image editor whose tools are small plug-in functions.

Open an image, optionally drag a rectangle to limit an operation to it, pick a tool in the list, tune its
parameters (the result is previewed live) and press Apply. Every step goes on an Undo/Redo history.

A tool is a function registered with the `@tool` decorator that takes an RGB uint8 array and returns one.
The built-in ones are below; more can be dropped as .py files into the `plugins` folder next to this file
(see plugins/example_threshold.py) without touching this file.

The core (registry, tools, history, load/save) does not need a display, so the tests can import it.
"""
import base64
import dataclasses
import functools
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile

import numpy as np
import cv2
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False

APP_NAME = "ImageForge"
HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.join(HERE, "plugins")
ERROR_LOG = os.path.join(HERE, "ImageForge_errors.log")
HISTORY_BYTES = 1 << 30            # oldest undo steps are dropped beyond this (at least 2 states are always kept)
IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif"), ("All files", "*.*")]
OPEN_TYPES = [("Images and projects", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.gif *.ifl"), ("All files", "*.*")]
SAVE_TYPES = [("PNG", "*.png"), ("JPEG", "*.jpg"), ("TIFF", "*.tif"), ("BMP", "*.bmp"), ("WebP", "*.webp")]
PROJECT_TYPE = ("ImageForge project (layers kept)", "*.ifl")

# ---------------------------------------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------------------------------------
TOOLS = {}                          # name -> Tool, in registration order (which is the order of the tool list)
TOOL_LAYOUT = {
    'Draw': ['Pencil', 'Paintbrush', 'Eraser brush', 'Fill bucket', 'Gradient'],
    'Retouch': ['Clone stamp', 'Clean brush', 'Dodge / burn', 'Erase rectangle', 'Move area'],
    'Annotate': ['Add text', 'Shapes', 'Scale bar', 'Measure'],
    'Layers': ['Move layer'],
    'Size and orientation': ['Resize', 'Enlarge (AI)', 'Canvas size', 'Crop to aspect ratio', 'Auto-crop borders', 'Rotate 90 / 180', 'Rotate (any angle)', 'Flip', 'Auto straighten', 'Perspective correction', 'Lens distortion'],
    'Adjustments': ['Brightness / contrast', 'Levels', 'Curves', 'Auto contrast', 'Local contrast (CLAHE)', 'Even out illumination', 'Hue / saturation', 'Colour balance', 'White balance', 'Portrait retouch', 'Grayscale', 'Invert', 'Threshold (black & white)'],
    'Filters': ['Sharpen (unsharp mask)', 'Blur (Gaussian)', 'Reduce noise', 'Morphology', 'Find edges'],
    'Clean-up': ['Erase / keep colours', 'Remove marks', 'Remove object (AI)', 'Remove object (generative)', 'Despeckle', 'Flatten ink to one colour', 'Join dashed lines'],
}          # the order of the groups and of the tools in the lists; a group of a plug-in comes after them, in registration order
TOOL_NOTES = {}                     # name -> a sentence a tool leaves about what it just did (shown after Apply)


class Param:
    """One adjustable value of a tool. kind: 'int', 'float', 'bool', 'choice' (then `choices` lists the
    options and `default` is one of them), 'colors' (text with hex colours such as '#ff0000 #00ff00') or 'text'.
    `slider` adds a slider next to the number box. `pick` adds a button that fills the value by clicking on
    the image: 'color' (appends the colour clicked), 'color1' (a single colour, also with a colour dialog) or
    'faces' (a text list of faces 'x y width; ...': Find / Mark / Clear buttons, Ctrl+click adds one), 'angle' (two clicks on a line that
    should be level) or 'point' (one click sets this int and the one named `<key>` with
    x/y swapped: used by the clone source, src_x / src_y)."""

    def __init__(self, key, label, kind, default, lo=None, hi=None, step=1, choices=(), slider=False, pick=None,
                 progressive=False):
        self.key, self.label, self.kind, self.default = key, label, kind, default
        self.lo, self.hi, self.step, self.choices, self.slider = lo, hi, step, tuple(choices), slider
        self.pick = pick
        self.progressive = progressive      # slider position -> value is quadratic: fine steps at the low end, coarse at the high end

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
                 layer_geometry=None, layer_shift=None, external=False, takes_region=False):
        self.name, self.group, self.func, self.params = name, group, func, tuple(params)
        self.geometry, self.preview, self.doc, self.interactive, self.place = geometry, preview, doc, interactive, place
        self.layer = layer
        self.layer_geometry = layer_geometry          # None, or how it may transform one layer: 'exact' / 'resample'
        self.layer_shift = layer_shift                # None, or f(params, old shape) -> (dx, dy): the layers move with the canvas
        self.external = external                      # runs another program and can take minutes: the editor runs it in the background
        self.takes_region = takes_region              # the function gets the working area as `region=` (it IS the input, e.g. the object to remove)


def tool(name, group, params=(), geometry=False, preview=True, doc="", interactive=None, place=False,
         layer=None, layer_geometry=None, layer_shift=None, external=False, takes_region=False):
    """Register `func(img, **params) -> img` as a tool. `img` is an (H, W, 3) uint8 array that must not be
    modified in place (it belongs to the Undo history); return a new array, or `img` itself for 'no change'.
    The keys of `params` are the keyword arguments."""
    def deco(func):
        TOOLS[name] = Tool(name, group, func, params, geometry, preview, doc or (func.__doc__ or "").strip(),
                           interactive, place, layer, layer_geometry, layer_shift, external, takes_region)
        return func
    return deco


# the toolbox: the mouse tools as icon buttons (a group per line of two), then the selection commands and the Hand
TOOLBOX = [
    ("Select", ["Rectangle", "Magic wand", "Lasso", "Hand"]),
    ("Draw", ["Pencil", "Paintbrush", "Eraser brush", "Fill bucket", "Gradient"]),
    ("Retouch", ["Clone stamp", "Clean brush", "Dodge / burn", "Erase rectangle", "Move area"]),
    ("Annotate", ["Add text", "Shapes", "Scale bar", "Measure"]),
    ("Layers", ["Move layer"]),
]
TOOLBOX_MODES = {"Rectangle": 'area', "Magic wand": 'wand', "Lasso": 'lasso'}      # buttons that start a selection mode, not a tool
GROUP_SHORT = {"Size and orientation": "Size", "Adjustments": "Adjust", "Clean-up": "Clean-up", "Filters": "Filters"}


def search_tools(query, tools=None):
    """Names of the tools that match `query` (every word of it is somewhere in the tool's name or group, without regard to case), the
    ones whose name starts with the first word first, then in the order of the list. An empty query matches nothing."""
    words = query.lower().split()
    if not words:
        return []
    tools = TOOLS if tools is None else tools
    hits = [t for t in tools.values() if all(w in (t.name + " " + t.group).lower() for w in words)]
    hits.sort(key=lambda t: (not t.name.lower().startswith(words[0]), 0))
    return [t.name for t in hits]


def tool_icon(name, size=26):
    """A small RGBA picture for a toolbox button, drawn here (no image files): a pencil, a brush, a bucket ... 4 x larger, then shrunk,
    so the lines are smooth. An unknown name gets a rounded square with its first letters."""
    S = size * 4
    u = S / 26.0
    im = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ink, acc, soft = (44, 52, 66, 255), (31, 111, 235, 255), (170, 180, 196, 255)
    w = max(2, int(round(2.0 * u)))
    P = lambda *xy: [v * u for v in xy]
    line = lambda xy, col=ink, wd=None: d.line(P(*xy), fill=col, width=wd or w)
    poly = lambda xy, fill=None, outline=ink: d.polygon(P(*xy), fill=fill, outline=outline)
    box = lambda xy, fill=None, outline=ink, wd=None: d.rectangle(P(*xy), fill=fill, outline=outline, width=wd or w)
    dash = lambda a, b: [line((a[0] + (b[0] - a[0]) * t / 8.0, a[1] + (b[1] - a[1]) * t / 8.0,
                               a[0] + (b[0] - a[0]) * (t + 0.55) / 8.0, a[1] + (b[1] - a[1]) * (t + 0.55) / 8.0)) for t in range(8)]
    if name == "Pencil":
        line((7, 19, 20, 6), ink, int(4.5 * u))
        poly((4, 23, 6, 17, 9, 20), acc, acc)
    elif name in ("Paintbrush", "Clean brush"):
        line((22, 3, 12, 13), ink, int(3 * u))
        d.ellipse(P(4, 13, 14, 23), fill=acc)
        if name == "Clean brush":
            for cx, cy in ((19, 16), (22, 11), (16, 21)):
                line((cx - 1.5, cy, cx + 1.5, cy), acc, int(1.6 * u))
                line((cx, cy - 1.5, cx, cy + 1.5), acc, int(1.6 * u))
    elif name == "Eraser brush":
        poly((4, 16, 13, 7, 22, 14, 12, 23), None, ink)
        poly((4, 16, 8.5, 11.5, 17.5, 18.5, 12, 23), acc, acc)
        line((4, 16, 13, 7, 22, 14, 12, 23, 4, 16))
    elif name == "Fill bucket":
        poly((4, 11, 13, 3, 20, 11, 12, 19), soft, ink)
        line((4, 11, 13, 3, 20, 11, 12, 19, 4, 11))
        d.ellipse(P(20, 16, 24, 22), fill=acc)
        poly((22, 12, 20, 17, 24, 17), acc, acc)
    elif name == "Gradient":
        for i in range(12):
            g = int(40 + i * 17)
            d.rectangle(P(3 + i * 20 / 12.0, 5, 3 + (i + 1) * 20 / 12.0, 21), fill=(g, g, g, 255))
        box((3, 5, 23, 21), None, ink)
    elif name == "Dodge / burn":
        d.ellipse(P(3, 3, 23, 23), fill=(235, 238, 244, 255), outline=ink, width=w)
        d.pieslice(P(3, 3, 23, 23), 90, 270, fill=ink)
        d.ellipse(P(3, 3, 23, 23), outline=ink, width=w)
    elif name == "Clone stamp":
        d.ellipse(P(8.5, 3, 17.5, 12), fill=ink)
        box((11, 11, 15, 16), ink, ink)
        box((5, 16, 21, 21), acc, acc)
    elif name == "Erase rectangle":
        for a, b in (((4, 5), (22, 5)), ((22, 5), (22, 21)), ((22, 21), (4, 21)), ((4, 21), (4, 5))):
            dash(a, b)
        line((9, 10, 17, 17), acc)
        line((17, 10, 9, 17), acc)
    elif name == "Move area":
        for a, b in (((3, 4), (16, 4)), ((16, 4), (16, 17)), ((16, 17), (3, 17)), ((3, 17), (3, 4))):
            dash(a, b)
        line((12, 12, 22, 22), acc)
        poly((23, 23, 23, 17, 17, 23), acc, acc)
    elif name == "Add text":
        line((5, 5.5, 21, 5.5), ink, int(2.6 * u))
        line((13, 5.5, 13, 22), ink, int(2.6 * u))
        line((9.5, 22, 16.5, 22), ink, int(2.2 * u))
    elif name == "Shapes":
        box((3, 11, 14, 22), soft, ink)
        d.ellipse(P(10, 3, 23, 16), outline=acc, width=w)
    elif name == "Scale bar":
        line((3, 15, 23, 15), ink, int(3 * u))
        line((3, 10, 3, 20))
        line((23, 10, 23, 20))
        for x in (9, 13, 17):
            line((x, 8, x, 12), acc, int(1.6 * u))
    elif name == "Measure":
        poly((3, 18, 17, 4, 23, 10, 9, 24), soft, ink)
        line((3, 18, 17, 4, 23, 10, 9, 24, 3, 18))
        for tick in ((6, 15, 8, 17), (9, 12, 11, 14), (12, 9, 14, 11)):
            line(tick, acc, int(1.5 * u))
    elif name == "Move layer":
        box((3, 9, 15, 21), soft, ink)
        box((10, 3, 22, 15), None, acc)
    elif name == "Rectangle":
        for a, b in (((4, 5), (22, 5)), ((22, 5), (22, 21)), ((22, 21), (4, 21)), ((4, 21), (4, 5))):
            dash(a, b)
        for cx, cy in ((4, 5), (22, 5), (22, 21), (4, 21)):
            box((cx - 2, cy - 2, cx + 2, cy + 2), acc, acc)
    elif name == "Magic wand":
        line((4, 22, 15, 11), ink, int(3 * u))
        for a in ((19, 2, 19, 12), (14, 7, 24, 7), (15.5, 3.5, 22.5, 10.5), (22.5, 3.5, 15.5, 10.5)):
            line(a, acc, int(1.7 * u))
    elif name == "Lasso":
        d.ellipse(P(3, 3, 22, 15), outline=acc, width=w)
        line((8, 15, 6, 20, 10, 23), ink)
        d.ellipse(P(9, 21, 13, 25), fill=ink)
    elif name == "Hand":
        for x0, top in ((6, 6), (10, 3.5), (14, 4.5), (18, 7.5)):
            d.rounded_rectangle(P(x0, top, x0 + 3.6, 15), radius=1.8 * u, fill=soft, outline=ink, width=int(1.4 * u))
        d.rounded_rectangle(P(5, 12, 22, 23), radius=4 * u, fill=soft, outline=ink, width=int(1.4 * u))
        line((5.5, 17, 2, 13.5), ink)
    else:
        d.rounded_rectangle(P(3, 3, 23, 23), radius=4 * u, outline=ink, width=w)
        d.text((S / 2, S / 2), "".join(x[0] for x in name.split()[:2]).upper(), fill=ink, anchor='mm', font=ImageFont.load_default(int(11 * u)))
    return im.resize((size, size), Image.LANCZOS)


# (section, keys, what it does, how it compares with other programs): the table of Help > Keyboard shortcuts and mouse
SHORTCUTS = [
    ("Files", "Ctrl+O", "Open a picture or a project", "standard"),
    ("Files", "Ctrl+S / Ctrl+Shift+S", "Save (project, keeps layers) / Save as", "standard"),
    ("Files", "Ctrl+E", "Export (format, quality, size, dpi)", "like GIMP"),
    ("Files", "Ctrl+V", "Paste a picture from the clipboard as a layer", "standard"),
    ("Undo and selection", "Ctrl+Z / Ctrl+Y", "Undo / Redo (Ctrl+Shift+Z too)", "standard"),
    ("Undo and selection", "Ctrl+A / Ctrl+D", "Select all / Deselect", "standard"),
    ("Undo and selection", "Ctrl+I", "Invert the selection", "like GIMP (Photoshop: Ctrl+Shift+I)"),
    ("Undo and selection", "Ctrl+J / Ctrl+Shift+J", "Copy / cut the selection to a new layer", "like Photoshop"),
    ("Undo and selection", "Ctrl+Shift+X", "Crop to the selection", "ours"),
    ("Undo and selection", "Ctrl+Enter", "Apply the tool", "ours"),
    ("Undo and selection", "Ctrl+K", "Find a tool by typing part of its name", "like the command palette of many editors"),
    ("Undo and selection", "Esc", "Cancel what is in progress (pick, lasso, measure, gradient); otherwise deselect", "standard"),
    ("Undo and selection", "Arrow keys / Shift+arrows", "Move the rectangle selection by 1 / 10 px (click the picture first)", "like Photoshop"),
    ("View", "Ctrl+0 / Ctrl+1", "Fit to window / actual size (100 %)", "like Photoshop"),
    ("View", "Ctrl++ / Ctrl+-", "Zoom in / out", "standard"),
    ("View", "Mouse wheel", "Zoom around the pointer", "like Krita and Figma (Photoshop and GIMP scroll)"),
    ("View", "Shift + wheel", "Scroll the picture sideways", "standard"),
    ("View", "H", "Hand on / off (the left button moves the picture)", "standard"),
    ("View", "Space + drag", "Move the picture, with any tool (not while typing in a box)", "standard"),
    ("View", "Right or middle button + drag", "Move the picture", "standard"),
    ("View", "Hand button in the toolbox", "The left button moves the picture", "standard (tool)"),
    ("View", "Ctrl+H", "Histogram window", "ours"),
    ("Selections with the mouse", "Drag", "Draw a rectangle; drag inside it to move it, drag a handle to resize", "standard"),
    ("Selections with the mouse", "Shift + click", "Magic wand and Lasso: add to the selection", "standard"),
    ("Selections with the mouse", "Ctrl + click", "Magic wand and Lasso: remove from the selection", "like GIMP (Photoshop uses Alt)"),
    ("Selections with the mouse", "Shift + Ctrl + click", "Magic wand and Lasso: keep only the overlap", "like GIMP"),
    ("Selections with the mouse", "Double-click / Enter / first point", "Close the lasso (also: end a path or area measurement)", "standard"),
    ("Selections with the mouse", "Ctrl / Shift + drag in a selection", "With 'Drag moves the content': Ctrl does the opposite of the box, Shift copies", "ours"),
    ("Drawing and retouch", "Ctrl + click", "Pick the foreground colour; Clone stamp: the source; Erase / keep colours: a colour to find", "like GIMP (Photoshop uses Alt)"),
    ("Drawing and retouch", "Ctrl + Shift + click", "Pick the background colour (or the colour to paint with)", "ours"),
    ("Drawing and retouch", "[ / ]", "A smaller / larger brush (Size box of the tool, about 10 % a step)", "standard"),
    ("Drawing and retouch", "Delete or Backspace / Shift + Delete", "Clear the selection (background colour, or transparent on a layer) / fill it with the foreground colour", "like Photoshop"),
    ("Drawing and retouch", "X / D", "Swap the foreground and background colours / back to black and white", "standard"),
    ("Drawing and retouch", "Shift + click", "Pencil and Paintbrush: a straight line from the end of the last stroke", "standard"),
    ("Shapes and measures", "Drag, then drag an end", "Shapes: draw, then move one end of the selected shape", "standard"),
    ("Shapes and measures", "Click, click, double-click", "Measure: the points of a path or an area", "standard"),
]


class MaskRegion(tuple):
    """A selection that is not a plain rectangle: the rectangle (x0, y0, x1, y1) that bounds it, as a tuple (so everything that
    only knows rectangles keeps working with it), plus `mask`, a float32 array 0..1 of the whole canvas (1 = selected, values in
    between = a feathered edge). Code that knows about masks uses `getattr(region, 'mask', None)`."""

    def __new__(cls, rect, mask):
        obj = tuple.__new__(cls, rect)
        obj.mask = mask
        return obj


def region_mask(region, shape):
    """The mask of a region when it is a MaskRegion whose mask fits the (h, w) of `shape`, else None."""
    m = getattr(region, 'mask', None)
    return m if m is not None and m.shape == tuple(shape[:2]) else None


def masked_merge(orig, new, region):
    """`new` where `region` selects, `orig` elsewhere: a plain rectangle pastes its box, a MaskRegion blends by its mask
    (so a feathered edge fades). Works on RGB and RGBA arrays of the same shape; returns `new` when region is None."""
    if region is None:
        return new
    x0, y0, x1, y1 = region
    m = region_mask(region, orig.shape)
    out = orig.copy()
    if m is None:
        out[y0:y1, x0:x1] = new[y0:y1, x0:x1]
        return out
    w = m[y0:y1, x0:x1][..., None]
    a, b = orig[y0:y1, x0:x1].astype(np.float32), new[y0:y1, x0:x1].astype(np.float32)
    out[y0:y1, x0:x1] = np.clip(np.rint(a + (b - a) * w), 0, 255).astype(np.uint8)
    return out


def apply_tool(name, img, params=None, region=None):
    """Run a registered tool. `region` = (x0, y0, x1, y1) limits a non-geometry tool to that rectangle (a MaskRegion: to its mask)."""
    t = TOOLS[name]
    values = {p.key: p.default for p in t.params}
    for p in t.params:
        if params and p.key in params:
            values[p.key] = p.coerce(params[p.key])
    if values.get('fill') == 'transparent':           # only a layer has transparency: pixels have to be white here
        values['fill'] = 'white'
    if t.takes_region:                                  # the tool uses the area itself (and leaves everything outside it as it is)
        return t.func(img, region=region, **values)
    out = t.func(img, **values)
    if region is not None and not t.geometry and out is not img:
        return masked_merge(img, out, region)
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


@tool("Brightness / contrast", "Adjustments", [
    Param('brightness', "Brightness", 'int', 0, -100, 100, slider=True),
    Param('contrast', "Contrast", 'int', 0, -100, 99, slider=True)])
def brightness_contrast(img, brightness, contrast):
    """Shift the brightness and stretch the contrast about mid-grey."""
    if brightness == 0 and contrast == 0:
        return img
    k = math.tan((contrast + 100) * math.pi / 400)          # 1 at 0, towards infinity at 100
    return _to_u8((img.astype(np.float32) - 128.0) * k + 128.0 + brightness * 2.55)


_FACE_CACHE = {}


def find_faces(img):
    """The faces in `img` as a list of (centre x, centre y, width) in pixels: OpenCV's Haar cascade for frontal faces, run on a copy shrunk
    to 800 px. Good for faces that look at the camera or turn a little; a face in profile, covered, tilted or very small may be missed
    (the Portrait retouch tool then lets you click on it). The answer is remembered for the last picture, because the live preview asks
    again at every slider move."""
    h, w = img.shape[:2]
    key = (h, w, int(img[::97, ::89].sum()))
    if key in _FACE_CACHE:
        return _FACE_CACHE[key]
    s = min(1.0, 800.0 / max(h, w))
    g = cv2.equalizeHist(cv2.cvtColor(cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY))
    found = []
    try:
        for name in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
            casc = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if casc.empty():
                continue
            r = casc.detectMultiScale(g, 1.1, 5, minSize=(max(24, int(0.04 * g.shape[1])),) * 2)
            if len(r):
                found = [(float(fx + fw / 2) / s, float(fy + fh / 2) / s, float(fw) / s) for fx, fy, fw, fh in r]
                if name.endswith("default.xml"):                         # the looser cascade makes false hits: keep only the biggest
                    found = [max(found, key=lambda f: f[2])]
                break
    except Exception:
        found = []
    _FACE_CACHE.clear()
    _FACE_CACHE[key] = found
    return found


def _soft_ellipse(shape, box, cx, cy, rx, ry, edge=0.5):
    """A float map (the size of `box` = (x0, y0, x1, y1)) that is 1 inside the ellipse and falls to 0 over `edge` (fraction of the radius)."""
    ys, xs = np.mgrid[box[1]:box[3], box[0]:box[2]].astype(np.float32)
    d = np.hypot((xs - cx) / rx, (ys - cy) / ry)
    return np.clip((1.0 + edge - d) / edge, 0.0, 1.0)


def portrait_retouch_faces(img, faces, smooth=40, light=15, surround=25, warmth=0):
    """Portrait finish for the faces `faces` ((cx, cy, width) in px): the skin is smoothed (bilateral filter, only where the colour is
    skin, so eyes, lips, hair and glasses stay sharp), the light on the face is lifted (dodge on the midtones), the warmth of the skin
    shifted, and everything away from the faces darkened with a wide soft pool of light round them. Returns `img` itself when every
    amount is 0 or there are no faces."""
    if not faces or (smooth == 0 and light == 0 and surround == 0 and warmth == 0):
        return img
    h, w = img.shape[:2]
    out = img
    for cx, cy, fw in faces:
        if smooth or light or warmth:
            pad = int(1.2 * fw)
            x0, y0, x1, y1 = max(0, int(cx - pad)), max(0, int(cy - pad)), min(w, int(cx + pad)), min(h, int(cy + pad))
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            roi = out[y0:y1, x0:x1]
            box = (x0, y0, x1, y1)
            face = _soft_ellipse(roi.shape, box, cx, cy + 0.10 * fw, 0.60 * fw, 0.78 * fw)
            ycc = cv2.cvtColor(roi, cv2.COLOR_RGB2YCrCb).astype(np.float32)
            skin = np.clip(1.0 - np.maximum(np.abs(ycc[..., 1] - 150.0) - 26.0, np.abs(ycc[..., 2] - 112.0) - 26.0) / 14.0, 0.0, 1.0)
            skin = cv2.GaussianBlur(skin, (0, 0), max(1.0, 0.012 * fw))
            wgt = face * skin
            f = roi.astype(np.float32)
            if smooth:
                bil = cv2.bilateralFilter(roi, 0, 26, max(2.0, 0.011 * fw)).astype(np.float32)
                f = f + (bil - f) * (0.85 * smooth / 100.0) * wgt[..., None]
            if warmth:
                f = f + np.array([0.4, 0.0, -0.4], np.float32) * warmth * wgt[..., None]
            part = _to_u8(f)
            if light:
                part = tone_paint(part, _soft_ellipse(roi.shape, box, cx, cy + 0.05 * fw, 0.62 * fw, 0.80 * fw, 0.9), 'dodge (lighten)', 'midtones', light)
            out = out.copy() if out is img else out
            out[y0:y1, x0:x1] = part
    if surround:
        k = max(1, int(max(h, w) / 400))                                # the pool is smooth: build it on a small copy
        sh, sw = max(2, h // k), max(2, w // k)
        pool = np.zeros((sh, sw), np.float32)
        for cx, cy, fw in faces:
            pool = np.maximum(pool, _soft_ellipse(pool.shape, (0, 0, sw, sh), cx / k, (cy + 0.9 * fw) / k, 1.9 * fw / k, 2.6 * fw / k, 0.9))
        dark = cv2.resize(cv2.GaussianBlur(1.0 - pool, (0, 0), 3), (w, h), interpolation=cv2.INTER_LINEAR)
        out = tone_paint(out, dark, 'burn (darken)', 'all', surround)
    return out


def parse_faces(text, default_width=0.0, shape=None):
    """The faces written in `text` as a list of (centre x, centre y, width): one face per `;` or line, as 'x y width' (commas or spaces;
    a missing or zero width is `default_width`, or 22 % of the shorter side of `shape` = (h, w, ...) when that is 0). Entries that cannot be
    read are skipped."""
    out = []
    for part in str(text).replace("\n", ";").split(";"):
        nums = []
        for tok in part.replace(",", " ").split():
            try:
                nums.append(float(tok))
            except ValueError:
                nums = []
                break
        if len(nums) < 2:
            continue
        wd = nums[2] if len(nums) > 2 and nums[2] > 0 else float(default_width)
        if wd <= 0 and shape is not None:
            wd = 0.22 * min(shape[0], shape[1])
        if wd > 0:
            out.append((nums[0], nums[1], wd))
    return out


def format_faces(faces):
    """The text form of a list of faces, as `parse_faces` reads it: '440 526 190; 120 80 150'."""
    return "; ".join("%d %d %d" % (round(x), round(y), round(w)) for x, y, w in faces)


@tool("Portrait retouch", "Adjustments", [
    Param('faces', "Faces (x y width; one per face; empty = find them)", 'text', "", pick='faces'),
    Param('size', "Width for a face written without one (px; 0 = automatic)", 'int', 0, 0, 20000),
    Param('smooth', "Smooth the skin (%)", 'int', 40, 0, 100, slider=True),
    Param('light', "Light on the face (%)", 'int', 15, 0, 60, slider=True),
    Param('surround', "Darken the surroundings (%)", 'int', 25, 0, 80, slider=True),
    Param('warmth', "Warmth of the skin", 'int', 0, -50, 50, slider=True)],
    doc="One-step portrait finish. With the list of faces empty it finds the faces that look at the camera and treats each one; it can "
        "miss some (a profile, a tilted, partly cut or small face). Press 'Find faces' to write what it found in the list, then 'Mark "
        "faces' and click on the ones it missed (Ctrl+click does the same without the button); edit the list by hand to remove or move a "
        "face. The skin is smoothed only where the colour is skin (eyes, lips, hair and glasses stay sharp), the face gets a little "
        "light, and the rest of the picture is darkened round a wide soft pool of light, which brings the eye to the faces. Warmth shifts "
        "the skin towards orange (+) or blue (-). Keep the amounts low: a natural face still has texture. Limited to the selection if "
        "there is one.")
def portrait_retouch(img, faces, size, smooth, light, surround, warmth):
    if smooth == 0 and light == 0 and surround == 0 and warmth == 0:
        return img
    listed = parse_faces(faces, size, img.shape)
    if listed:
        found = listed
        TOOL_NOTES.pop("Portrait retouch", None)
    else:
        found = find_faces(img)
        if size > 0:
            found = [(fx, fy, float(size)) for fx, fy, fw in found]
        TOOL_NOTES["Portrait retouch"] = ("%d face%s found" % (len(found), "" if len(found) == 1 else "s")) if found else \
            "no face found: press 'Mark faces' and click on each one"
    return portrait_retouch_faces(img, found, smooth, light, surround, warmth)


@tool("Grayscale", "Adjustments", [
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


@tool("Invert", "Adjustments")
def invert(img):
    """Negative: every value v becomes 255 - v."""
    return 255 - img


# ---- tone and colour ------------------------------------------------------------------------------------------
CHANNELS = ('RGB', 'Red', 'Green', 'Blue')


def polyline_length(pts):
    """Length of the path through `pts` ((x, y) pairs), in the units of the points."""
    p = np.asarray(pts, np.float64)
    return float(np.hypot(*np.diff(p, axis=0).T).sum()) if len(p) > 1 else 0.0


def polygon_area(pts):
    """Area of the polygon through `pts` (shoelace formula; the sign of the orientation is dropped)."""
    p = np.asarray(pts, np.float64)
    if len(p) < 3:
        return 0.0
    x, y = p[:, 0], p[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)


def angle_between(a, vertex, b):
    """The angle at `vertex` between the points a and b, in degrees (0..180); 0 when a point coincides with the vertex."""
    u, v = np.asarray(a, np.float64) - vertex, np.asarray(b, np.float64) - vertex
    n = float(np.hypot(*u) * np.hypot(*v))
    if n == 0:
        return 0.0
    return float(math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(u, v)) / n)))))


def format_measure(value, kind, scale=None):
    """Text of a measurement: `value` is in pixels (a length), square pixels (an area) or degrees (an angle). With a scale
    (units per pixel, unit) a length or an area is given in those units as well."""
    if kind == 'angle':
        return "%.1f deg" % value
    sq = kind == 'area'
    base = "%.1f %s" % (value, "px2" if sq else "px")
    if not scale:
        return base
    v = value * scale[0] ** (2 if sq else 1)
    return "%s = %.4g %s%s" % (base, v, scale[1], "2" if sq else "")


def apply_lut(img, lut, channel='RGB'):
    """Map the values of `img` through `lut` (256 uint8 values): all channels, or only the Red / Green / Blue one."""
    if channel == 'RGB':
        return cv2.LUT(img, lut)
    out = img.copy()
    k = CHANNELS.index(channel) - 1
    out[..., k] = lut[img[..., k]]
    return out


def levels_lut(in_black, in_white, gamma, out_black, out_white):
    """The 256-value table of a Levels adjustment: the input range [in_black, in_white] is stretched to 0..1, raised to
    1 / gamma (gamma > 1 brightens the midtones) and mapped to [out_black, out_white]."""
    x = np.arange(256, dtype=np.float32)
    t = np.clip((x - in_black) / max(1.0, float(in_white - in_black)), 0.0, 1.0) ** (1.0 / max(gamma, 1e-3))
    return _to_u8(out_black + t * (out_white - out_black))


@tool("Levels", "Adjustments", [
    Param('channel', "Channel", 'choice', 'RGB', choices=CHANNELS),
    Param('in_black', "Input black point", 'int', 0, 0, 254, slider=True),
    Param('in_white', "Input white point", 'int', 255, 1, 255, slider=True),
    Param('gamma', "Midtones (gamma)", 'float', 1.0, 0.1, 5.0, step=0.01, slider=True),
    Param('out_black', "Output black", 'int', 0, 0, 255, slider=True),
    Param('out_white', "Output white", 'int', 255, 0, 255, slider=True)],
    doc="Slide the input black / white points towards the ends of the histogram to stretch the contrast; gamma above 1 lightens "
        "the midtones, below 1 darkens them. The Output values lower the contrast the other way. Choose a single channel to "
        "correct a colour cast. View > Histogram shows where the values lie.")
def levels(img, channel, in_black, in_white, gamma, out_black, out_white):
    if in_white <= in_black:
        in_white = in_black + 1
    if (in_black, in_white, out_black, out_white) == (0, 255, 0, 255) and abs(gamma - 1.0) < 1e-6:
        return img
    return apply_lut(img, levels_lut(in_black, in_white, gamma, out_black, out_white), channel)


def monotone_curve(xs, ys):
    """The 256 values of the smooth monotone curve (Fritsch-Carlson cubic) through the points (xs, ys): it never overshoots
    between points, so a curve through rising points always rises."""
    xs, ys = np.asarray(xs, np.float64), np.asarray(ys, np.float64)
    h, d = np.diff(xs), np.diff(ys) / np.diff(xs)
    m = np.zeros(len(xs))
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(xs) - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else 2.0 / (1.0 / d[i - 1] + 1.0 / d[i])
    x = np.arange(256, dtype=np.float64)
    idx = np.clip(np.searchsorted(xs, x, side='right') - 1, 0, len(xs) - 2)
    t = (x - xs[idx]) / h[idx]
    h00, h10, h01, h11 = 2 * t ** 3 - 3 * t ** 2 + 1, t ** 3 - 2 * t ** 2 + t, -2 * t ** 3 + 3 * t ** 2, t ** 3 - t ** 2
    y = h00 * ys[idx] + h10 * h[idx] * m[idx] + h01 * ys[idx + 1] + h11 * h[idx] * m[idx + 1]
    return _to_u8(y)


@tool("Curves", "Adjustments", [
    Param('channel', "Channel", 'choice', 'RGB', choices=CHANNELS),
    Param('black', "Black ends at", 'int', 0, 0, 255, slider=True),
    Param('shadows', "Shadows (input 64) becomes", 'int', 64, 0, 255, slider=True),
    Param('mid', "Midtones (input 128) becomes", 'int', 128, 0, 255, slider=True),
    Param('high', "Highlights (input 192) becomes", 'int', 192, 0, 255, slider=True),
    Param('white', "White ends at", 'int', 255, 0, 255, slider=True)],
    doc="A smooth tone curve through five points (input 0, 64, 128, 192, 255): raise a point to lighten that part of the range, "
        "lower it to darken it. A gentle S (shadows down, highlights up) adds contrast without clipping.")
def curves(img, channel, black, shadows, mid, high, white):
    if (black, shadows, mid, high, white) == (0, 64, 128, 192, 255):
        return img
    return apply_lut(img, monotone_curve([0, 64, 128, 192, 255], [black, shadows, mid, high, white]), channel)


@tool("Auto contrast", "Adjustments", [
    Param('clip', "Ignore the darkest and lightest (%)", 'float', 0.5, 0.0, 10.0, step=0.1, slider=True),
    Param('per_channel', "Each colour channel on its own (also removes a colour cast)", 'bool', False)],
    doc="Stretches the values so that the darkest and the lightest ends of the picture reach black and white. By default one "
        "stretch is used for the three colours (keeps the colours); 'each channel' fixes a colour cast but can change hues. "
        "The ends are measured over the whole picture, not only the working area.")
def auto_contrast(img, clip, per_channel):
    f = img.astype(np.float32)
    chans = [f[..., k] for k in range(3)] if per_channel else [f]
    out = f.copy()
    for k, ch in enumerate(chans):
        lo, hi = np.percentile(ch, [clip, 100.0 - clip])
        if hi - lo < 1.0:
            continue
        scaled = (ch - lo) * (255.0 / (hi - lo))
        if per_channel:
            out[..., k] = scaled
        else:
            out = scaled
    return img if np.array_equal(_to_u8(out), img) else _to_u8(out)


@tool("Local contrast (CLAHE)", "Adjustments", [
    Param('clip_limit', "Strength", 'float', 2.0, 0.5, 10.0, step=0.1, slider=True),
    Param('tiles', "Tiles across (more = more local)", 'int', 8, 2, 32, slider=True)],
    doc="Adaptive histogram equalisation of the lightness: brings out detail in dim and washed-out parts of a picture (microscopy, "
        "gels, scans) without blowing out the rest. Raise the strength carefully: noise grows with it.")
def clahe(img, clip_limit, tiles):
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
    lab[..., 0] = cv2.createCLAHE(clipLimit=float(clip_limit), tileGridSize=(int(tiles), int(tiles))).apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


@tool("Even out illumination", "Adjustments", [
    Param('radius', "Scale of the uneven light (px)", 'int', 80, 5, 600, slider=True, progressive=True),
    Param('strength', "Strength (%)", 'int', 100, 0, 100, slider=True),
    Param('mode', "Correct by", 'choice', 'divide', choices=('divide', 'subtract'))],
    doc="Removes a slow brightness gradient (a vignette, an unevenly lit scan or microscope field): the large-scale light is "
        "estimated with a very wide blur and taken out, keeping the colours. Set the scale larger than the objects of "
        "interest. 'divide' suits a bright background; 'subtract' a dark one.")
def even_illumination(img, radius, strength, mode):
    if strength == 0:
        return img
    h, w = img.shape[:2]
    k = max(1.0, radius / 12.0)                                     # the blur is done on a small copy: same look, far faster
    small = cv2.resize(img, (max(1, int(w / k)), max(1, int(h / k))), interpolation=cv2.INTER_AREA).astype(np.float32)
    bg = cv2.GaussianBlur(small, (0, 0), max(0.5, radius / k), borderType=cv2.BORDER_REFLECT)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_CUBIC)
    lum = bg.mean(axis=2, keepdims=True)
    f = img.astype(np.float32)
    if mode == 'divide':
        out = f * (float(lum.mean()) / np.maximum(lum, 1.0))
    else:
        out = f - (lum - float(lum.mean()))
    return _to_u8(f + (out - f) * (strength / 100.0))


@tool("Hue / saturation", "Adjustments", [
    Param('hue', "Hue shift (deg)", 'int', 0, -180, 180, slider=True),
    Param('saturation', "Saturation (%)", 'int', 0, -100, 100, slider=True),
    Param('lightness', "Lightness (%)", 'int', 0, -100, 100, slider=True)],
    doc="Rotates the hues, scales the colour intensity (-100 = grey, +100 = twice as intense) and lightens or darkens towards "
        "white or black.")
def hue_saturation(img, hue, saturation, lightness):
    if (hue, saturation, lightness) == (0, 0, 0):
        return img
    hsv = cv2.cvtColor(img, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[..., 0] = (hsv[..., 0] + hue / 2.0) % 180.0
    hsv[..., 1] = np.clip(hsv[..., 1] * (1.0 + saturation / 100.0), 0, 255)
    out = cv2.cvtColor(_to_u8(hsv), cv2.COLOR_HSV2RGB).astype(np.float32)
    if lightness > 0:
        out = out + (255.0 - out) * (lightness / 100.0)
    elif lightness < 0:
        out = out * (1.0 + lightness / 100.0)
    return _to_u8(out)


@tool("Colour balance", "Adjustments", [
    Param('tones', "Tones to change", 'choice', 'midtones', choices=('shadows', 'midtones', 'highlights', 'all')),
    Param('red', "Cyan  <->  Red", 'int', 0, -100, 100, slider=True),
    Param('green', "Magenta  <->  Green", 'int', 0, -100, 100, slider=True),
    Param('blue', "Yellow  <->  Blue", 'int', 0, -100, 100, slider=True)],
    doc="Shifts the colour of the shadows, midtones or highlights (or everything) towards red / green / blue or their opposites "
        "cyan / magenta / yellow. A small move is enough: a colour cast of the whole picture is best removed with White balance.")
def colour_balance(img, tones, red, green, blue):
    if (red, green, blue) == (0, 0, 0):
        return img
    f = img.astype(np.float32)
    y = (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]) / 255.0
    w = {'shadows': (1.0 - y) ** 2, 'highlights': y ** 2, 'midtones': 1.0 - np.abs(2.0 * y - 1.0), 'all': np.ones_like(y)}[tones]
    shift = np.array([red, green, blue], np.float32) * 0.6
    return _to_u8(f + w[..., None] * shift)


@tool("White balance", "Adjustments", [
    Param('method', "Method", 'choice', 'picked neutral', choices=('picked neutral', 'gray world', 'brightest areas')),
    Param('neutral', "Neutral colour (Pick or Ctrl+click on grey / white)", 'colors', '', pick='color1'),
    Param('strength', "Strength (%)", 'int', 100, 0, 100, slider=True)],
    doc="Removes a colour cast. 'picked neutral': click on something that should be grey or white and every colour is scaled "
        "so that it becomes neutral. 'gray world' assumes the picture averages to grey; 'brightest areas' assumes the lightest "
        "pixels are white. Without a picked colour the first method does nothing.")
def white_balance(img, method, neutral, strength):
    f = img.astype(np.float32)
    if method == 'picked neutral':
        cols = parse_colors(neutral)
        if not cols:
            return img
        ref = np.asarray(cols[0], np.float32)
        target = float(ref.mean())
    elif method == 'gray world':
        ref = f.reshape(-1, 3).mean(axis=0)
        target = float(ref.mean())
    else:
        ref = np.percentile(f.reshape(-1, 3), 99.0, axis=0).astype(np.float32)
        target = float(ref.max())
    gains = target / np.maximum(ref, 1.0)
    gains = 1.0 + (gains - 1.0) * (strength / 100.0)
    if np.allclose(gains, 1.0, atol=1e-3):
        return img
    return _to_u8(f * gains)


def histogram_counts(img, mask=None):
    """(3, 256) counts of the values of each channel; `mask` (bool, same height and width) restricts it."""
    px = img[mask] if mask is not None else img.reshape(-1, 3)
    return np.stack([np.bincount(px[:, k], minlength=256) for k in range(3)]) if len(px) else np.zeros((3, 256), np.int64)


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


@tool("Reduce noise", "Filters", [
    Param('method', "Method", 'choice', 'non-local means', choices=('median', 'bilateral', 'non-local means')),
    Param('strength', "Strength", 'int', 8, 1, 50, slider=True),
    Param('radius', "Radius (px, median and bilateral)", 'int', 2, 1, 15, slider=True)],
    doc="median: removes salt-and-pepper specks and keeps edges (radius). bilateral: smooths flat areas but not edges (radius sets "
        "the reach, strength how different two colours may be and still be mixed). non-local means: the best for grainy photos "
        "and microscope pictures, slowest (strength = how much grain to remove; too much gives a waxy look).")
def reduce_noise(img, method, strength, radius):
    if method == 'median':
        return cv2.medianBlur(img, 2 * int(radius) + 1)
    if method == 'bilateral':
        return cv2.bilateralFilter(img, 2 * int(radius) + 1, float(strength) * 4.0, float(radius) * 2.0 + 1.0)
    return cv2.fastNlMeansDenoisingColored(img, None, float(strength), float(strength), 7, 21)


MORPH_OPS = {'dilate': cv2.MORPH_DILATE, 'erode': cv2.MORPH_ERODE, 'open': cv2.MORPH_OPEN, 'close': cv2.MORPH_CLOSE,
             'outline (gradient)': cv2.MORPH_GRADIENT, 'light details (top-hat)': cv2.MORPH_TOPHAT,
             'dark details (black-hat)': cv2.MORPH_BLACKHAT}


@tool("Morphology", "Filters", [
    Param('op', "Operation", 'choice', 'open', choices=tuple(MORPH_OPS)),
    Param('radius', "Size (px)", 'int', 1, 1, 50, slider=True),
    Param('shape', "Shape", 'choice', 'ellipse', choices=('ellipse', 'square', 'cross'))],
    doc="Works on the values of the pixels, colour by colour: dilate grows the light parts (and thins dark lines), erode grows the dark "
        "ones (and thickens dark lines on white); open removes light specks smaller than the size, close fills dark gaps; "
        "outline gives the edges of shapes; top-hat / black-hat keep only the light / dark details smaller than the size "
        "(handy to even out uneven backgrounds before thresholding).")
def morphology(img, op, radius, shape):
    kind = {'ellipse': cv2.MORPH_ELLIPSE, 'square': cv2.MORPH_RECT, 'cross': cv2.MORPH_CROSS}[shape]
    return cv2.morphologyEx(img, MORPH_OPS[op], cv2.getStructuringElement(kind, (2 * int(radius) + 1,) * 2))


@tool("Find edges", "Filters", [
    Param('method', "Method", 'choice', 'Canny', choices=('Canny', 'Sobel', 'Laplacian')),
    Param('blur', "Smooth first (px)", 'float', 1.0, 0.0, 10.0, step=0.1, slider=True),
    Param('low', "Canny: low threshold", 'int', 50, 0, 500, slider=True),
    Param('high', "Canny: high threshold", 'int', 150, 0, 500, slider=True),
    Param('dark_on_light', "Dark edges on a white page", 'bool', True)],
    doc="Turns the picture into its edges. Canny gives thin one-pixel lines (the high threshold decides which edges start a line, "
        "the low one how far it follows them); Sobel and Laplacian give soft edge strength. Smooth first to ignore fine grain.")
def find_edges(img, method, blur, low, high, dark_on_light):
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if blur > 0:
        g = cv2.GaussianBlur(g, (0, 0), blur)
    if method == 'Canny':
        e = cv2.Canny(g, int(min(low, high)), int(max(low, high)))
    elif method == 'Sobel':
        e = np.clip(np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1)), 0, 255).astype(np.uint8)
    else:
        e = np.clip(np.abs(cv2.Laplacian(g, cv2.CV_32F)) * 2.0, 0, 255).astype(np.uint8)
    e = 255 - e if dark_on_light else e
    return np.ascontiguousarray(np.repeat(e[..., None], 3, axis=2))


def estimate_skew(img, max_angle=10.0, method='projection'):
    """The angle (degrees, counter-clockwise, the sign the Rotate tool wants) that levels the lines of text or rules in `img`;
    0 when nothing line-like is found. 'projection': the rotation that makes the dark pixels pile up in the fewest rows (works
    for text, tables, scans); 'hough': the median direction of the long straight edges (works for pictures with a few strong lines)."""
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    k = 900.0 / max(g.shape)
    if k < 1:
        g = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    if method == 'hough':
        edges = cv2.Canny(cv2.GaussianBlur(g, (0, 0), 1.0), 50, 150)
        lines = cv2.HoughLinesP(edges, 1, math.pi / 720, threshold=60, minLineLength=max(g.shape) // 4, maxLineGap=8)
        if lines is None:
            return 0.0
        ang, wts = [], []
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            a = math.degrees(math.atan2(y2 - y1, x2 - x1))
            a = a - 180 if a > 90 else a + 180 if a <= -90 else a
            a = a - 90 if a > 45 else a + 90 if a < -45 else a                 # a vertical line levels like a horizontal one
            if abs(a) <= max_angle:
                ang.append(a)
                wts.append(math.hypot(x2 - x1, y2 - y1))
        if not ang:
            return 0.0
        order = np.argsort(ang)
        cum = np.cumsum(np.asarray(wts)[order])
        return float(np.asarray(ang)[order][int(np.searchsorted(cum, cum[-1] / 2.0))])      # the length-weighted median
    _, ink = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if ink.mean() > 127:                                    # light ink on a dark page
        ink = 255 - ink
    ink = ink.astype(np.float32)
    if not ink.any():
        return 0.0
    h, w = ink.shape
    c = (w / 2.0, h / 2.0)

    def score(a):
        rows = cv2.warpAffine(ink, cv2.getRotationMatrix2D(c, a, 1.0), (w, h)).sum(axis=1)
        return float(np.sum(np.diff(rows) ** 2))

    best, step = 0.0, max(0.5, max_angle / 10.0)
    cand = np.arange(-max_angle, max_angle + 1e-9, step)
    best = float(cand[int(np.argmax([score(a) for a in cand]))])
    while step > 0.02:
        step /= 3.0
        cand = best + np.array([-2, -1, 0, 1, 2]) * step
        best = float(cand[int(np.argmax([score(a) for a in cand]))])
    return round(best, 2)


@tool("Auto straighten", "Size and orientation", [
    Param('method', "Look at", 'choice', 'lines of text or rules', choices=('lines of text or rules', 'straight edges')),
    Param('max_angle', "Largest tilt to look for (deg)", 'float', 10.0, 1.0, 45.0, step=0.5, slider=True),
    Param('expand', "Enlarge canvas to fit", 'bool', True),
    Param('fill', "Fill the corners with", 'choice', 'white', choices=('white', 'black', 'edge'))],
    geometry=True,
    doc="Finds how much the page or picture is tilted and rotates it level. 'lines of text or rules' suits scans of text, tables and "
        "figures with horizontal lines; 'straight edges' suits pictures with a few strong straight lines (a horizon, a frame). "
        "For a tilt you can measure yourself use Rotate (any angle) and its measuring button. The angle found is shown after Apply.")
def auto_straighten(img, method, max_angle, expand, fill):
    angle = estimate_skew(img, max_angle, 'hough' if method == 'straight edges' else 'projection')
    TOOL_NOTES["Auto straighten"] = "tilt found and removed: %.2f deg" % angle
    if abs(angle) < 0.05:
        return img
    return rotate_free(img, angle, expand, fill)


def order_corners(pts):
    """The four points ordered top-left, top-right, bottom-right, bottom-left (whatever order they were clicked in)."""
    p = np.asarray(pts, np.float64).reshape(4, 2)
    s, d = p.sum(axis=1), p[:, 1] - p[:, 0]
    return np.array([p[np.argmin(s)], p[np.argmin(d)], p[np.argmax(s)], p[np.argmax(d)]])


def perspective_warp(img, corners, out_w=0, out_h=0):
    """The quadrilateral with the given four corners (any order) stretched to a rectangle: a photographed page or a slide seen
    at an angle becomes square-on. The size is that of the longer sides unless `out_w` / `out_h` say otherwise."""
    src = order_corners(corners).astype(np.float32)
    w = out_w or int(round(max(np.hypot(*(src[1] - src[0])), np.hypot(*(src[2] - src[3])))))
    h = out_h or int(round(max(np.hypot(*(src[3] - src[0])), np.hypot(*(src[2] - src[1])))))
    w, h = max(2, w), max(2, h)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], np.float32)
    return cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)


@tool("Perspective correction", "Size and orientation", [
    Param('tl_x', "Corner 1 X  (use the button to click all four)", 'int', 0, -100000, 100000, pick='quad'),
    Param('tl_y', "Corner 1 Y", 'int', 0, -100000, 100000),
    Param('tr_x', "Corner 2 X", 'int', 0, -100000, 100000),
    Param('tr_y', "Corner 2 Y", 'int', 0, -100000, 100000),
    Param('br_x', "Corner 3 X", 'int', 0, -100000, 100000),
    Param('br_y', "Corner 3 Y", 'int', 0, -100000, 100000),
    Param('bl_x', "Corner 4 X", 'int', 0, -100000, 100000),
    Param('bl_y', "Corner 4 Y", 'int', 0, -100000, 100000),
    Param('out_w', "Result width (px, 0 = automatic)", 'int', 0, 0, 100000),
    Param('out_h', "Result height (px, 0 = automatic)", 'int', 0, 0, 100000)],
    geometry=True,
    doc="Press the button and click the four corners of the page, slide or object that looks distorted (in any order): the area "
        "inside them is stretched to a rectangle. The corners can be corrected in the boxes afterwards; a yellow outline shows "
        "them on the picture. With every box at 0 nothing changes.")
def perspective_correction(img, tl_x, tl_y, tr_x, tr_y, br_x, br_y, bl_x, bl_y, out_w, out_h):
    q = [(tl_x, tl_y), (tr_x, tr_y), (br_x, br_y), (bl_x, bl_y)]
    if not any(v for pt in q for v in pt):
        return img
    return perspective_warp(img, q, out_w, out_h)


def canvas_size(img, left, top, right, bottom, fill='white', color=None):
    """The picture with `left`, `top`, `right`, `bottom` pixels added round it (negative = cut off). The new area is white, black,
    a copy of the nearest edge pixels or the first colour of `color`."""
    h, w = img.shape[:2]
    nh, nw = h + top + bottom, w + left + right
    if nh < 1 or nw < 1 or (left, top, right, bottom) == (0, 0, 0, 0):          # a cut that would leave nothing does nothing
        return img
    if fill == 'edge':
        pad = cv2.copyMakeBorder(img, max(0, top), max(0, bottom), max(0, left), max(0, right), cv2.BORDER_REPLICATE)
    else:
        rgb = {'white': (255, 255, 255), 'black': (0, 0, 0)}.get(fill) or _picked_rgb(color)
        pad = cv2.copyMakeBorder(img, max(0, top), max(0, bottom), max(0, left), max(0, right), cv2.BORDER_CONSTANT, value=rgb)
    x0, y0 = max(0, -left), max(0, -top)
    return np.ascontiguousarray(pad[y0:y0 + nh, x0:x0 + nw])


# ---- AI enlargement with programs that are already on the computer (Real-ESRGAN, waifu2x) ------------------------------------------
AI_DIR = None                       # a folder that holds the programs (or their folders); set from the settings, IMAGEFORGE_AI_DIR also works
AI_CANCEL = threading.Event()       # set to stop a running enlargement
_popen = subprocess.Popen           # the tests put a fake in here
AI_METHODS = {                      # label -> (program, model folder, the scale the model makes by itself)
    "Real-ESRGAN photo (4x, most detail)": ('realesrgan', 'realesrgan-x4plus', 4),
    "waifu2x photo (2x, gentler)": ('waifu2x', 'models-upconv_7_photo', 2),
    "waifu2x drawings (2x, cunet)": ('waifu2x', 'models-cunet', 2),
}
AI_PROGRAMS = {'realesrgan': 'realesrgan-ncnn-vulkan', 'waifu2x': 'waifu2x-ncnn-vulkan'}


class ToolUnavailable(RuntimeError):
    """A tool needs a program that is not there (the message says which and what to do)."""


class Cancelled(RuntimeError):
    """The user stopped a running tool."""


def set_ai_dir(path):
    global AI_DIR
    AI_DIR = path or None


def _ai_spots(prefix, folder=None):
    """The folders where something called `prefix...` may be: `folder`, IMAGEFORGE_AI_DIR and AI_DIR themselves, and the folders inside them whose name
    starts with `prefix` (one level down)."""
    spots = []
    for d in (folder, os.environ.get("IMAGEFORGE_AI_DIR"), AI_DIR):
        if not d or not os.path.isdir(d):
            continue
        spots.append(d)
        try:
            spots += [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.lower().startswith(prefix) and os.path.isdir(os.path.join(d, n))]
        except OSError:
            pass
    return spots


def find_ai_program(kind, folder=None):
    """Path of the executable of `kind` ('realesrgan' or 'waifu2x'), or None. Looked for in `folder`, in IMAGEFORGE_AI_DIR and in AI_DIR, each
    either the program's own folder or a folder that contains it one level down, then on the PATH."""
    base = AI_PROGRAMS[kind]
    for sp in _ai_spots(base, folder):
        for n in (base + ".exe", base):
            f = os.path.join(sp, n)
            if os.path.isfile(f):
                return f
    return shutil.which(base)


def find_ai_model(prefix, folder=None):
    """Path of a model file `<prefix>*.onnx` (for example 'lama' -> lama_fp32.onnx) in the same places as the programs, or None."""
    for sp in _ai_spots(prefix, folder):
        try:
            names = sorted(n for n in os.listdir(sp) if n.lower().startswith(prefix) and n.lower().endswith(".onnx"))
        except OSError:
            continue
        if names:
            return os.path.join(sp, names[0])
    return None


def ai_command(method, exe, src, dst, scale, denoise):
    """The command line that runs `exe` for `method` (a key of AI_METHODS). Real-ESRGAN always makes the model's own scale (a 2x result is that
    reduced by half afterwards, which looks better than the 2x of the x4 model); waifu2x makes the scale asked and removes noise at level `denoise`."""
    kind, model, native = AI_METHODS[method]
    folder = os.path.dirname(exe)
    if kind == 'realesrgan':
        return [exe, "-i", src, "-o", dst, "-n", model, "-s", str(native), "-m", os.path.join(folder, "models")]
    return [exe, "-i", src, "-o", dst, "-m", os.path.join(folder, model), "-n", str(int(denoise)), "-s", str(int(scale))]


def ai_enlarge(img, method, scale, denoise=1, folder=None):
    """`img` ((h, w, 3) RGB) enlarged `scale` times (2 or 4) by an external AI program, or ToolUnavailable if it is not found. Runs the program
    on a temporary PNG and waits for it, polling AI_CANCEL (set it from another thread to stop: Cancelled). A GPU with Vulkan is needed;
    on a laptop's integrated GPU expect minutes per megapixel."""
    kind, model, native = AI_METHODS[method]
    exe = find_ai_program(kind, folder)
    if exe is None:
        raise ToolUnavailable("%s was not found. Tools > AI programs folder... tells ImageForge where the programs are."
                              % {'realesrgan': "Real-ESRGAN (realesrgan-ncnn-vulkan)", 'waifu2x': "waifu2x (waifu2x-ncnn-vulkan)"}[kind])
    scale = int(scale)
    h, w = img.shape[:2]
    with tempfile.TemporaryDirectory(prefix="imageforge_ai_", ignore_cleanup_errors=True) as tmp:
        src, dst, err = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.png"), os.path.join(tmp, "err.txt")
        Image.fromarray(np.ascontiguousarray(img)).save(src)
        cmd = ai_command(method, exe, src, dst, scale, denoise)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with open(err, "w") as ef:
            proc = _popen(cmd, cwd=os.path.dirname(exe), stdout=subprocess.DEVNULL, stderr=ef, creationflags=flags)
            while proc.poll() is None:
                if AI_CANCEL.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)                                # let go of the files before the temporary folder is removed
                    except Exception:
                        pass
                    raise Cancelled("stopped")
                time.sleep(0.1)
        if proc.returncode != 0 or not os.path.isfile(dst):
            try:
                tail = open(err, errors="replace").read().strip().splitlines()[-3:]
            except OSError:
                tail = []
            raise RuntimeError("%s failed (code %s)%s" % (os.path.basename(exe), proc.returncode, ": " + " | ".join(tail) if tail else ""))
        out = Image.open(dst).convert("RGB")
        want = (w * scale, h * scale)
        if out.size != want:                                         # Real-ESRGAN x4 asked for 2x: reduce, with a good filter
            out = out.resize(want, Image.LANCZOS)
        return np.asarray(out, dtype=np.uint8).copy()


@tool("Enlarge (AI)", "Size and orientation", [
    Param('method', "Program and model", 'choice', next(iter(AI_METHODS)), choices=tuple(AI_METHODS)),
    Param('scale', "Enlarge", 'choice', '2x', choices=('2x', '4x')),
    Param('denoise', "Remove noise (waifu2x only, 0-3)", 'int', 1, 0, 3)],
    geometry=True, preview=False, external=True,
    doc="Makes the picture 2 or 4 times larger with a neural network that invents plausible detail, instead of just stretching the pixels. "
        "It runs Real-ESRGAN or waifu2x, programs that must already be on this computer (Tools > AI programs folder... tells ImageForge "
        "where), and needs a GPU with Vulkan. It can take many minutes (about 2.5 minutes for 1 megapixel x4 on an integrated Intel GPU): "
        "ImageForge keeps working meanwhile and Esc stops it. Real-ESRGAN gives the most detail but can make skin look waxy; waifu2x is "
        "gentler. The new detail is invented, not recovered: do not use it on pictures that must stay faithful to the original (measurements).")
def enlarge_ai(img, method, scale, denoise):
    TOOL_NOTES.pop("Enlarge (AI)", None)
    out = ai_enlarge(img, method, int(scale[0]), denoise)
    TOOL_NOTES["Enlarge (AI)"] = "%dx%d -> %dx%d px" % (img.shape[1], img.shape[0], out.shape[1], out.shape[0])
    return out


# ---- removing an object with a neural network (LaMa, ONNX) ---------------------------------------------------------------------------------
_LAMA = {}                                  # model path -> onnxruntime session (loading takes a second or two)


def region_bool(region, shape):
    """The working area `region` (rectangle or MaskRegion) as a bool mask of the picture, or None when there is none."""
    if region is None:
        return None
    h, w = shape[:2]
    m = region_mask(region, shape)
    if m is not None:
        return m > 0.5
    x0, y0, x1, y1 = region
    out = np.zeros((h, w), bool)
    out[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = True
    return out


def _lama_session(path):
    """The onnxruntime session of the LaMa model at `path` (CPU), kept for the next call; ToolUnavailable when onnxruntime is missing."""
    if path not in _LAMA:
        try:
            import onnxruntime as ort
        except ImportError:
            raise ToolUnavailable("onnxruntime is not installed: run  pip install onnxruntime  (in the Python that runs ImageForge).")
        _LAMA[path] = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    return _LAMA[path]


def _lama_fill(sess, img, mask, grain=True):
    """`img` with the bool `mask` rebuilt by LaMa. The model works on 512 x 512 pictures, so a square crop around the hole (1.3 x its size, at
    least 256 px) is shrunk to that, filled, enlarged again and pasted back only where the mask is (soft edge). With `grain`, noise of the
    level found in a ring round the hole is added to the fill, because the model's output is smoother than a photograph."""
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    h, w = img.shape[:2]
    S = int(min(max(max(x1 - x0, y1 - y0) * 1.3, 256), min(h, w)))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    X0, Y0 = int(np.clip(cx - S // 2, 0, w - S)), int(np.clip(cy - S // 2, 0, h - S))
    crop = img[Y0:Y0 + S, X0:X0 + S]
    mc = mask[Y0:Y0 + S, X0:X0 + S].astype(np.uint8)
    a = cv2.resize(crop, (512, 512), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    mm = (cv2.resize(mc, (512, 512), interpolation=cv2.INTER_NEAREST) > 0).astype(np.float32)
    out = sess.run(None, {"image": a.transpose(2, 0, 1)[None], "mask": mm[None, None]})[0][0].transpose(1, 2, 0)
    out = np.clip(out if out.max() > 2 else out * 255.0, 0, 255)
    big = cv2.resize(out, (S, S), interpolation=cv2.INTER_CUBIC).astype(np.float32)
    if grain:
        k = max(3, int(0.04 * S) | 1)
        ring = (cv2.dilate(mc, np.ones((k, k), np.uint8)) > 0) & (mc == 0)
        if ring.sum() > 50:
            hp = crop.astype(np.float32) - cv2.GaussianBlur(crop.astype(np.float32), (0, 0), 1.5)
            sd = float(hp[ring].std())
            noise = np.random.default_rng(1).normal(0.0, sd * 0.8, big.shape).astype(np.float32)
            big = big + cv2.GaussianBlur(noise, (0, 0), 0.6)
    soft = cv2.GaussianBlur(mc.astype(np.float32), (0, 0), max(1.5, S / 512.0 * 1.5))[..., None]
    soft = np.maximum(soft, mc[..., None].astype(np.float32))          # the hole itself is always the model's
    res = img.copy()
    res[Y0:Y0 + S, X0:X0 + S] = np.clip(crop * (1 - soft) + big * soft, 0, 255).astype(np.uint8)
    return res


def lama_remove(img, region, grow=12, grain=True, model=None):
    """`img` with the area `region` (rectangle or MaskRegion) removed and rebuilt from its surroundings by the LaMa model (`model` = path of the .onnx
    file, else a lama*.onnx found in the AI programs folder). Every separate piece of the selection is filled on its own, after widening it by
    `grow` % of its size: the widening matters, because the fringe of a transparent or shiny object (a bottle's green tint and reflection) is
    outside the outline you draw and the model would copy it. ValueError when there is no selection; ToolUnavailable when the model is missing."""
    mask = region_bool(region, img.shape)
    if mask is None or not mask.any():
        raise ValueError("Select the object first (Lasso, Magic wand or Rectangle), then press Apply.")
    path = model or find_ai_model("lama")
    if path is None or not os.path.isfile(path):
        raise ToolUnavailable("The LaMa model (lama_fp32.onnx) was not found. Put it in the AI programs folder and use Tools > AI programs folder...")
    sess = _lama_session(path)
    h, w = img.shape[:2]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    order = sorted(range(1, n), key=lambda i: -stats[i, cv2.CC_STAT_AREA])
    out, done = img, 0
    for i in order:
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < 4:
            continue
        g = max(3, int(round(grow / 100.0 * max(bw, bh))))
        X0, Y0, X1, Y1 = max(0, x - g - 2), max(0, y - g - 2), min(w, x + bw + g + 2), min(h, y + bh + g + 2)
        piece = (lab[Y0:Y1, X0:X1] == i).astype(np.uint8)
        piece = cv2.dilate(piece, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * g + 1, 2 * g + 1))) > 0
        full = np.zeros((h, w), bool)
        full[Y0:Y1, X0:X1] = piece
        out = _lama_fill(sess, out, full, grain)
        done += 1
    TOOL_NOTES["Remove object (AI)"] = "%d piece%s removed" % (done, "" if done == 1 else "s")
    return out if done else img


@tool("Remove object (AI)", "Clean-up", [
    Param('grow', "Widen the selection by (% of the object's size)", 'int', 12, 0, 60, slider=True),
    Param('grain', "Add the picture's grain to the fill", 'bool', True)],
    preview=False, external=True, takes_region=True,
    doc="Removes what is inside the working area and rebuilds it from the surroundings with the LaMa neural network (a 'magic eraser'). Select the "
        "object first with the Lasso, the Magic wand or the Rectangle (several separate pieces are removed one after the other), then press Apply: it "
        "takes about ten seconds per piece on the CPU and the window stays usable (Esc stops it). Widening the selection by about 10-15 % is "
        "important for transparent or shiny things (a bottle, glass, a reflection): their tint and reflection lie outside the outline. The model "
        "works at 512 x 512 on a crop around the object, so the fill is a little smoother than the photograph (the grain option adds it back); "
        "what is invented is plausible, not true: do not use it on pictures that must stay faithful. Needs the LaMa model (lama*.onnx in the AI "
        "programs folder) and the Python package onnxruntime.")
def remove_object(img, grow, grain, region=None):
    return lama_remove(img, region, grow, grain)


# ---- removing an object with a generative model through OpenRouter (Gemini image models; paid, per use) ------------------------------------------
OPENROUTER_API = "https://openrouter.ai/api/v1"
_urlopen = urllib.request.urlopen              # the tests put a fake here
GEN_MODELS = ("google/gemini-2.5-flash-image", "google/gemini-3.1-flash-image")
GEN_IMAGE_TOKENS = {"google/gemini-2.5-flash-image": 1290, "google/gemini-3.1-flash-image": 1120}   # output tokens of one ~1 megapixel picture, as measured
GEN_INPUT_TOKENS = 1400                       # what the request itself costs in input tokens (text + the picture), as measured
GEN_FALLBACK_PRICES = {"google/gemini-2.5-flash-image": (3e-7, 3e-5), "google/gemini-3.1-flash-image": (5e-7, 6e-5)}     # (input, image output) USD per token
GEN_MAX_SIDE = 1024                           # the crop sent is shrunk to this: the model hands back about one megapixel anyway
OR_STATE = {'credit': None, 'prices': {}}     # the credit as far as we know (USD), and the prices read from OpenRouter's public list


def openrouter_key():
    """The OpenRouter key: the environment variable OPENROUTER_API_KEY, else (Windows) the one in the user's profile, so a key set after the program
    started is found too. None when there is none. The key is never shown, logged or stored by this program."""
    k = os.environ.get("OPENROUTER_API_KEY")
    if not k and sys.platform == 'win32':
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                k = winreg.QueryValueEx(h, "OPENROUTER_API_KEY")[0]
        except OSError:
            k = None
    return k.strip() if k and k.strip() else None


def or_call(path, key=None, body=None, timeout=60):
    """A call to the OpenRouter API (GET, or POST when `body` is given); the decoded JSON. RuntimeError with a plain message, never containing the key."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(OPENROUTER_API + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with _urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        raise RuntimeError("OpenRouter answered %s%s" % (e.code, ": " + msg if msg else ""))
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError("Could not reach OpenRouter (%s)" % getattr(e, 'reason', e))


def or_credit(key):
    """The credit left on the account in USD (bought minus used). The server's count can lag behind by a few calls."""
    d = or_call("/credits", key, timeout=20)["data"]
    return float(d["total_credits"]) - float(d["total_usage"])


def gen_prices(model):
    """(USD per input token, USD per image-output token) of `model`, read from OpenRouter's public model list (no key needed) and remembered; the
    built-in figures when the list cannot be read."""
    if model not in OR_STATE['prices']:
        try:
            for m in or_call("/models", timeout=20)["data"]:
                pr = m.get("pricing", {})
                if m.get("id") in GEN_MODELS and pr.get("image_output") is not None:
                    OR_STATE['prices'][m["id"]] = (float(pr.get("prompt") or 0), float(pr["image_output"]))
        except Exception:
            pass
    return OR_STATE['prices'].get(model) or GEN_FALLBACK_PRICES.get(model, (5e-7, 6e-5))


def gen_estimate(model, calls=1):
    """The cost in USD of `calls` requests with `model`: the image tokens of a ~1 MP answer plus the input, at the current prices."""
    pin, pout = gen_prices(model)
    return calls * (GEN_IMAGE_TOKENS.get(model, 1290) * pout + GEN_INPUT_TOKENS * pin)


def gen_prompt(what):
    """The instruction sent with the crop. One object per request, described in words (the model gets no mask): the crop is centred on it."""
    return ("Edit this photo: remove %s (it is at the centre of the picture). Rebuild what would be behind it in a realistic way, continuing the "
            "surrounding surfaces, textures, light and shadows. Do not change anything else: keep the framing, the size, the colours, the light "
            "and every other detail exactly as they are." % what.strip().rstrip("."))


def gen_edit(crop, what, model, key):
    """One paid request: `crop` ((h, w, 3) RGB) goes to `model` with the instruction to remove `what`. Returns (the picture that comes back as RGB
    uint8, the cost in USD as reported by OpenRouter or None). RuntimeError when no picture comes back."""
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(crop)).save(buf, 'JPEG', quality=92)
    body = {"model": model, "modalities": ["image", "text"], "usage": {"include": True},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": gen_prompt(what)},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}]}]}
    r = or_call("/chat/completions", key, body, timeout=240)
    if r.get("error"):
        raise RuntimeError("OpenRouter: %s" % (r["error"].get("message") if isinstance(r["error"], dict) else r["error"]))
    msg = (r.get("choices") or [{}])[0].get("message", {})
    imgs = msg.get("images") or []
    if not imgs:
        raise RuntimeError("The model returned no picture%s" % (": " + str(msg.get("content"))[:160] if msg.get("content") else ""))
    out = np.asarray(Image.open(io.BytesIO(base64.b64decode(imgs[0]["image_url"]["url"].split(",", 1)[1]))).convert("RGB"))
    cost = (r.get("usage") or {}).get("cost")
    return out, (float(cost) if cost is not None else None)


def align_to(sent, gen):
    """The 2 x 3 affine matrix that maps pixel coordinates of `gen` onto those of `sent` (RGB arrays; ORB features + RANSAC), and the number of
    matches that agree with it; (None, 0) when there are too few. The model gives the picture back a little rescaled (here: x 0.90, a few pixels
    off), so it has to be put back in register before anything is pasted."""
    gray = lambda im: cv2.cvtColor(np.ascontiguousarray(im), cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(6000)
    ka, da = orb.detectAndCompute(gray(sent), None)
    kb, db = orb.detectAndCompute(gray(gen), None)
    if da is None or db is None:
        return None, 0
    ms = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(db, da), key=lambda m: m.distance)[:600]
    if len(ms) < 10:
        return None, 0
    M, inl = cv2.estimateAffine2D(np.float32([kb[m.queryIdx].pt for m in ms]), np.float32([ka[m.trainIdx].pt for m in ms]), ransacReprojThreshold=3.0)
    return (M, int(inl.sum())) if M is not None else (None, 0)


def gen_paste(crop, sent, gen, hole, grow_px, min_inliers=40):
    """`crop` with the generated picture `gen` (the answer to `sent`, a shrunk copy of `crop`) put in only where the bool mask `hole`, widened by
    `grow_px`, is: aligned to the crop, its colours matched to the crop in a ring round the hole, edge feathered. Everything outside stays as it
    was. RuntimeError when the answer cannot be aligned (the model changed the picture too much): nothing is pasted then."""
    M, n = align_to(sent, gen)
    if n < min_inliers:
        raise RuntimeError("The result could not be matched to the original (the model changed the picture too much): try again, or describe "
                           "the object differently.")
    ch, cw = crop.shape[:2]
    up = np.diag([cw / sent.shape[1], ch / sent.shape[0], 1.0]) @ np.vstack([M, [0, 0, 1]])
    warp = cv2.warpAffine(np.ascontiguousarray(gen), up[:2], (cw, ch), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT).astype(np.float32)
    mask = cv2.dilate(hole.astype(np.uint8) * 255, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(grow_px) + 1,) * 2))
    k = max(21, int(0.04 * max(ch, cw)) | 1)
    ring = (cv2.dilate(mask, np.ones((k, k), np.uint8)) > 0) & (mask == 0)
    o = crop.astype(np.float32)
    if ring.sum() > 50:
        for c in range(3):
            mo, so_ = o[..., c][ring].mean(), o[..., c][ring].std() + 1e-3
            mw, sw = warp[..., c][ring].mean(), warp[..., c][ring].std() + 1e-3
            warp[..., c] = (warp[..., c] - mw) * (so_ / sw) + mo
    soft = cv2.GaussianBlur(mask.astype(np.float32) / 255.0, (0, 0), max(3.0, 0.008 * max(ch, cw)))[..., None]
    return np.clip(o * (1 - soft) + warp * soft, 0, 255).astype(np.uint8)


def gen_pieces(img, region, grow=8):
    """The pieces a selection will be removed in, one request each: a list of dicts with the square crop (x0, y0, side), the mask of the piece in the
    crop (`hole`), the widening in pixels (`grow_px`) and the size of the picture that will be sent. A crop is 1.5 x the piece (plus the widening) so
    the model sees some surroundings but as little else as possible. ValueError when nothing is selected."""
    mask = region_bool(region, img.shape)
    if mask is None or not mask.any():
        raise ValueError("Select the object first (Lasso, Magic wand or Rectangle), then press Apply.")
    h, w = img.shape[:2]
    n, lab, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
    out = []
    for i in sorted(range(1, n), key=lambda i: -stats[i, cv2.CC_STAT_AREA]):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < 16:
            continue
        g = max(2, int(round(grow / 100.0 * max(bw, bh))))
        side = int(min(max(max(bw, bh) * 1.5 + 2 * g, 384), h, w))
        cx, cy = x + bw // 2, y + bh // 2
        x0, y0 = int(np.clip(cx - side // 2, 0, w - side)), int(np.clip(cy - side // 2, 0, h - side))
        sent = side if side <= GEN_MAX_SIDE else GEN_MAX_SIDE
        out.append(dict(x0=x0, y0=y0, side=side, hole=(lab[y0:y0 + side, x0:x0 + side] == i), grow_px=g, sent=sent))
    return out


def generative_remove(img, region, what, model=GEN_MODELS[0], grow=8, key=None, max_cost=None):
    """`img` with the object(s) of the selection removed by a generative model: for each separate piece a crop around it is sent to `model` through
    OpenRouter with the instruction to remove `what` (describe the object; several pieces get the same description), and the answer is aligned and
    pasted back only over the piece, so the rest of the picture is exactly the original. Costs money on every call. ValueError (nothing selected, no
    description), ToolUnavailable (no key), RuntimeError (the service or the alignment failed), Cancelled (AI_CANCEL set). With `max_cost` the
    work stops before a request that would take the total spent above it."""
    if not str(what).strip():
        raise ValueError("Describe what to remove (for example: the green water bottle).")
    pieces = gen_pieces(img, region, grow)
    key = key or openrouter_key()
    if not key:
        raise ToolUnavailable("No OpenRouter key: set the environment variable OPENROUTER_API_KEY (Help > Guide: removing objects explains how).")
    out, spent, calls, est = img, 0.0, 0, gen_estimate(model)
    for pc in pieces:
        if AI_CANCEL.is_set():
            raise Cancelled("stopped")
        if max_cost is not None and spent + est > max_cost:
            break
        x0, y0, S = pc['x0'], pc['y0'], pc['side']
        crop = out[y0:y0 + S, x0:x0 + S]
        sent = crop if S <= GEN_MAX_SIDE else cv2.resize(crop, (GEN_MAX_SIDE, GEN_MAX_SIDE), interpolation=cv2.INTER_AREA)
        gen, cost = gen_edit(sent, what, model, key)
        calls += 1
        c = cost if cost is not None else est
        spent += c
        if OR_STATE['credit'] is not None:
            OR_STATE['credit'] = max(0.0, OR_STATE['credit'] - c)
        out = out.copy() if out is img else out
        out[y0:y0 + S, x0:x0 + S] = gen_paste(crop, sent, gen, pc['hole'], pc['grow_px'])
    TOOL_NOTES["Remove object (generative)"] = "%d request%s, %.4f USD" % (calls, "" if calls == 1 else "s", spent)
    return out


@tool("Remove object (generative)", "Clean-up", [
    Param('what', "What to remove (describe it in words)", 'text', ""),
    Param('model', "Model", 'choice', GEN_MODELS[0], choices=GEN_MODELS),
    Param('grow', "Widen the selection by (% of the object's size)", 'int', 8, 0, 40, slider=True)],
    preview=False, external=True, takes_region=True,
    doc="COSTS MONEY (about 0.04 USD per object, from your OpenRouter credit). Select the object (Lasso, Magic wand or Rectangle), describe it in "
        "words, press Apply: a generative AI model redraws what was behind it. You see what will be sent and the cost, and nothing leaves the computer "
        "until you say yes; only a crop around the object is sent. One object per run. Press Guide... below for the steps, tips, money and privacy.")
def remove_object_generative(img, what, model, grow, region=None):
    return generative_remove(img, region, what, model, grow)


OBJECT_GUIDE = """REMOVING OBJECTS AND MARKS: WHICH TOOL, AND HOW

Four tools take things out of a picture. Pick the simplest one that works.

1. Clean brush (Retouch, in the toolbox) - free, instant.
   For a scratch, a speck, a stray mark or a thin line over a plain or smooth area. Paint over it; it is rebuilt from the edge of the hole.

2. Remove marks (Clean-up) - free, instant.
   For many small marks of one colour over a drawing (stars, stamps, dots on a figure). Pick the colour, press Apply.

3. Remove object (AI) (Clean-up) - free, on this computer, a few seconds per object.
   Good for plain backgrounds: a wall, a table, a floor, the sky, a cable. It continues what is around the hole. It cannot invent things (an arm
   or a hand behind the object comes out smooth) and the fill is a little softer than the photograph. Needs the LaMa model and onnxruntime
   (see the README). The first use of a session loads the model: about a minute.

4. Remove object (generative) (Clean-up) - COSTS MONEY, about 0.04 USD per object, a few seconds.
   For what the other tools cannot do: an object in front of something with structure (a bottle in front of an arm, a person in a crowd, a sign on a
   building). A generative model redraws what was behind it. On a test photo it kept the surroundings as they were and rebuilt a partly hidden
   hair band and an arm faithfully; but what it adds is always a plausible guess, so look at the result.

HOW TO USE "REMOVE OBJECT (GENERATIVE)"

1. Select the object: Lasso, Magic wand or Rectangle (the toolbox, top left). It does not need to be precise: it tells the program where the object is.
2. Describe it in the box "What to remove", in words, as you would to a person: 'the transparent green water bottle', 'the man in the red jacket'.
3. Press Apply. A window tells you what will be sent, the estimated cost and your OpenRouter credit. Nothing is sent until you press Yes.
4. Wait a few seconds. The window stays usable; Esc stops it. The result is one step of the history: Undo takes it back.
5. Look at the result. If it is wrong, Undo, change the description or the widening, and try again: each try costs again.

TIPS
- One object per run. The model gets your words, not the selection: with two different objects, run the tool twice. (Several pieces of the same
  kind, for instance three bottles, can be removed in one run; each piece is a separate request.)
- Be specific about which one: colour, size, where it is. 'the dark blue flask on the left, with the wooden lid' works better than 'the flask'.
- Widen the selection a little (the default 8 % is fine) so the glass, the shadow and the reflection of the object are included.
- A selection that is small compared with the picture works best: the crop sent is only 1.5 times the size of the object.
- Do not select faces or people unless you mean to send them: the crop goes to an outside service. The program tells you if it finds a face in the
  crop. A face in the crop is also something the model may redraw.
- The model gives the picture back a little rescaled; the program aligns it again and pastes only the area of the object. If it cannot be aligned
  you are told and nothing is pasted (the request has been paid, though).

MONEY AND PRIVACY
- Each request is charged by OpenRouter to your credit. The estimate comes from OpenRouter's published prices (about 0.039 USD for the default
  model, about 0.068 USD for the other); the real cost of each request is read from the answer and shown after it. The credit shown in the panel is
  what the program knows: the account's own count can lag behind by a few requests, so press 'Refresh credit' after a moment.
- The key is read from the environment variable OPENROUTER_API_KEY and is never shown, saved or logged by ImageForge. To set it in Windows
  (PowerShell, once; then restart ImageForge):
      [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY','your-key','User')
- What is sent: a JPEG crop of the area (at most 1024 px on the long side) and your description. OpenRouter passes it to the model provider.
  Pictures that must not leave the computer should be done with the free tools.

LIMITS
- What the model draws is invented: plausible, not true. Do not use it on pictures that must stay faithful (measurements, evidence).
- The new area has about the resolution of a 1 megapixel picture: on a large photo it can be a little softer than the rest.
- It works best with objects that are small compared with the picture. Removing a large part of the picture is not a job for this tool.
"""


@tool("Canvas size", "Size and orientation", [
    Param('left', "Add on the left (px, negative cuts)", 'int', 0, -20000, 20000),
    Param('top', "Add on top (px)", 'int', 0, -20000, 20000),
    Param('right', "Add on the right (px)", 'int', 0, -20000, 20000),
    Param('bottom', "Add on the bottom (px)", 'int', 0, -20000, 20000),
    Param('fill', "New area", 'choice', 'white', choices=('white', 'black', 'edge', 'picked colour')),
    Param('color', "Picked colour", 'colors', '#ffffff', pick='color1')],
    geometry=True, layer_shift=lambda p, shape: (p['left'], p['top']),
    doc="Makes the canvas bigger by adding a border (white, black, a copy of the edge, or a picked colour), or smaller by giving "
        "negative numbers. Layers keep their place on the picture.")
def canvas_size_tool(img, left, top, right, bottom, fill, color):
    return canvas_size(img, left, top, right, bottom, fill, color)


ASPECTS = {'1 : 1': (1, 1), '5 : 4': (5, 4), '4 : 3': (4, 3), '3 : 2': (3, 2), '16 : 9': (16, 9), '2 : 1': (2, 1),
           'golden (1.618 : 1)': (1.618, 1.0)}


def aspect_crop_box(w, h, rw, rh, anchor='centre'):
    """(x0, y0, x1, y1): the largest rectangle of aspect rw : rh inside w x h, placed at the centre or against the named side."""
    target = rw / float(rh)
    cw, ch = (w, int(round(w / target))) if w / float(h) < target else (int(round(h * target)), h)
    cw, ch = max(1, min(w, cw)), max(1, min(h, ch))
    x0 = {'left': 0, 'right': w - cw}.get(anchor, (w - cw) // 2)
    y0 = {'top': 0, 'bottom': h - ch}.get(anchor, (h - ch) // 2)
    return x0, y0, x0 + cw, y0 + ch


def _aspect_of(p):
    rw, rh = ASPECTS.get(p['ratio']) or (max(1, p['custom_w']), max(1, p['custom_h']))
    return (rh, rw) if p['portrait'] else (rw, rh)


@tool("Crop to aspect ratio", "Size and orientation", [
    Param('ratio', "Ratio (width : height)", 'choice', '3 : 2', choices=tuple(ASPECTS) + ('custom',)),
    Param('custom_w', "Custom: width part", 'int', 4, 1, 1000),
    Param('custom_h', "Custom: height part", 'int', 5, 1, 1000),
    Param('portrait', "Upright (swap the two numbers)", 'bool', False),
    Param('anchor', "Keep", 'choice', 'centre', choices=('centre', 'top', 'bottom', 'left', 'right'))],
    geometry=True,
    layer_shift=lambda p, shape: tuple(-v for v in aspect_crop_box(shape[1], shape[0], *_aspect_of(p), p['anchor'])[:2]),
    doc="Cuts the largest rectangle of the chosen proportions out of the picture, from the centre or from one side (a journal's "
        "figure shape, a square thumbnail). Layers keep their place.")
def crop_aspect(img, ratio, custom_w, custom_h, portrait, anchor):
    h, w = img.shape[:2]
    box = aspect_crop_box(w, h, *_aspect_of(dict(ratio=ratio, custom_w=custom_w, custom_h=custom_h, portrait=portrait)), anchor)
    return img if box == (0, 0, w, h) else crop(img, box)


def undistort_radial(img, k1, k2=0.0, zoom=100.0):
    """Radial lens distortion: every output pixel takes the picture at its distance r (0 at the centre, 1 at the corner) from the
    centre scaled by 1 + k1 r^2 + k2 r^4. k1 < 0 straightens the lines of a barrel-distorted (bulging) picture, k1 > 0 a
    pincushion one; `zoom` % enlarges the result to hide the empty edges this leaves."""
    h, w = img.shape[:2]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    norm = math.hypot(cx, cy)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = (xs - cx) / (norm * zoom / 100.0), (ys - cy) / (norm * zoom / 100.0)
    r2 = dx * dx + dy * dy
    f = 1.0 + k1 * r2 + k2 * r2 * r2
    return cv2.remap(img, (cx + dx * f * norm).astype(np.float32), (cy + dy * f * norm).astype(np.float32), cv2.INTER_CUBIC,
                     borderMode=cv2.BORDER_REPLICATE)


@tool("Lens distortion", "Size and orientation", [
    Param('k1', "Bend: minus = barrel (bulging), plus = pincushion", 'float', 0.0, -1.0, 1.0, step=0.01, slider=True),
    Param('k2', "Fine bend (edges)", 'float', 0.0, -1.0, 1.0, step=0.01, slider=True),
    Param('zoom', "Enlarge (%)", 'int', 100, 100, 200, slider=True)],
    geometry=True,
    doc="Straightens the bent lines of a wide-angle or cheap lens. Slide the first bar until a straight object near the edge of the "
        "picture looks straight, then enlarge a little to hide the stretched border.")
def lens_distortion(img, k1, k2, zoom):
    if k1 == 0 and k2 == 0 and zoom == 100:
        return img
    return undistort_radial(img, k1, k2, zoom)


@tool("Rotate 90 / 180", "Size and orientation", [
    Param('turn', "Turn", 'choice', '90 clockwise', choices=('90 clockwise', '90 counter-clockwise', '180'))],
    geometry=True, layer_geometry='exact')
def rotate_quarter(img, turn):
    """Exact quarter turns: no resampling, no loss."""
    k = {'90 clockwise': -1, '90 counter-clockwise': 1, '180': 2}[turn]
    return np.ascontiguousarray(np.rot90(img, k))


@tool("Rotate (any angle)", "Size and orientation", [
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


@tool("Flip", "Size and orientation", [Param('axis', "Axis", 'choice', 'horizontal', choices=('horizontal', 'vertical'))],
      geometry=True, layer_geometry='exact')
def flip(img, axis):
    """Mirror the image left-right (horizontal) or top-bottom (vertical)."""
    return np.ascontiguousarray(img[:, ::-1] if axis == 'horizontal' else img[::-1])


INTERPOLATIONS = {'nearest': cv2.INTER_NEAREST, 'linear': cv2.INTER_LINEAR, 'cubic': cv2.INTER_CUBIC,
                  'lanczos': cv2.INTER_LANCZOS4, 'area': cv2.INTER_AREA}


@tool("Resize", "Size and orientation", [
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


def inpaint_mask(img, hole, radius, valid=None):
    """Rebuild the `hole` pixels (bool mask) from their surroundings with Telea inpainting (cv2.inpaint): the colours
    flow in from the edge of the hole, so a line over a gradient or a photo disappears into it. With `valid` (bool mask)
    only those pixels are used as the source: the others are treated as holes too, but only next to the real one, so a
    transparent frame does not slow it down. Returns `img` itself when there is nothing to fill or nothing to copy from."""
    if not hole.any() or hole.all():
        return img
    mask = hole.copy()
    if valid is not None:
        reach = cv2.dilate(hole.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (4 * radius + 1,) * 2))
        mask |= (~valid) & (reach > 0)
        if (mask | ~valid).all():                       # no visible pixel left to learn from
            return img
    out = cv2.inpaint(np.ascontiguousarray(img), mask.astype(np.uint8) * 255, radius, cv2.INPAINT_TELEA)
    return img if np.array_equal(out, img) else out


@tool("Erase / keep colours", "Clean-up", [
    Param('colors', "Colours (click Pick on the image)", 'colors', '', pick='color'),
    Param('mode', "Mode", 'choice', 'erase', choices=('erase', 'keep only')),
    Param('tolerance', "Tolerance (RGB distance)", 'int', 60, 1, 441, slider=True),
    Param('grow', "Widen the area by (px)", 'int', 0, 0, 20),
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS) + ('picked colour', 'transparent', 'inpaint')),
    Param('fill_color', "Picked colour (to paint with)", 'colors', '#ffffff', pick='color1'),
    Param('radius', "Inpaint radius (px)", 'int', 3, 1, 30, slider=True)])
def erase_colors(img, colors, mode, tolerance, grow, fill, fill_color, radius):
    """Paint with the fill colour the pixels within the tolerance of any picked colour ('erase') or of none of them
    ('keep only') with white, black or 'picked colour' (Pick it from the picture, or choose it). 'inpaint' rebuilds them from their surroundings instead (a line over a gradient or a photo fades into
    it): widen the area by a pixel or two so that the pale edge of the line goes too, and keep the area narrow, since
    inpainting a wide hole invents smeared texture. Shades of one ink need several picks, or a larger tolerance."""
    cols = parse_colors(colors)
    if not cols:
        return img
    near = np.zeros(img.shape[:2], bool)
    for c in cols:
        near |= _distance_from(img, c) < tolerance
    hit = ~near if mode == 'keep only' else near
    if not hit.any():
        return img
    if grow > 0:
        hit = cv2.dilate(hit.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1,) * 2)) > 0
    if fill == 'inpaint':
        return inpaint_mask(img, hit, radius)
    out = img.copy()
    out[hit] = _picked_rgb(fill_color) if fill == 'picked colour' else FILLS[fill]
    return out


def _picked_rgb(color):
    cols = parse_colors(color or "")
    return cols[0] if cols else (255, 255, 255)


def _fit_stub(pts, roi, near, ratio):
    """A stub from the ink pixels `pts` (N x 2, x then y) that lead to a hole: entry point (their end nearest to the hole),
    unit direction pointing into the hole, width and colour; None when they are too few or not elongated (a letter, a dot)."""
    if len(pts) < 6:
        return None
    pts = pts.astype(np.float32)
    c = pts.mean(0)
    evals, evecs = np.linalg.eigh(np.cov((pts - c).T) + 1e-6 * np.eye(2))
    if evals[1] < ratio * evals[0]:
        return None
    d = evecs[:, 1]
    xi, yi = pts[:, 0].astype(int), pts[:, 1].astype(int)
    dist = near[yi, xi]
    e = pts[dist <= dist.min() + 1.5].mean(0)
    if np.dot(d, e - c) < 0:                                # from the body of the stub towards the hole
        d = -d
    proj = (pts - e) @ d
    width = max(1.0, len(pts) / max(float(proj.max() - proj.min()) + 1.0, 1.0))
    px = roi[yi, xi]
    dark = px[px.max(axis=1) <= np.percentile(px.max(axis=1), 60)]
    return dict(p=e, d=d, w=width, color=dark.mean(axis=0))


def _points_at(hole, p, d, limit):
    """True when the ray from p along d enters `hole` (bool mask) within `limit` px."""
    for t in range(1, int(limit)):
        x, y = int(round(p[0] + d[0] * t)), int(round(p[1] + d[1] * t))
        if not (0 <= y < hole.shape[0] and 0 <= x < hole.shape[1]):
            return False
        if hole[y, x]:
            return True
    return False


def _stubs(roi, ink, hole, others, reach, min_extent=0.0):
    """Dark strokes (stubs) that run into `hole` (one component): list of dicts (see `_fit_stub`). One stub per place where
    the ink crosses the rim of the hole (`near <= 3.5`); its direction comes from the ink within 0.8 x `reach` px of the
    crossing. Where two lines meet right at the hole (a divider joining a ring) that estimate is bent, so the ink further out
    (6 .. 1.3 x `reach` px from the hole), where the lines have come apart, is looked at too: if it falls into several strokes,
    or points another way, those are the stubs. Blobs (letters, round dots) are not elongated and are ignored, and so are
    strokes whose whole ink shape (the ink they are joined to) is smaller than `min_extent` px: the arm of a letter is not an
    outline that goes on under the mark."""
    near = cv2.distanceTransform((~hole).astype(np.uint8), cv2.DIST_L2, 3)
    free = ~others
    far_reach = reach * 1.3
    rim = (near <= 3.5) & ~hole & free
    zone = (near <= far_reach) & ~hole & free
    nr, lab_r = cv2.connectedComponents((ink & rim).astype(np.uint8), connectivity=8)
    nz, lab_z = cv2.connectedComponents((ink & zone).astype(np.uint8), connectivity=8)
    _, lab_all, shapes, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    out = []
    for i in range(1, nr):
        cross = lab_r == i
        if cross.sum() < 3:
            continue
        ey, ex = [float(v.mean()) for v in np.nonzero(cross)]
        mem = lab_z == lab_z[cross][0]
        yy, xx = np.nonzero(mem)
        whole = shapes[lab_all[yy[0], xx[0]]]
        if float(np.hypot(whole[cv2.CC_STAT_WIDTH], whole[cv2.CC_STAT_HEIGHT])) < min_extent:
            continue
        dd = np.hypot(xx - ex, yy - ey)
        pts = np.stack([xx, yy], 1)
        s_near = _fit_stub(pts[dd <= reach * 0.8], roi, near, 1.8)
        if s_near is not None:
            s_near['p'] = np.array([ex, ey])                # the crossing itself
        far = mem & (near >= 6)
        far[yy[dd > far_reach], xx[dd > far_reach]] = False
        nf, lab_f = cv2.connectedComponents(far.astype(np.uint8), connectivity=8)
        far_stubs = []
        for j in range(1, nf):
            fy, fx = np.nonzero(lab_f == j)
            st = _fit_stub(np.stack([fx, fy], 1), roi, near, 1.5)
            if st is not None and _points_at(hole, st['p'], st['d'], far_reach * 2):
                far_stubs.append(st)                        # (ink further out that does not point at the hole is something else)
        if len(far_stubs) >= 2 or (len(far_stubs) == 1 and (s_near is None or float(np.dot(s_near['d'], far_stubs[0]['d'])) < 0.9)):
            out.extend(far_stubs)                           # a junction near the hole (or a bent estimate): trust the far ink
        elif s_near is not None:
            out.append(s_near)
    return out


def _link_stubs(stubs, diag, corners=False, skip=()):
    """Links between stubs that are the two halves of one line, as (a, b, corner). With `corners` False only smooth ones
    (`corner` None): facing each other, close enough, similar width, shorter / straighter / more alike first. With `corners`
    True only corners (a cone's tip, a box corner hidden under the mark): `corner` is the point where the two lines would meet.
    Each stub is used once; `skip` lists stubs (by id) that are already taken."""
    cand = []
    for i in range(len(stubs)):
        for j in range(i + 1, len(stubs)):
            a, b = stubs[i], stubs[j]
            if id(a) in skip or id(b) in skip:
                continue
            wmax = max(a['w'], b['w'])
            v = b['p'] - a['p']
            dist = float(np.hypot(*v))
            if dist < 1 or dist > diag * 1.6 + 6:
                continue
            u = v / dist
            ca, cb = float(np.dot(a['d'], u)), float(np.dot(b['d'], -u))
            if not corners:
                if abs(a['w'] - b['w']) > 0.5 * wmax + 1.5 or ca < 0.55 or cb < 0.55:
                    continue
                cand.append((dist * (3.0 - ca - cb) * (1.0 + 2.0 * abs(a['w'] - b['w']) / wmax), i, j, None))
                continue
            if abs(a['w'] - b['w']) > 0.25 * wmax + 0.6:
                continue
            m = np.array([a['d'], -b['d']]).T                  # a.p + t a.d = b.p + s b.d
            if abs(np.linalg.det(m)) < 0.25:                    # nearly parallel: no corner
                continue
            t, s_ = np.linalg.solve(m, v)
            if 1 <= t <= diag * 1.3 and 1 <= s_ <= diag * 1.3:
                cand.append((2.0 * (t + s_), i, j, a['p'] + a['d'] * t))
    used, pairs = set(), []
    for _, i, j, corner in sorted(cand, key=lambda c: c[0]):
        if i not in used and j not in used:
            used |= {i, j}
            pairs.append((stubs[i], stubs[j], corner))
    return pairs


def _draw_stroke(shape, pts, width, ss=4):
    """Anti-aliased polyline (points x, y) of the given width on a float 0..1 plane of `shape`, drawn `ss` times larger."""
    layer = np.zeros((shape[0] * ss, shape[1] * ss), np.uint8)
    cv2.polylines(layer, [np.round((np.asarray(pts) + 0.5) * ss - 0.5).astype(np.int32)], False, 255, max(1, int(round(width * ss))), cv2.LINE_AA)
    return cv2.resize(layer, (shape[1], shape[0]), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0


def rebuild_under(img, hole, ink_below=100, reach=12):
    """Rebuild the `hole` pixels (bool mask) of a drawing made of fills and dark outlines, so what lay under a mark comes back:
    the COLOUR is inpainted from the fills around the hole with the outlines left out of it (no black smear into a shaded
    region), and every outline that enters the hole on one side is LINKED to the one that leaves on the other side with a
    smooth curve of the same width and colour, drawn on top. An outline that ends in the hole stays ended. Returns `img`
    itself when there is nothing to fill."""
    if not hole.any() or hole.all():
        return img
    h, w = hole.shape
    out = np.ascontiguousarray(img).copy()
    n, labels = cv2.connectedComponents(hole.astype(np.uint8), connectivity=8)
    ell = lambda r: cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1,) * 2)
    for comp in range(1, n):
        cur = labels == comp
        ys, xs = np.nonzero(cur)
        m = reach + 8
        y0, y1, x0, x1 = max(0, ys.min() - m), min(h, ys.max() + m + 1), max(0, xs.min() - m), min(w, xs.max() + m + 1)
        roi = out[y0:y1, x0:x1].copy()
        hc = cur[y0:y1, x0:x1]
        others = hole[y0:y1, x0:x1] & ~hc
        ink = (roi.max(axis=2) < ink_below) & ~hc & ~others
        inkd = cv2.dilate(ink.astype(np.uint8), ell(1)) > 0
        colour = cv2.inpaint(roi, ((inkd | hc | others).astype(np.uint8)) * 255, 3, cv2.INPAINT_TELEA)
        diag = float(np.hypot(ys.max() - ys.min() + 1, xs.max() - xs.min() + 1))
        stubs = _stubs(roi, ink, hc, others, reach)
        alpha = np.zeros(hc.shape, np.float32)
        paint = np.zeros(roi.shape, np.float32)
        def draw(a, b, corner):
            nonlocal alpha, paint
            if corner is None:
                dist = float(np.hypot(*(b['p'] - a['p'])))
                p0, p3 = a['p'] - a['d'] * 2, b['p'] - b['d'] * 2           # start a little outside the hole: no gap at the rim
                p1, p2 = a['p'] + a['d'] * dist / 3, b['p'] + b['d'] * dist / 3
                t = np.linspace(0, 1, 40)[:, None]
                pts = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t ** 2 * p2 + t ** 3 * p3
            else:
                pts = np.array([a['p'] - a['d'] * 2, corner, b['p'] - b['d'] * 2])
            lay = _draw_stroke(hc.shape, pts, (a['w'] + b['w']) / 2)
            body = lay > 0.5
            if (body & ~hc & ~others & ~inkd).sum() > 0.15 * body.sum():
                return False                                               # it would cross visible pixels that are not ink: no such line
            paint = np.where((lay > alpha)[..., None], (a['color'] + b['color']) / 2, paint)
            alpha = np.maximum(alpha, lay)
            return True

        links = [(a, b, c) for a, b, c in _link_stubs(stubs, diag) if draw(a, b, c)]
        taken = {id(x) for pr in links for x in pr[:2]}
        # a stub with no partner that runs into one of the lines just drawn (a divider meeting a ring) is extended up to that
        # line, if it is about as thick as the lines drawn (a thin letter stem is not a line)
        wref = float(np.mean([(a['w'] + b['w']) / 2 for a, b, _ in links])) if links else 0.0
        for a in stubs:
            if id(a) in taken or not links or not 0.7 * wref <= a['w'] <= 1.4 * wref:
                continue
            entered = False
            for step in range(2, int(diag * 1.2) + 14):
                q = a['p'] + a['d'] * step
                xi, yi = int(round(q[0])), int(round(q[1]))
                inside = 0 <= yi < hc.shape[0] and 0 <= xi < hc.shape[1] and hc[yi, xi]
                if not inside:
                    if entered:
                        break
                    continue
                entered = True
                if alpha[yi, xi] > 0.3:
                    lay = _draw_stroke(hc.shape, np.array([a['p'] - a['d'] * 2, q]), a['w'])
                    paint = np.where((lay > alpha)[..., None], a['color'], paint)
                    alpha = np.maximum(alpha, lay)
                    taken.add(id(a))
                    break
        for a, b, corner in _link_stubs(stubs, diag, corners=True, skip=taken):
            draw(a, b, corner)
        barrier = cv2.dilate((alpha > 0.05).astype(np.uint8), ell(1)) > 0
        if barrier.any():
            # each side of a drawn outline is filled only from its own side (a mark that straddles a border would otherwise
            # tint the one side with the colour of the other): the other side and the line are held back as unknown
            walk = ((hc & ~barrier) | (~hc & ~inkd & ~others)).astype(np.uint8)     # fills, cut along the outlines
            nreg, reg = cv2.connectedComponents(walk, connectivity=4)
            sides = {int(v) for v in np.unique(reg[hc & ~barrier]) if v}
            if len(sides) > 1:
                colour = colour.copy()
                for sid in sides:
                    known_here = (reg == sid) & ~hc
                    if not known_here.any():
                        continue
                    sep = cv2.inpaint(roi, ((~known_here).astype(np.uint8)) * 255, 3, cv2.INPAINT_TELEA)
                    mine = (reg == sid) & hc
                    colour[mine] = sep[mine]
            # under the outline itself (and its soft edge) take the colour of the nearest side, not the blend of both
            seam = hc & barrier
            if seam.any() and (~seam).any():
                _, near_lab = cv2.distanceTransformWithLabels((seam).astype(np.uint8), cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
                src_idx = np.flatnonzero(~seam.ravel())
                colour = colour.copy()
                colour[seam] = colour.reshape(-1, 3)[src_idx[near_lab[seam] - 1]]
        res = colour.astype(np.float32) * (1 - alpha[..., None]) + paint * alpha[..., None]
        res = np.clip(res + 0.5, 0, 255).astype(np.uint8)
        out[y0:y1, x0:x1][hc] = res[hc]
    return out


@tool("Remove marks", "Clean-up", [
    Param('colors', "Colour of the marks (click Pick on one)", 'colors', '', pick='color'),
    Param('tolerance', "Tolerance (RGB distance)", 'int', 35, 1, 441, slider=True),
    Param('grow', "Widen each mark by (px)", 'int', 3, 0, 20),
    Param('max_area', "Ignore shapes larger than (px)", 'int', 1500, 10, 1000000, slider=True, progressive=True),
    Param('method', "Rebuild with", 'choice', 'colour + outlines', choices=('colour + outlines', 'inpaint'))],
    doc="Removes small marks of one colour (the red stars of a figure, a stamp, a dot) and rebuilds what was under them. "
        "Pick the colour on a mark (Pick, then click it); every shape of that colour no bigger than the limit is taken out "
        "together with its anti-aliased fringe ('Widen'), so a larger object of a similar colour is left alone. 'colour + "
        "outlines' fills the shading from the fills around, side by side, and joins the dark outlines that enter the mark "
        "with the ones that leave it (also through a corner, and a divider that ends on a ring), so rings, dividers and shapes run on "
        "through it; 'inpaint' just smears the surroundings in (fine over flat colour only). A line that runs along the mark "
        "instead of into it may leave a small gap: touch it up with the Clone stamp. Use the working area to restrict it.")
def remove_marks(img, colors, tolerance, grow, max_area, method):
    cols = parse_colors(colors)
    if not cols:
        return img
    near = np.zeros(img.shape[:2], bool)
    for c in cols:
        near |= _distance_from(img, c) < tolerance
    if not near.any():
        return img
    n, labels, stats, _ = cv2.connectedComponentsWithStats(near.astype(np.uint8), connectivity=8)
    keep = stats[:, cv2.CC_STAT_AREA] <= max_area
    keep[0] = False
    hit = keep[labels]
    if not hit.any():
        return img
    if grow > 0:
        hit = cv2.dilate(hit.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1,) * 2)) > 0
    if method == 'inpaint':
        return inpaint_mask(img, hit, max(3, grow))
    return rebuild_under(img, hit)


def erase_rect(img, rect, fill='white', radius=3, color=None):
    """Paint the rectangle (x0, y0, x1, y1) of an RGB image: a flat 'white' / 'black' / 'picked colour' (the first colour
    of `color`, '#rrggbb'; 'transparent' is white here, there is no alpha) or 'inpaint' (rebuilt from the surroundings).
    Returns `img` itself when nothing changes."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = max(0, rect[0]), max(0, rect[1]), min(w, rect[2]), min(h, rect[3])
    if x1 <= x0 or y1 <= y0:
        return img
    if fill == 'inpaint':
        hole = np.zeros((h, w), bool)
        hole[y0:y1, x0:x1] = True
        return inpaint_mask(img, hole, radius)
    out = img.copy()
    out[y0:y1, x0:x1] = _picked_rgb(color) if fill == 'picked colour' else FILLS.get(fill, FILLS['white'])
    return out if not np.array_equal(out, img) else img


def erase_rect_layer(rgba, rect, fill='transparent', radius=3, color=None):
    """The same on an RGBA layer, `rect` in the layer's own coordinates: 'transparent' makes the rectangle see-through,
    'white' / 'black' paint it opaque, 'inpaint' rebuilds the colours from the visible pixels around it (alpha kept)."""
    h, w = rgba.shape[:2]
    x0, y0, x1, y1 = max(0, rect[0]), max(0, rect[1]), min(w, rect[2]), min(h, rect[3])
    if x1 <= x0 or y1 <= y0:
        return rgba
    out = rgba.copy()
    if fill == 'inpaint':
        hole = np.zeros((h, w), bool)
        hole[y0:y1, x0:x1] = True
        out[..., :3] = inpaint_mask(np.ascontiguousarray(rgba[..., :3]), hole, radius, rgba[..., 3] > 0)
    elif fill == 'transparent':
        out[y0:y1, x0:x1, 3] = 0
    else:
        out[y0:y1, x0:x1, :3] = _picked_rgb(color) if fill == 'picked colour' else FILLS[fill]
        out[y0:y1, x0:x1, 3] = 255
    return out if not np.array_equal(out, rgba) else rgba


@tool("Erase rectangle", "Retouch", [
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS) + ('picked colour', 'transparent', 'inpaint')),
    Param('color', "Picked colour", 'colors', '#ffffff', pick='color1'),
    Param('radius', "Inpaint radius (px)", 'int', 3, 1, 30, slider=True)],
    preview=False, interactive='rect-erase',
    doc="Drag a rectangle: what is inside is erased as soon as you let go (one Undo step). Pick a colour from the picture "
        "(Pick) or choose one (...) and the rectangle is painted with it. On an image layer 'transparent' makes it "
        "see-through; on the Background it paints white. Only the part inside the working area, if one is set.")
def erase_rectangle(img, fill, color, radius):
    return img


@tool("Move area", "Retouch", [Param('copy', "Copy instead of moving", 'bool', False)],
      preview=False, interactive='rect-move',
      doc="Drag a rectangle round what you want to move, then drag inside it to move it (Shift also copies). The place it "
          "leaves is filled as the 'leaves' box says. Drag a handle to resize the rectangle first; drag outside to draw "
          "another. On an image layer it moves pixels inside that layer.")
def move_area(img, copy):
    return img


def stamp_clone(dst, src, cx, cy, ox, oy, size, hardness=100, opacity=100, region=None):
    """Copy a round patch of `src`, centred at (cx + ox, cy + oy), into `dst` centred at (cx, cy); `dst` is modified IN
    PLACE and has the same shape as `src` (RGB or RGBA). `hardness` 100 = a hard disc, lower = a soft edge that fades out over
    that part of the radius; `opacity` 0..100. Only pixels inside `region` (x0, y0, x1, y1) change, and nothing is copied
    from outside `src`. Returns False when nothing was inside."""
    h, w = dst.shape[:2]
    r = max(0.5, size / 2.0)
    x0, y0 = max(0, int(math.floor(cx - r)), -ox), max(0, int(math.floor(cy - r)), -oy)
    x1, y1 = min(w, int(math.ceil(cx + r)) + 1, w - ox), min(h, int(math.ceil(cy + r)) + 1, h - oy)
    if region is not None:
        x0, y0, x1, y1 = max(x0, region[0]), max(y0, region[1]), min(x1, region[2]), min(y1, region[3])
    if x1 <= x0 or y1 <= y0:
        return False
    ys, xs = np.mgrid[y0:y1, x0:x1]
    soft = max(r * (1.0 - hardness / 100.0), 1e-6)
    wgt = np.clip((r - np.hypot(xs - cx, ys - cy)) / soft, 0.0, 1.0).astype(np.float32) * (opacity / 100.0)
    m = region_mask(region, dst.shape)
    if m is not None:
        wgt = wgt * m[y0:y1, x0:x1]
    if not wgt.any():
        return False
    patch = src[y0 + oy:y1 + oy, x0 + ox:x1 + ox].astype(np.float32)
    cur = dst[y0:y1, x0:x1].astype(np.float32)
    dst[y0:y1, x0:x1] = _to_u8(cur + (patch - cur) * wgt[..., None])
    return True


@tool("Clone stamp", "Retouch", [
    Param('size', "Diameter (px)", 'int', 10, 1, 400, slider=True, progressive=True),
    Param('hardness', "Hardness (%)", 'int', 60, 0, 100, slider=True),
    Param('opacity', "Opacity (%)", 'int', 100, 1, 100, slider=True),
    Param('aligned', "Aligned (keep the offset between strokes)", 'bool', True),
    Param('src_x', "Source X (px, -1 = not set)", 'int', -1, -1, 100000, pick='point'),
    Param('src_y', "Source Y (px)", 'int', -1, -1, 100000)],
    preview=False, interactive='clone',
    doc="First define the SOURCE: press 'Set source' and click on the picture (or Ctrl+click while painting, or type X and "
        "Y); a cyan cross marks it. Then paint where the copy should go: the pixels under the cursor are copied from the "
        "source, which moves along with it. Limited to the selection if there is one; on an image layer it copies inside "
        "that layer. One stroke is one Undo step.")
def clone_stamp(img, size, hardness, opacity, aligned, src_x, src_y):
    return img


@tool("Clean brush", "Retouch", [
    Param('size', "Diameter (px)", 'int', 10, 1, 300, slider=True, progressive=True),
    Param('radius', "Inpaint radius (px)", 'int', 3, 1, 30, slider=True)],
    preview=False, interactive='clean',
    doc="Paint over a spot, a scratch or a stray mark (it shows red): when you let go it is rebuilt from what is around it. "
        "Several short strokes work better than one wide one. Limited to the selection if there is one; on an image layer "
        "only that layer's visible pixels are used. One stroke is one Undo step.")
def clean_brush(img, size, radius):
    return img


@tool("Despeckle", "Clean-up", [
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


@tool("Flatten ink to one colour", "Clean-up", [
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


@tool("Eraser brush", "Draw", [
    Param('size', "Diameter (px)", 'int', 10, 1, 300, slider=True, progressive=True),
    Param('fill', "Paint with", 'choice', 'white', choices=tuple(FILLS) + ('picked colour', 'transparent')),
    Param('color', "Picked colour", 'colors', '#ffffff', pick='color1')],
    preview=False, interactive='brush',
    doc="Drag on the image to paint over it (limited to the selection if there is one: draw the selection with another tool first). "
        "Pick a colour from the picture (Pick) or choose one (...) to paint with it. On an image layer 'transparent' erases "
        "to see through. One stroke is one Undo step.")
def eraser_brush(img, size, fill, color):
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
        m = region_mask(region, img.shape)
        if m is not None:                                  # a mask selection: fade by its value (0 keeps, 1 paints)
            wgt = m[y0:y1, x0:x1]
            if sub.ndim == 3:
                wgt = wgt[..., None]
            sub[:] = np.clip(np.rint(keep + (sub.astype(np.float32) - keep) * wgt), 0, 255).astype(np.uint8)


# ---- drawing: pencil, paintbrush, fill bucket, gradient ---------------------------------------------------------------------------
def region_weight_box(region, shape, box):
    """The weight (0..1) the working area gives to the pixels of `box` (x0, y0, x1, y1) of an array of `shape`: None when there is
    no area (everything counts), else a (h, w) float array: 1 inside a rectangle, the mask value inside a shape, 0 outside."""
    if region is None:
        return None
    x0, y0, x1, y1 = box
    w = np.zeros((y1 - y0, x1 - x0), np.float32)
    ix0, iy0, ix1, iy1 = max(x0, region[0]), max(y0, region[1]), min(x1, region[2]), min(y1, region[3])
    if ix1 > ix0 and iy1 > iy0:
        m = region_mask(region, shape)
        w[iy0 - y0:iy1 - y0, ix0 - x0:ix1 - x0] = 1.0 if m is None else m[iy0:iy1, ix0:ix1]
    return w


def stamp_cover(cover, p0, p1, size, hardness=100, square=False, aliased=False):
    """Add a round (or square) stroke of diameter `size` from p0 to p1 (pixels) to `cover`, a float (h, w) array of how much each pixel
    has been painted so far in this stroke (0..1): stamps are spaced along the segment and the larger weight wins, so overlapping
    stamps do not build up. `aliased` gives hard, jagged pixels (the pencil); otherwise the edge fades over (100 - hardness) % of the
    radius. Returns the box (x0, y0, x1, y1) that was touched, or None."""
    h, w = cover.shape
    r = max(0.5, size / 2.0)
    dist = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
    step = max(0.5, size * (0.15 if aliased else 0.1))
    n = max(1, int(math.ceil(dist / step)))
    soft = max(r * (1.0 - hardness / 100.0), 1e-6)
    touched = None
    for i in range(n + 1):
        cx, cy = p0[0] + (p1[0] - p0[0]) * i / n, p0[1] + (p1[1] - p0[1]) * i / n
        x0, y0 = max(0, int(math.floor(cx - r)) - 1), max(0, int(math.floor(cy - r)) - 1)
        x1, y1 = min(w, int(math.ceil(cx + r)) + 2), min(h, int(math.ceil(cy + r)) + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        ys, xs = np.mgrid[y0:y1, x0:x1]
        if square:
            d = np.maximum(np.abs(xs - cx), np.abs(ys - cy))
        else:
            d = np.hypot(xs - cx, ys - cy)
        wgt = (d <= r).astype(np.float32) if aliased else np.clip((r - d) / soft, 0.0, 1.0).astype(np.float32)
        sub = cover[y0:y1, x0:x1]
        np.maximum(sub, wgt, out=sub)
        touched = (x0, y0, x1, y1) if touched is None else (min(touched[0], x0), min(touched[1], y0), max(touched[2], x1), max(touched[3], y1))
    return touched


def step_size(size, direction, lo=1, hi=1000):
    """The next brush size for a `[` (direction -1) or `]` (+1) key: about 10 % of the size, at least 1 px, inside [lo, hi]."""
    step = max(1, int(round(size * 0.1)))
    return int(min(hi, max(lo, size + direction * step)))


def clear_region(target, color, region):
    """`target` ((h, w, 3) RGB or (h, w, 4) RGBA) with the working area `region` (rectangle or MaskRegion) cleared: filled with `color`
    ((r, g, b)), or made transparent when `color` is None and the target has alpha (on RGB, None means white). Weights follow a
    feathered mask. Returns `target` itself when nothing changes (no area, empty overlap, already that colour)."""
    if region is None:
        return target
    h, w = target.shape[:2]
    weight = region_weight_box(region, target.shape, (0, 0, w, h))
    if weight is None or not weight.any():
        return target
    if color is None and target.shape[2] == 4:
        out = target.copy()
        out[..., 3] = _to_u8(target[..., 3].astype(np.float32) * (1.0 - np.clip(weight, 0.0, 1.0)))
    else:
        out = paint_over(target, color if color is not None else (255, 255, 255), weight)
    return target if np.array_equal(out, target) else out


def paint_over(orig, color, weight):
    """`orig` ((h, w, 3) RGB or (h, w, 4) RGBA, straight alpha) with `color` ((3,) or (h, w, 3)) painted over it with `weight` (h, w,
    0..1) as its opacity. On RGBA the alpha grows too (paint over a see-through pixel makes it opaque). Returns a new array."""
    w = np.clip(np.asarray(weight, np.float32), 0.0, 1.0)[..., None]
    c = np.asarray(color, np.float32)
    f = orig.astype(np.float32)
    if orig.shape[2] == 3:
        return _to_u8(f + (c - f) * w)
    a0 = f[..., 3:4] / 255.0
    ao = a0 + w * (1.0 - a0)
    rgb = np.where(ao > 1e-6, (f[..., :3] * a0 * (1.0 - w) + c * w) / np.maximum(ao, 1e-6), c)
    return np.concatenate([_to_u8(rgb), _to_u8(ao * 255.0)], axis=2)


def flood_fill(img, x, y, color, tolerance=32, contiguous=True, opacity=100, grow=1, region=None):
    """Paint `color` where the picture looks like the pixel at (x, y): within `tolerance` (distance of the colours, alpha included on
    RGBA), joined to it unless `contiguous` is off. `grow` widens the filled area by that many pixels so the pale fringe of a line is
    covered too. Only inside `region`. Returns `img` itself when nothing changes."""
    h, w = img.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return img
    seed = np.median(img[max(0, y - 1):y + 2, max(0, x - 1):x + 2].reshape(-1, img.shape[2]), axis=0)
    near = _distance_from(img, seed) <= tolerance
    if contiguous:
        if not near[y, x]:
            return img
        n, lab = cv2.connectedComponents(near.astype(np.uint8), connectivity=8)
        near = lab == lab[y, x]
    mask = near.astype(np.uint8)
    if grow > 0:
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * int(grow) + 1,) * 2))
    weight = mask.astype(np.float32) * (opacity / 100.0)
    rw = region_weight_box(region, img.shape, (0, 0, w, h))
    if rw is not None:
        weight = weight * rw
    if not weight.any():
        return img
    out = paint_over(img, color, weight)
    return img if np.array_equal(out, img) else out


TONE_EFFECTS = ('dodge (lighten)', 'burn (darken)')
TONE_RANGES = ('midtones', 'shadows', 'highlights', 'all')


def tone_paint(orig, weight, effect, tones='midtones', exposure=30):
    """`orig` ((h, w, 3) RGB or (h, w, 4) RGBA) lightened (dodge) or darkened (burn) where `weight` (h, w, 0..1) is: a pixel moves
    towards white (dodge) or black (burn) by weight * exposure % * how much of `tones` it is (judged on the ORIGINAL pixel, so
    a stroke does not feed itself). Moving a fraction of the way keeps colours and cannot clip; alpha is left alone and a see-through
    pixel changes less. Returns a new array."""
    f = orig[..., :3].astype(np.float32)
    y = (0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]) / 255.0
    r = {'shadows': (1.0 - y) ** 2, 'highlights': y ** 2, 'midtones': 1.0 - np.abs(2.0 * y - 1.0), 'all': np.ones_like(y)}[tones]
    a = np.clip(np.asarray(weight, np.float32), 0.0, 1.0) * (exposure / 100.0) * r
    if orig.shape[2] == 4:
        a = a * (orig[..., 3].astype(np.float32) / 255.0)
    a = a[..., None]
    rgb = _to_u8(f + (255.0 - f) * a) if effect.startswith('dodge') else _to_u8(f * (1.0 - a))
    return rgb if orig.shape[2] == 3 else np.concatenate([rgb, orig[..., 3:4]], axis=2)


GRADIENT_KINDS = ('linear', 'radial', 'reflected', 'angle')
GRADIENT_COLOURS = ('foreground to background', 'foreground to transparent', 'background to foreground')


def gradient_t(shape, p0, p1, kind='linear'):
    """The position (0..1) of every pixel along a gradient from p0 to p1: along the line (linear), by distance from p0 (radial), along
    the line and back (reflected, p0 is the middle) or round p0 starting at the direction of p1 (angle)."""
    h, w = shape
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    n2 = max(dx * dx + dy * dy, 1e-6)
    if kind == 'radial':
        t = np.hypot(xs - p0[0], ys - p0[1]) / math.sqrt(n2)
    elif kind == 'reflected':
        t = np.abs((xs - p0[0]) * dx + (ys - p0[1]) * dy) / n2
    elif kind == 'angle':
        t = ((np.arctan2(ys - p0[1], xs - p0[0]) - math.atan2(dy, dx)) / (2 * math.pi)) % 1.0
    else:
        t = ((xs - p0[0]) * dx + (ys - p0[1]) * dy) / n2
    return np.clip(t, 0.0, 1.0).astype(np.float32)


def gradient_fill(img, p0, p1, fg, bg, kind='linear', colours='foreground to background', opacity=100, region=None):
    """`img` with a gradient painted over it from p0 to p1 (pixel coordinates of `img`): foreground to background, foreground fading
    to transparent, or background to foreground; only inside `region`. Returns `img` itself when the two points are the same."""
    if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) < 1.0:
        return img
    t = gradient_t(img.shape[:2], p0, p1, kind)[..., None]
    fg, bg = np.asarray(fg, np.float32), np.asarray(bg, np.float32)
    if colours == 'foreground to transparent':
        colour, weight = np.broadcast_to(fg, t.shape[:2] + (3,)), (1.0 - t[..., 0])
    else:
        a, b = (bg, fg) if colours == 'background to foreground' else (fg, bg)
        colour, weight = a + (b - a) * t, np.ones(t.shape[:2], np.float32)
    weight = weight * (opacity / 100.0)
    h, w = img.shape[:2]
    rw = region_weight_box(region, img.shape, (0, 0, w, h))
    if rw is not None:
        weight = weight * rw
    out = paint_over(img, colour, weight)
    return img if np.array_equal(out, img) else out


@tool("Pencil", "Draw", [
    Param('size', "Size (px)", 'int', 3, 1, 100, slider=True, progressive=True),
    Param('shape', "Tip", 'choice', 'round', choices=('round', 'square')),
    Param('opacity', "Opacity (%)", 'int', 100, 1, 100, slider=True)],
    preview=False, interactive='paint',
    doc="Drag to draw with hard, jagged pixels in the foreground colour (the two squares above the picture; Ctrl+click on the picture "
        "picks it, Ctrl+Shift+click the background colour, X swaps them). Shift+click draws a straight line from where the last "
        "stroke ended. Limited to the selection if there is one; on an image layer it draws in that layer. One stroke is one Undo step.")
def pencil(img, size, shape, opacity):
    return img


@tool("Paintbrush", "Draw", [
    Param('size', "Size (px)", 'int', 20, 1, 500, slider=True, progressive=True),
    Param('hardness', "Hardness (%)", 'int', 60, 0, 100, slider=True),
    Param('opacity', "Opacity (%)", 'int', 100, 1, 100, slider=True)],
    preview=False, interactive='paint',
    doc="Drag to paint with a soft round brush in the foreground colour (the two squares above the picture; Ctrl+click on the picture "
        "picks it, Ctrl+Shift+click the background colour, X swaps them). A stroke never gets darker where it crosses itself: the "
        "opacity is that of the whole stroke. Shift+click draws a straight line from where the last stroke ended.")
def paintbrush(img, size, hardness, opacity):
    return img


@tool("Dodge / burn", "Retouch", [
    Param('effect', "Effect", 'choice', 'dodge (lighten)', choices=TONE_EFFECTS),
    Param('tones', "Works on", 'choice', 'midtones', choices=TONE_RANGES),
    Param('exposure', "Exposure (%)", 'int', 30, 1, 100, slider=True),
    Param('size', "Size (px)", 'int', 60, 1, 500, slider=True, progressive=True),
    Param('hardness', "Hardness (%)", 'int', 0, 0, 100, slider=True)],
    preview=False, interactive='paint',
    doc="Drag to lighten (dodge) or darken (burn) parts of the picture by hand, as in a darkroom: faces, eyes, folds, the corner of a "
        "frame. 'Works on' limits the effect to the midtones, the shadows or the highlights, so a dodge lifts the shadows without "
        "washing out the lights. The exposure is that of the whole stroke (it does not build up where the stroke crosses itself): go "
        "over the same place again, with a new stroke, to add more; a low exposure and a soft edge look natural. Colours are kept. "
        "Shift+click draws a straight line. Limited to the selection if there is one; on an image layer it works in that layer.")
def dodge_burn(img, effect, tones, exposure, size, hardness):
    return img


@tool("Fill bucket", "Draw", [
    Param('tolerance', "Tolerance (colour distance)", 'int', 32, 0, 441, slider=True),
    Param('contiguous', "Only the area joined to the click", 'bool', True),
    Param('grow', "Widen the fill by (px)", 'int', 1, 0, 20),
    Param('opacity', "Opacity (%)", 'int', 100, 1, 100, slider=True),
    Param('use', "Fill with", 'choice', 'foreground', choices=('foreground', 'background'))],
    preview=False, interactive='fill',
    doc="Click on the picture: the area of that colour is filled with the foreground (or background) colour. Widen by a pixel or two so "
        "the pale edge of a line is covered; raise the tolerance for shaded or noisy areas. On an image layer it works on the layer's "
        "own pixels, transparency included. Limited to the selection if there is one.")
def fill_bucket(img, tolerance, contiguous, grow, opacity, use):
    return img


@tool("Gradient", "Draw", [
    Param('kind', "Shape", 'choice', 'linear', choices=GRADIENT_KINDS),
    Param('colours', "Colours", 'choice', GRADIENT_COLOURS[0], choices=GRADIENT_COLOURS),
    Param('opacity', "Opacity (%)", 'int', 100, 1, 100, slider=True)],
    preview=False, interactive='gradient',
    doc="Drag from where the gradient starts to where it ends (the picture shows it while you drag, Esc cancels): foreground to "
        "background, foreground fading to transparent, or the other way round. Linear, radial, reflected (the start is the middle) or "
        "angle (round the start). Limited to the selection if there is one.")
def gradient_tool(img, kind, colours, opacity):
    return img


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


@tool("Join dashed lines", "Clean-up", [
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


@tool("Add text", "Annotate", [
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
    kind 'text' / 'shape' / 'scalebar': the keyword arguments of its renderer in `params` (see LAYER_KINDS), rasterised on
    demand so the text, the shape or the scale bar stays editable.
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
        if self.kind in LAYER_KINDS:
            return LAYER_KINDS[self.kind]['render'](**self.params)
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


# ---- shapes (line, arrow, rectangle, ellipse) and the scale bar: layers drawn from parameters --------------------
SHAPES = ('line', 'arrow', 'double arrow', 'rectangle', 'ellipse')
DASHES = ('solid', 'dashed', 'dotted')
_SS = 4                                                      # shapes are drawn 4 x larger and shrunk: smooth edges


def _dash_runs(pts, closed, on, off):
    """Pieces [(p, q), ...] of the polyline `pts` (N x 2 array): `on` long, then a gap `off` long, along its whole length."""
    pts = np.asarray(pts, np.float64)
    if closed:
        pts = np.vstack([pts, pts[:1]])
    runs, pos, drawing, left = [], pts[0], True, on
    for a, b in zip(pts[:-1], pts[1:]):
        seg = float(np.hypot(*(b - a)))
        if seg == 0:
            continue
        u, t = (b - a) / seg, 0.0
        while seg - t > 1e-9:
            step = min(left, seg - t)
            if drawing:
                runs.append((a + u * t, a + u * (t + step)))
            t += step
            left -= step
            if left <= 1e-9:
                drawing = not drawing
                left = on if drawing else off
    return runs


@functools.lru_cache(maxsize=128)
def _render_shape_cached(shape, x0, y0, x1, y1, stroke, width, filled, fill, dash, head, opacity):
    if opacity <= 0:
        return None
    arrow = shape in ('arrow', 'double arrow')
    pad = int(math.ceil(width / 2.0 + (head if arrow else 0) + 2))
    W, H = int(math.ceil(max(x0, x1) + pad)), int(math.ceil(max(y0, y1) + pad))
    ss = _SS
    sa, fa = np.zeros((H * ss, W * ss), np.uint8), np.zeros((H * ss, W * ss), np.uint8)
    thick = max(1, int(round(width * ss)))
    P = lambda x, y: (int(round(x * ss)), int(round(y * ss)))
    on, off = {'solid': (0, 0), 'dashed': (max(6.0, 4 * width) * ss, max(4.0, 3 * width) * ss),
               'dotted': (max(1.0, width) * ss, max(3.0, 2 * width) * ss)}[dash]

    def stroke_path(pts, closed):
        pts = np.asarray(pts, np.float64)
        if on == 0:
            cv2.polylines(sa, [np.round(pts * ss).astype(np.int32)], closed, 255, thick, cv2.LINE_AA)
        else:
            for a, b in _dash_runs(pts * ss, closed, on, off):
                cv2.line(sa, P(*(a / ss)), P(*(b / ss)), 255, thick, cv2.LINE_AA)

    if shape in ('line', 'arrow', 'double arrow'):
        a, b = np.array([x0, y0], np.float64), np.array([x1, y1], np.float64)
        d = b - a
        n = float(np.hypot(*d)) or 1.0
        u = d / n
        ha = min(head, n * 0.5) if shape == 'double arrow' else min(head, n)
        start = a + u * ha * 0.8 if shape == 'double arrow' else a
        end = b - u * ha * 0.8 if arrow else b                # the stroke stops inside the head, no blunt end poking out
        stroke_path([start, end], False)
        for tip, direction in ((b, u), (a, -u)) if shape == 'double arrow' else (((b, u),) if shape == 'arrow' else ()):
            base = tip - direction * ha
            side = np.array([-direction[1], direction[0]]) * ha * 0.45
            cv2.fillPoly(sa, [np.round(np.array([tip, base + side, base - side]) * ss).astype(np.int32)], 255, cv2.LINE_AA)
    elif shape == 'rectangle':
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        if filled:
            cv2.fillPoly(fa, [np.round(np.array(corners) * ss).astype(np.int32)], 255, cv2.LINE_AA)
        stroke_path(corners, True)
    else:                                                    # ellipse inscribed in the box
        cx, cy, rx, ry = (x0 + x1) / 2.0, (y0 + y1) / 2.0, abs(x1 - x0) / 2.0, abs(y1 - y0) / 2.0
        if filled:
            cv2.ellipse(fa, P(cx, cy), (int(round(rx * ss)), int(round(ry * ss))), 0, 0, 360, 255, -1, cv2.LINE_AA)
        n = max(48, int(math.pi * (rx + ry) / 2))
        ang = np.linspace(0, 2 * math.pi, n, endpoint=False)
        stroke_path(np.stack([cx + rx * np.cos(ang), cy + ry * np.sin(ang)], 1), True)
    sa = cv2.resize(sa, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    fa = cv2.resize(fa, (W, H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    s_rgb = np.asarray((parse_colors(stroke) or [(0, 0, 0)])[0], np.float32)
    f_rgb = np.asarray((parse_colors(fill) or [(255, 255, 255)])[0], np.float32)
    alpha = sa + fa * (1.0 - sa)
    num = s_rgb * sa[..., None] + f_rgb * (fa * (1.0 - sa))[..., None]
    out = np.empty((H, W, 4), np.uint8)
    out[..., :3] = _to_u8(num / np.maximum(alpha, 1e-6)[..., None])
    out[..., 3] = _to_u8(alpha * 255.0 * (opacity / 100.0))
    out.flags.writeable = False
    return out


def render_shape(shape, x0, y0, x1, y1, stroke='#000000', width=3.0, filled=False, fill='#ffffff', dash='solid', head=14.0,
                 opacity=100):
    """RGBA array (straight alpha) of a shape drawn between (x0, y0) and (x1, y1), in coordinates of the array itself (so they
    must be >= the margin the stroke needs: `shape_layer` takes care of that). Anti-aliased; a dashed or dotted outline;
    arrow heads of `head` px. None when nothing would show."""
    return _render_shape_cached(str(shape), float(x0), float(y0), float(x1), float(y1), str(stroke), float(width), bool(filled),
                                str(fill), str(dash), float(head), int(opacity))


@functools.lru_cache(maxsize=64)
def _render_scalebar_cached(px_len, thickness, color, text, size, font, label_pos, opacity):
    if opacity <= 0:
        return None
    rgb = (parse_colors(color) or [(0, 0, 0)])[0]
    bar_w, thick = max(1, int(round(px_len))), max(1, int(thickness))
    f = load_font(font, size)
    show = bool(text) and label_pos != 'none'
    if show:
        probe = ImageDraw.Draw(Image.new('L', (1, 1)))
        l, t, r, b = probe.textbbox((0, 0), text, font=f)
        tw, th = r - l, b - t
    else:
        tw = th = l = t = 0
    gap, pad = max(2, size // 4), 1
    W = max(bar_w, tw) + 2 * pad
    H = thick + (th + gap if show else 0) + 2 * pad
    mask = Image.new('L', (W, H), 0)
    d = ImageDraw.Draw(mask)
    bar_y = pad + (th + gap if show and label_pos == 'above' else 0)
    bx = (W - bar_w) // 2
    d.rectangle([bx, bar_y, bx + bar_w - 1, bar_y + thick - 1], fill=255)
    if show:
        ty = pad - t if label_pos == 'above' else pad + thick + gap - t
        d.text(((W - tw) // 2 - l, ty), text, font=f, fill=255)
    out = np.empty((H, W, 4), np.uint8)
    out[..., :3] = rgb
    out[..., 3] = _to_u8(np.asarray(mask, np.float32) * (opacity / 100.0))
    out.flags.writeable = False
    return out


def render_scalebar(px_len, thickness=6, color='#000000', text='', size=24, font='default', label_pos='above', opacity=100, **_):
    """RGBA array of a scale bar `px_len` pixels long (a crisp rectangle `thickness` thick) with `text` centred above or below
    it ('none' = no label). Other keywords (length, unit, label ...) are kept in the layer only for editing."""
    return _render_scalebar_cached(float(px_len), int(thickness), str(color), str(text), int(size), str(font), str(label_pos),
                                   int(opacity))


def _text_layer(p, old, scale):
    """Layer from the boxes of Add text (x, y and the render_text keywords), or None for an empty text."""
    p = dict(p)
    x, y = p.pop('x'), p.pop('y')
    if render_text(**p) is None:
        return None
    first = p['text'].replace("\\n", "\n").strip().split("\n")[0][:24]
    if old is None:
        return Layer('text', "Text: " + first, x, y, params=p)
    name = "Text: " + first if old.name.startswith("Text: ") else old.name
    return old.replace(x=x, y=y, params=p, name=name)


def _text_boxes(L, scale):
    return dict(L.params, x=L.x, y=L.y)


def _shape_layer(p, old, scale):
    """Layer from the boxes of Shapes: x0, y0, x1, y1 are CANVAS coordinates; the layer is the tight box round the drawing, with the
    points kept relative to it (so moving the layer moves the shape). None when the shape has no extent."""
    p = dict(p)
    x0, y0, x1, y1 = (float(p.pop(k)) for k in ('x0', 'y0', 'x1', 'y1'))
    flat = p['shape'] in ('line', 'arrow', 'double arrow')
    if (flat and x0 == x1 and y0 == y1) or (not flat and (x0 == x1 or y0 == y1)):
        return None
    arrow = p['shape'] in ('arrow', 'double arrow')
    pad = int(math.ceil(float(p['width']) / 2.0 + (float(p['head']) if arrow else 0) + 2))
    ox, oy = int(math.floor(min(x0, x1))) - pad, int(math.floor(min(y0, y1))) - pad
    params = dict(p, x0=x0 - ox, y0=y0 - oy, x1=x1 - ox, y1=y1 - oy)
    name = "Shape: " + p['shape']
    if old is None:
        return Layer('shape', name, ox, oy, params=params)
    return old.replace(x=ox, y=oy, params=params, name=name if old.name.startswith("Shape: ") else old.name)


def _shape_boxes(L, scale):
    q = dict(L.params)
    q.update(x0=int(round(q['x0'] + L.x)), y0=int(round(q['y0'] + L.y)), x1=int(round(q['x1'] + L.x)), y1=int(round(q['y1'] + L.y)))
    return q


def _scalebar_layer(p, old, scale):
    """Layer from the boxes of Scale bar. `scale` = (units per pixel, unit) of the picture; the box 'Scale: units per pixel' overrides
    it. None while there is no scale."""
    p = dict(p)
    x, y = p.pop('x'), p.pop('y')
    upp = float(p.pop('units_per_px', 0.0)) or (scale[0] if scale else 0.0)
    unit = scale[1] if scale else ""
    if upp <= 0:
        return None
    length = float(p['length'])
    p['unit'] = unit
    p['px_len'] = length / upp
    p['text'] = p['label'].strip() or ("%g %s" % (length, unit)).strip()
    if render_scalebar(**p) is None:
        return None
    name = "Scale bar: " + p['text']
    if old is None:
        return Layer('scalebar', name, x, y, params=p)
    return old.replace(x=x, y=y, params=p, name=name if old.name.startswith("Scale bar: ") else old.name)


def _scalebar_boxes(L, scale):
    q = {k: v for k, v in L.params.items() if k not in ('px_len', 'text', 'unit')}
    return dict(q, x=L.x, y=L.y)


TEXT_KEYS = {'text': str, 'size': int, 'font': str, 'color': str, 'angle': float, 'opacity': int}
SHAPE_KEYS = {'shape': str, 'x0': float, 'y0': float, 'x1': float, 'y1': float, 'stroke': str, 'width': float, 'filled': bool,
              'fill': str, 'dash': str, 'head': float, 'opacity': int}
SCALEBAR_KEYS = {'length': float, 'unit': str, 'label': str, 'text': str, 'px_len': float, 'thickness': int, 'color': str,
                 'size': int, 'font': str, 'label_pos': str, 'opacity': int}
# what each non-image layer kind is: how it is drawn, which parameters the project file keeps (and their types), how the boxes of
# its tool become a layer (`build(boxes, old layer or None, scale)`), and how a layer fills those boxes again (`boxes`)
LAYER_KINDS = {
    'text': dict(render=render_text, keys=TEXT_KEYS, build=_text_layer, boxes=_text_boxes, tag="text", tool="Add text",
                 empty="The text is empty: nothing to add."),
    'shape': dict(render=render_shape, keys=SHAPE_KEYS, build=_shape_layer, boxes=_shape_boxes, tag="shape", tool="Shapes",
                  empty="The shape has no size yet: drag on the image from where it starts to where it ends."),
    'scalebar': dict(render=render_scalebar, keys=SCALEBAR_KEYS, build=_scalebar_layer, boxes=_scalebar_boxes, tag="scale bar",
                     tool="Scale bar", empty="There is no scale yet: calibrate it (Measure menu) or type the units per pixel in the box."),
}


@tool("Measure", "Annotate", [
    Param('what', "Measure", 'choice', 'distance', choices=('distance', 'path length', 'angle', 'area'))],
    preview=False, interactive='measure',
    doc="distance: drag from one point to the other (or click both). path length: click the points, double-click or Enter ends. "
        "angle: click three points, the second is the corner. area: click the corners of a polygon, double-click, Enter or a click "
        "on the first point closes it. Results are listed in Measure > Measurements, where one distance can calibrate the scale "
        "(then lengths and areas are also given in real units). Esc cancels the one in progress.")
def measure(img, what):
    return img


@tool("Shapes", "Annotate", [
    Param('shape', "Shape", 'choice', 'arrow', choices=SHAPES),
    Param('stroke', "Line colour", 'colors', '#e02020', pick='color1'),
    Param('width', "Line width (px)", 'float', 3.0, 0.5, 100.0, step=0.5, slider=True, progressive=True),
    Param('dash', "Line style", 'choice', 'solid', choices=DASHES),
    Param('head', "Arrow head size (px)", 'int', 16, 4, 300, slider=True),
    Param('filled', "Fill (rectangle, ellipse)", 'bool', False),
    Param('fill', "Fill colour", 'colors', '#ffffff', pick='color1'),
    Param('opacity', "Opacity (%)", 'int', 100, 0, 100, slider=True),
    Param('x0', "Start X (px)", 'int', 40, -100000, 100000),
    Param('y0', "Start Y (px)", 'int', 40, -100000, 100000),
    Param('x1', "End X (px)", 'int', 140, -100000, 100000),
    Param('y1', "End Y (px)", 'int', 100, -100000, 100000)],
    place='segment', layer='shape',
    doc="Drag on the image from the start to the end of the line (or the two corners of a rectangle / ellipse), then press the "
        "button: the shape becomes a layer you can restyle, move or reshape later. With the shape's layer selected, drag one "
        "end to move only that end, or drag elsewhere to draw it again.")
def shapes(img, shape, stroke, width, dash, head, filled, fill, opacity, x0, y0, x1, y1):
    """Paint the shape into the pixels (the app makes a layer instead and bakes it only on Flatten)."""
    L = _shape_layer(dict(shape=shape, stroke=stroke, width=width, dash=dash, head=head, filled=filled, fill=fill,
                          opacity=opacity, x0=x0, y0=y0, x1=x1, y1=y1), None, None)
    rgba = L.raster() if L is not None else None
    if rgba is None:
        return img
    out = img.copy()
    return out if blend_rgba(out, rgba, L.x, L.y) else img


@tool("Scale bar", "Annotate", [
    Param('length', "Length of the bar (in the picture's units)", 'float', 10.0, 0.0001, 1000000.0, step=1.0),
    Param('label', "Label (empty = length and unit)", 'text', ""),
    Param('label_pos', "Label", 'choice', 'above', choices=('above', 'below', 'none')),
    Param('thickness', "Thickness (px)", 'int', 6, 1, 200, slider=True),
    Param('color', "Colour", 'colors', '#000000', pick='color1'),
    Param('size', "Label size (px)", 'int', 24, 4, 600, slider=True),
    Param('font', "Font", 'choice', FONT_CHOICES[0], choices=FONT_CHOICES),
    Param('opacity', "Opacity (%)", 'int', 100, 0, 100, slider=True),
    Param('units_per_px', "Scale: units per pixel (0 = the calibrated one)", 'float', 0.0, 0.0, 1000000.0, step=0.001),
    Param('x', "X of the top-left corner (px)", 'int', 20, -100000, 100000),
    Param('y', "Y of the top-left corner (px)", 'int', 20, -100000, 100000)],
    place=True, layer='scalebar',
    doc="A scale bar of the length you give, drawn with the picture's scale (set it first: Measure > Calibrate from a measured length, or type "
        "the units per pixel below). Click on the image to place it, then press the button: it is a layer, so the length, label, "
        "colour and place can be changed later.")
def scale_bar(img, length, label, label_pos, thickness, color, size, font, opacity, units_per_px, x, y):
    """Paint the bar into the pixels (needs 'units per pixel' here: the app uses the picture's calibration and makes a layer)."""
    L = _scalebar_layer(dict(length=length, label=label, label_pos=label_pos, thickness=thickness, color=color, size=size,
                             font=font, opacity=opacity, units_per_px=units_per_px, x=x, y=y), None, None)
    rgba = L.raster() if L is not None else None
    if rgba is None:
        return img
    out = img.copy()
    return out if blend_rgba(out, rgba, L.x, L.y) else img


def shift_layers(layers, dx, dy):
    return tuple(L.replace(x=L.x + dx, y=L.y + dy) for L in layers)


def layer_region(sel, layer):
    """The canvas rectangle `sel` in the layer's own pixel coordinates, clipped to the layer; empty when they do not
    overlap (x1 <= x0 or y1 <= y0). None stays None."""
    if sel is None:
        return None
    h, w = layer.rgba.shape[:2]
    x0, y0, x1, y1 = sel[0] - layer.x, sel[1] - layer.y, sel[2] - layer.x, sel[3] - layer.y
    rect = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    m = getattr(sel, 'mask', None)
    if m is None:
        return rect
    lm = np.zeros((h, w), np.float32)                 # the canvas mask cut to the layer's own box
    cx0, cy0 = max(0, layer.x), max(0, layer.y)
    cx1, cy1 = min(m.shape[1], layer.x + w), min(m.shape[0], layer.y + h)
    if cx1 > cx0 and cy1 > cy0:
        lm[cy0 - layer.y:cy1 - layer.y, cx0 - layer.x:cx1 - layer.x] = m[cy0:cy1, cx0:cx1]
    return MaskRegion(rect, lm)


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
    if params.get('fill') in ('transparent', 'inpaint'):
        a = apply_tool(name, rgb, dict(params, fill='white'), region)
        b = apply_tool(name, rgb, dict(params, fill='black'), region)
        painted = (a != b).any(axis=2)
        if not painted.any():
            return rgba
        if params['fill'] == 'transparent':
            out_rgb, alpha = a, np.where(painted, 0, alpha).astype(np.uint8)
        else:                                          # rebuild from visible pixels only: transparent ones carry no colour
            out_rgb = inpaint_mask(rgb, painted, int(params.get('radius', 3)), alpha > 0)
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


@tool("Auto-crop borders", "Size and orientation", [
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


def intersect_rect(a, b):
    """The overlap of two (x0, y0, x1, y1) rectangles; empty (x1 <= x0 or y1 <= y0) when they do not touch."""
    return max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])


# ---- selections that are not rectangles (magic wand, lasso, invert, grow / shrink, feather) -------------------
def wand_mask(img, x, y, tolerance, contiguous=True):
    """Bool mask of the pixels whose colour is within `tolerance` (RGB distance) of the pixel at (x, y) (median of the 3x3 around
    it, so a stray pixel does not decide). `contiguous`: only the area connected to that pixel (8 neighbours); otherwise every
    such pixel of the picture. Empty mask when (x, y) is outside."""
    h, w = img.shape[:2]
    if not (0 <= x < w and 0 <= y < h):
        return np.zeros((h, w), bool)
    patch = img[max(0, y - 1):y + 2, max(0, x - 1):x + 2, :3].reshape(-1, 3)
    seed = np.median(patch, axis=0)
    near = _distance_from(img[..., :3], seed) <= tolerance
    if not contiguous or not near[y, x]:
        return near
    n, lab = cv2.connectedComponents(near.astype(np.uint8), connectivity=8)
    return lab == lab[y, x]


def polygon_mask(shape, points):
    """Bool mask of the polygon through `points` (x, y image pixels; at least 3), clipped to the (h, w) of `shape`."""
    m = np.zeros(shape[:2], np.uint8)
    if len(points) >= 3:
        cv2.fillPoly(m, [np.round(np.asarray(points, np.float64)).astype(np.int32)], 1)
    return m > 0


def sel_to_mask(sel, shape):
    """The float mask (h, w) of a selection (None = everything, rectangle, or MaskRegion)."""
    h, w = shape[:2]
    m = region_mask(sel, shape)
    if m is not None:
        return m
    out = np.zeros((h, w), np.float32)
    if sel is None:
        out[:] = 1.0
    else:
        out[sel[1]:sel[3], sel[0]:sel[2]] = 1.0
    return out


def mask_to_sel(mask, floor=0.004):
    """A MaskRegion for a float mask, or None when nothing is selected (bounding box of the values above `floor`)."""
    ys, xs = np.nonzero(mask > floor)
    if len(ys) == 0:
        return None
    return MaskRegion((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1), mask)


def combine_selection(sel, new, mode, shape):
    """Combine the selection `sel` with the bool mask `new`: 'replace', 'add', 'subtract' (what is not new stays; with no
    selection that is everything but `new`) or 'intersect'. Returns a MaskRegion, or None when the result is empty."""
    new = new.astype(np.float32)
    if mode == 'replace' or (sel is None and mode == 'add'):
        out = new
    else:
        old = sel_to_mask(sel, shape)
        out = {'add': np.maximum(old, new), 'subtract': old * (1.0 - new), 'intersect': old * new}[mode]
    return mask_to_sel(out)


def invert_selection(sel, shape):
    """Everything that was not selected (a rectangle, a mask or nothing at all, which becomes empty -> None)."""
    return mask_to_sel(1.0 - sel_to_mask(sel, shape))


def grow_selection(sel, px, shape):
    """Grow (px > 0) or shrink (px < 0) the selection by px pixels; a feathered edge becomes hard (>= 0.5 counts)."""
    if px == 0:
        return sel
    solid = (sel_to_mask(sel, shape) >= 0.5).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * abs(int(px)) + 1,) * 2)
    solid = cv2.dilate(solid, k) if px > 0 else cv2.erode(solid, k)
    return mask_to_sel(solid.astype(np.float32))


def feather_selection(sel, px, shape):
    """Soften the edge of the selection over about `px` pixels (Gaussian): the tools then blend instead of cutting."""
    if px <= 0:
        return sel
    m = cv2.GaussianBlur(sel_to_mask(sel, shape), (0, 0), max(0.3, px / 2.0))
    return mask_to_sel(m)


def mask_outline(mask, level=0.5):
    """Polylines [N x 2 float arrays, x then y in image pixels] that follow the edge of a float mask (for drawing it)."""
    cnts, _ = cv2.findContours((mask >= level).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        if len(c) >= 2 or cv2.contourArea(c) > 0:
            c = cv2.approxPolyDP(c, 0.7, True)
            out.append(c.reshape(-1, 2).astype(np.float32) + 0.5)
    return out


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
        self.steps = [None]               # beside every state: the recipe step that made it (a dict) or None (cannot be replayed)
        self.lost = 0                     # states dropped from the start by the memory limit: the recipe is then incomplete
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

    def push(self, label, img, layers=None, step=None):
        """New state; `layers` None keeps the current layers. `step` is the recipe step (see recipe_step) that makes it, when it
        is something a recipe can repeat on another picture."""
        layers = self.layers if layers is None else tuple(layers)
        del self.states[self.index + 1:]
        del self.steps[self.index + 1:]
        self.states.append((label, img, layers))
        self.steps.append(step)
        self.index += 1
        while len(self.states) > 2 and self._bytes() > self.limit:
            del self.states[0]
            del self.steps[0]
            self.index -= 1
            self.lost += 1

    def recipe(self):
        """(steps, skipped): the replayable steps that lead to the current state, and the labels of the steps that cannot be
        repeated on another picture (brush strokes, moves, edits of one layer ...)."""
        steps, skipped = [], []
        for i in range(1, self.index + 1):
            if self.steps[i] is not None:
                steps.append(self.steps[i])
            else:
                skipped.append(self.states[i][0])
        return steps, skipped

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


def save_project(path, base, layers, dpi=None, scale=None):
    """Write a project: a zip with manifest.json (version, size, dpi, one entry per layer: kind, name, position,
    visibility, opacity and, for text, its parameters), background.png and one RGBA layer_N.png per image layer.
    Written to a temporary file first, so a failure never destroys the previous project."""
    entries, files = [], {}
    for i, L in enumerate(layers):
        e = dict(kind=L.kind, name=L.name, x=int(L.x), y=int(L.y), visible=bool(L.visible), opacity=int(L.opacity))
        if L.kind in LAYER_KINDS:
            e['params'] = L.params
        else:
            e['file'] = "layer_%d.png" % i
            files[e['file']] = _png_bytes(L.rgba)
        entries.append(e)
    manifest = dict(format="ImageForge project", version=PROJECT_VERSION, width=int(base.shape[1]),
                    height=int(base.shape[0]), dpi=list(dpi) if dpi else None, layers=entries,
                    scale=dict(units_per_px=float(scale[0]), unit=str(scale[1])) if scale else None)
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
                if e["kind"] in LAYER_KINDS:
                    params = {k: t(e["params"][k]) for k, t in LAYER_KINDS[e["kind"]]['keys'].items()}
                    layers.append(Layer(e["kind"], params=params, **common))
                elif e["kind"] == 'image':
                    rgba = np.array(Image.open(io.BytesIO(zf.read(e["file"]))).convert('RGBA'))
                    layers.append(Layer('image', rgba=rgba, **common))
                else:
                    raise ValueError("unknown layer kind %r" % e["kind"])
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, TypeError) as ex:
        raise ValueError("not a valid ImageForge project (%s: %s)" % (type(ex).__name__, ex))
    dpi = manifest.get("dpi")
    return base, tuple(layers), (float(dpi[0]), float(dpi[1])) if dpi else None


def project_scale(path):
    """(units per pixel, unit) recorded in a project file, or None (no scale, or a file that cannot be read)."""
    try:
        with zipfile.ZipFile(path) as zf:
            sc = json.loads(zf.read("manifest.json")).get("scale")
        return (float(sc["units_per_px"]), str(sc["unit"])) if sc else None
    except (zipfile.BadZipFile, KeyError, ValueError, TypeError, OSError):
        return None


# ---- recipes: steps recorded while editing, replayed on other pictures -------------------------------------------------------------
RECIPE_FORMAT = "ImageForge recipe"
RECIPE_VERSION = 1
RECIPE_EXT = ".ifr"


def tool_step(name, params, region=None):
    """A recipe step: the tool and the values of its boxes (and the rectangle it was limited to, if any)."""
    return dict(kind='tool', tool=name, params=dict(params), region=[int(v) for v in region] if region is not None else None)


def layer_step(tool_name, params, scale=None):
    """A recipe step that adds a layer (text, shape, scale bar) made by a layer tool; it is merged into the picture when the
    recipe is run on a file."""
    return dict(kind='layer', tool=tool_name, params=dict(params), scale=list(scale) if scale else None)


def validate_steps(steps):
    """Check recipe steps against the tools of this installation; ValueError with a plain message otherwise."""
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict) or st.get('kind') not in ('tool', 'layer') or st.get('tool') not in TOOLS:
            raise ValueError("step %d uses a tool this ImageForge does not have: %r" % (i, st.get('tool') if isinstance(st, dict) else st))
        t = TOOLS[st['tool']]
        if st['kind'] == 'tool' and (t.interactive or t.layer):
            raise ValueError("step %d (%s) is not a tool that can run on its own" % (i, t.name))
        if st['kind'] == 'layer' and not t.layer:
            raise ValueError("step %d (%s) does not make a layer" % (i, t.name))
        if not isinstance(st.get('params', {}), dict):
            raise ValueError("step %d has no usable parameters" % i)


def save_recipe(path, steps, note=""):
    """Write a recipe (JSON). Written to a temporary file first."""
    validate_steps(steps)
    doc = dict(format=RECIPE_FORMAT, version=RECIPE_VERSION, note=note, steps=steps)
    tmp = path + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def load_recipe(path):
    """The steps of a recipe file; ValueError with a plain message when it is not one, is newer, or needs a missing tool."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError) as ex:
        raise ValueError("not a readable recipe (%s)" % ex)
    if not isinstance(doc, dict) or doc.get('format') != RECIPE_FORMAT:
        raise ValueError("not an ImageForge recipe")
    if int(doc.get('version', 0)) > RECIPE_VERSION:
        raise ValueError("saved by a newer ImageForge (recipe version %s)" % doc['version'])
    steps = doc.get('steps')
    if not isinstance(steps, list):
        raise ValueError("the recipe has no steps")
    validate_steps(steps)
    return steps


def run_recipe(img, steps):
    """The RGB picture `img` after every step: tools run on the picture (a rectangle recorded with a step is clipped to this
    picture's size), layer steps add their layer, which stays editable until the next geometry tool (that merges the layers
    first, like the editor does) or the end, where everything is merged."""
    base, layers = img, ()
    for st in steps:
        t = TOOLS[st['tool']]
        if st['kind'] == 'layer':
            sc = tuple(st['scale']) if st.get('scale') else None
            L = LAYER_KINDS[t.layer]['build'](dict(st['params']), None, sc)
            if L is not None:
                layers = layers + (L,)
            continue
        region = None
        if st.get('region') and not t.geometry:
            h, w = base.shape[:2]
            x0, y0, x1, y1 = st['region']
            region = (max(0, x0), max(0, y0), min(w, x1), min(h, y1))
            if region[2] <= region[0] or region[3] <= region[1]:
                continue
        if t.geometry and layers:
            if t.layer_shift:
                dx, dy = t.layer_shift(dict(_full_params(t, st['params'])), base.shape)
                layers = shift_layers(layers, dx, dy)
            else:
                base, layers = composite(base, layers), ()
        base = apply_tool(t.name, base, st['params'], region)
    return composite(base, layers)


def _full_params(t, params):
    """The coerced values of every box of tool `t` (defaults for the ones `params` does not mention)."""
    return {p.key: p.coerce(params[p.key]) if p.key in params else p.default for p in t.params}


def parse_step_text(text):
    """A recipe step from 'Tool name:key=value,key=value' (the form of the command line): the tool name is matched without regard
    to case, values are converted by the kind of each box (True / False for tick boxes)."""
    name, _, rest = text.partition(":")
    found = [n for n in TOOLS if n.lower() == name.strip().lower()]
    if not found:
        raise ValueError("no tool called %r (python ImageForge.pyw --list-tools shows them)" % name.strip())
    t = TOOLS[found[0]]
    by_key = {p.key: p for p in t.params}
    params = {}
    for item in [x for x in rest.split(",") if x.strip()]:
        key, _, val = item.partition("=")
        key = key.strip()
        if key not in by_key:
            raise ValueError("%s has no box %r (boxes: %s)" % (t.name, key, ", ".join(by_key) or "none"))
        p = by_key[key]
        val = val.strip()
        params[key] = (val.lower() in ('1', 'true', 'yes', 'on')) if p.kind == 'bool' else val
    return tool_step(t.name, {k: by_key[k].coerce(v) for k, v in params.items()})


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")


def list_images(folder, recursive=False):
    """The picture files of a folder, sorted (and of its sub-folders when asked)."""
    out = []
    if recursive:
        for root_dir, _, names in os.walk(folder):
            out += [os.path.join(root_dir, n) for n in names if n.lower().endswith(IMAGE_EXTS)]
    else:
        out = [os.path.join(folder, n) for n in os.listdir(folder) if n.lower().endswith(IMAGE_EXTS) and os.path.isfile(os.path.join(folder, n))]
    return sorted(out, key=lambda q: q.lower())


def batch_output_path(src, out_dir, fmt="same", suffix="", overwrite=False, root_dir=None):
    """Where the result for `src` goes: the same name (plus `suffix`) in `out_dir`, in the format `fmt` ('same', 'png', 'jpg',
    'tif'), keeping the sub-folder it came from when `root_dir` is given; a free name (name_1, name_2 ...) unless `overwrite`."""
    stem, ext = os.path.splitext(os.path.basename(src))
    ext = ext if fmt == 'same' else {'png': '.png', 'jpg': '.jpg', 'tif': '.tif'}[fmt]
    sub = os.path.relpath(os.path.dirname(src), root_dir) if root_dir else "."
    folder = os.path.normpath(os.path.join(out_dir, sub))
    path = os.path.join(folder, stem + suffix + ext)
    if overwrite:
        return path
    n = 0
    while os.path.exists(path) or os.path.normcase(path) == os.path.normcase(src):
        n += 1
        path = os.path.join(folder, "%s%s_%d%s" % (stem, suffix, n, ext))
    return path


def run_batch(steps, files, out_dir, fmt="same", suffix="", overwrite=False, jpeg_quality=95, root_dir=None, progress=None):
    """Run a recipe on every file: [(source, output path or None, message)], one entry per file; a file that cannot be read or
    processed is reported and the batch goes on. `progress(i, n, src)` is called before each file."""
    results = []
    for i, src in enumerate(files):
        if progress:
            progress(i, len(files), src)
        try:
            img, dpi = load_image(src)
            out = run_recipe(img, steps)
            dest = batch_output_path(src, out_dir, fmt, suffix, overwrite, root_dir)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            save_image(out, dest, dpi, jpeg_quality)
            results.append((src, dest, "ok"))
        except Exception as ex:
            results.append((src, None, "%s: %s" % (type(ex).__name__, ex)))
    return results


# ---- settings: recent files, window, folders --------------------------------------------------------------------------------------------
def settings_path():
    """Where the settings live: %APPDATA%\\ImageForge on Windows, ~/.config/ImageForge elsewhere (IMAGEFORGE_SETTINGS overrides)."""
    env = os.environ.get("IMAGEFORGE_SETTINGS")
    if env:
        return env
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "ImageForge", "settings.json")


def load_settings(path=None):
    """The saved settings as a dict; {} when there are none or the file is damaged (it is never an error)."""
    try:
        with open(path or settings_path(), encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(d, path=None):
    """Write the settings; failures (a read-only profile ...) are ignored: settings are a convenience."""
    path = path or settings_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def add_recent(settings, path, limit=10):
    """Put `path` at the top of settings['recent'] (no duplicates, at most `limit`)."""
    path = os.path.abspath(path)
    key = os.path.normcase(path)
    rest = [q for q in settings.get('recent', []) if os.path.normcase(q) != key]
    settings['recent'] = ([path] + rest)[:limit]
    return settings


def recent_files(settings):
    """The recent files that still exist, newest first."""
    return [q for q in settings.get('recent', []) if isinstance(q, str) and os.path.isfile(q)]


def save_image(img, path, dpi=None, jpeg_quality=95, png_level=9):
    """Write `img` in the format of the extension (PNG, JPEG, TIFF, BMP, WebP)."""
    ext = os.path.splitext(path)[1].lower()
    im = Image.fromarray(img)
    kw = {}
    if dpi and ext in ('.png', '.jpg', '.jpeg', '.tif', '.tiff'):
        kw['dpi'] = dpi
    if ext in ('.jpg', '.jpeg'):
        kw.update(quality=jpeg_quality, subsampling=0)
    elif ext == '.png':
        kw['compress_level'] = int(png_level)
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
        self.autofit = True              # the picture follows the window (opening, resizing) until the user zooms or pans by hand
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
        self._busy, self._busy_after = None, None     # (tool name, thread, start time) while an `external` tool runs in the background
        self._placing = False            # a click/drag is setting the x, y of a 'place' tool
        self._grab = None                # (handle, press point in image pixels, selection at press) while dragging it
        self._hist_win, self._hist_pending, self._hist_key = None, False, None      # the Histogram window
        self.settings = load_settings()  # recent files, last folders, window: written back when the program is closed
        if self.settings.get('ai_dir'):
            set_ai_dir(self.settings['ai_dir'])
        self.fg = self._saved_rgb('fg', (0, 0, 0))                   # foreground / background colours of the drawing tools
        self.bg = self._saved_rgb('bg', (255, 255, 255))
        self._cover, self._paint_prev, self._grad = None, None, None
        self.scale = None                # (units per pixel, unit) of the picture, or None until it is calibrated
        self.measures = []               # finished measurements: dicts kind / pts (image px) / value / text
        self._mpts, self._mdrag, self._mwin, self._seg_end = [], False, None, 'p1'
        self._mlast_t = 0.0
        self._lasso = []                 # image points of the lasso being drawn, and the modifier of its first press
        self._lasso_mod, self._lasso_down, self._lasso_t = 'replace', False, 0.0
        self._outline = (None, [])       # (mask, polylines): the outline of a mask selection, computed once per mask
        self._area_start = None          # press point of a 'Define working area' drag, and the rectangle so far
        self._area_cur = None
        self._area_sync = False          # True while the X/Y/W/H boxes are filled from the area (no change events)
        self._rect_start = None          # press point of an 'Erase rectangle' drag, and the rectangle so far
        self._rect_cur = None
        self._stroke_layer = None        # the layer a brush stroke paints on (None: the Background)
        self._stroke_orig = None         # the target as it was when the stroke began (never modified)
        self._stroke_mask = None         # Clean brush: the painted pixels
        self._clone_off = None           # Clone stamp: the offset kept between strokes, and the source it was made for
        self._clone_last = None
        self._stroke = None              # image being painted by the eraser brush, and the last brush point
        self._last = None
        root.title(APP_NAME)
        root.geometry("1250x900")
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
        root.report_callback_exception = self._on_tk_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._rebuild_recent()
        geo = self.settings.get('geometry')
        try:
            if isinstance(geo, str) and re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", geo):
                gw, gh = (int(v) for v in geo.split('+')[0].split('-')[0].split('x'))
                if 600 <= gw <= root.winfo_screenwidth() and 400 <= gh <= root.winfo_screenheight():
                    root.geometry(geo)
            if self.settings.get('zoomed'):
                root.state('zoomed')
        except (tk.TclError, ValueError):
            pass

    def _on_tk_error(self, exc, val, tb):
        """An error inside a button or mouse handler: pythonw has no console, so without this it would vanish and the
        program would just seem to do nothing. It goes to ImageForge_errors.log and to the status line."""
        text = "".join(traceback.format_exception(exc, val, tb))
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write("%s\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except OSError:
            pass
        if sys.stderr:
            print(text, file=sys.stderr)
        self._status("Internal error: %s  (details in %s)" % (val, ERROR_LOG))
        self._stroke = self._stroke_layer = self._stroke_orig = self._stroke_mask = self._cover = self._grad = None    # never stay half-way in a drag
        self._mv = self._lmove = self._grab = self._rect_start = None
        self._pan_left = self._placing = False

    # ---- construction ----------------------------------------------------------------------------------
    def _build_menu(self):
        mb = tk.Menu(self.root)
        self.root.config(menu=mb)
        self.m_file, self.m_edit, self.m_view = tk.Menu(mb, tearoff=0), tk.Menu(mb, tearoff=0), tk.Menu(mb, tearoff=0)
        self.m_image = tk.Menu(mb, tearoff=0)
        self.m_tools = tk.Menu(mb, tearoff=0)
        self.m_layer = tk.Menu(mb, tearoff=0)
        self.m_measure = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="File", menu=self.m_file)
        mb.add_cascade(label="Edit", menu=self.m_edit)
        mb.add_cascade(label="View", menu=self.m_view)
        mb.add_cascade(label="Tools", menu=self.m_tools)
        mb.add_cascade(label="Image", menu=self.m_image)
        mb.add_cascade(label="Measure", menu=self.m_measure)
        self.m_recipe = tk.Menu(mb, tearoff=0)
        self.m_help = tk.Menu(mb, tearoff=0)
        mb.add_cascade(label="Recipe", menu=self.m_recipe)
        rc = self.m_recipe
        rc.add_command(label="Save the steps so far as a recipe...", command=self.save_recipe_dialog)
        rc.add_command(label="Run a recipe on this picture...", command=self.run_recipe_here)
        rc.add_command(label="Run a recipe on a folder of pictures...", command=self.batch_dialog)
        rc.add_separator()
        rc.add_command(label="Show the steps (History tab)", command=lambda: self.nbk.select(2))
        mb.add_cascade(label="Layer", menu=self.m_layer)
        mb.add_cascade(label="Help", menu=self.m_help)
        self.m_help.add_command(label="Keyboard shortcuts and mouse...", accelerator="F1", command=self.show_shortcuts)
        self.m_help.add_command(label="Guide: removing objects...", command=self.show_object_guide)
        ms = self.m_measure
        ms.add_command(label="Measure tool", command=lambda: self.tree.selection_set("Measure"))
        ms.add_command(label="Measurements...", command=self.show_measurements)
        ms.add_separator()
        ms.add_command(label="Calibrate from a measured length...", command=self.calibrate)
        ms.add_command(label="Set the scale by hand...", command=self.scale_by_hand)
        ms.add_command(label="Scale from the picture's dpi", command=self.scale_from_dpi)
        ms.add_command(label="Clear the scale", command=self.clear_scale)
        ms.add_separator()
        ms.add_command(label="Add a scale bar...", command=lambda: self.tree.selection_set("Scale bar"))
        f = self.m_file
        f.add_command(label="Open...", accelerator="Ctrl+O", command=self.open_dialog)
        self.m_recent = tk.Menu(f, tearoff=0)
        f.add_cascade(label="Open recent", menu=self.m_recent)
        f.add_command(label="Open as layer...", command=self.open_layer_dialog)
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        f.add_command(label="Save As...", accelerator="Ctrl+Shift+S", command=self.save_as)
        f.add_command(label="Export...", accelerator="Ctrl+E", command=self.export_dialog)
        f.add_separator()
        f.add_command(label="Exit", command=self.on_close)
        e = self.m_edit
        e.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        e.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        e.add_separator()
        e.add_command(label="Paste (new layer, or open)", accelerator="Ctrl+V", command=self.paste_clipboard)
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
        v.add_separator()
        v.add_command(label="Histogram...", accelerator="Ctrl+H", command=self.show_histogram)

    def _build_ui(self):
        paned = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, sashwidth=5, bg='#bbbbbb')
        paned.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(paned)
        paned.add(left, stretch='always')
        bar = tk.Frame(left, bg='#e8e8e8')
        bar.pack(fill=tk.X)
        for text, cmd in (("Center", self.center_view), ("Fit", self.fit_view), ("100%", self.actual_size)):
            b = tk.Button(bar, text=text, command=cmd, width=7)
            b.pack(side=tk.LEFT, padx=(4, 0), pady=2)
            if text == "Fit":
                self.b_fit = b
        self.v_hand = tk.BooleanVar(value=False)                      # the Hand button is in the toolbox (and the H key)
        tk.Label(bar, text="Move the picture: hold Space and drag, or drag with the right button (the Hand in the toolbox: H)",
                 bg='#e8e8e8', fg='#666666', font=('Arial', 8)).pack(side=tk.LEFT, padx=10)
        # the colours of the drawing tools: foreground, background, swap (X), black and white (D)
        tk.Button(bar, text="swap (X)", command=self.swap_colours, font=('Arial', 8)).pack(side=tk.RIGHT, padx=(2, 6), pady=2)
        self.b_bg = tk.Button(bar, width=3, relief=tk.SUNKEN, command=lambda: self.choose_colour('bg'))
        self.b_bg.pack(side=tk.RIGHT, padx=2, pady=2)
        self.b_fg = tk.Button(bar, width=3, relief=tk.SUNKEN, command=lambda: self.choose_colour('fg'))
        self.b_fg.pack(side=tk.RIGHT, padx=2, pady=2)
        tk.Label(bar, text="Colours", bg='#e8e8e8', font=('Arial', 8)).pack(side=tk.RIGHT, padx=(8, 0))
        self._draw_swatches()
        body = tk.Frame(left)
        body.pack(fill=tk.BOTH, expand=True)
        self.toolbox = tk.Frame(body, bg='#e8e8e8', width=74)
        self.toolbox.pack(side=tk.LEFT, fill=tk.Y)
        self.canvas = tk.Canvas(body, bg='#3a3a3a', highlightthickness=0, cursor='crosshair')
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
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

        # The working area: set it first, before choosing any tool; every tool then acts only inside it.
        wa = tk.LabelFrame(panel, text="Working area: every tool acts only inside it (draw it with the Select tools, top left)",
                           bg=bg, font=('Arial', 8))
        wa.pack(fill=tk.X, padx=8, pady=(6, 0))
        row = tk.Frame(wa, bg=bg)
        row.pack(fill=tk.X)
        tk.Button(row, text="Whole image (no area)", command=self.clear_area).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(4, 2), pady=3)
        tk.Button(row, text="Invert", command=self.invert_sel).pack(side=tk.LEFT, padx=(2, 4), pady=3)
        row = tk.Frame(wa, bg=bg)
        row.pack(fill=tk.X, padx=4)
        self.area_vars = {k: tk.StringVar() for k in 'XYWH'}
        for k in 'XYWH':
            tk.Label(row, text=k, bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(4, 1))
            sp = ttk.Spinbox(row, from_=0, to=100000, increment=1, textvariable=self.area_vars[k], width=6, format='%.0f',
                             command=self._apply_area_box)
            sp.pack(side=tk.LEFT)
            sp.bind('<Return>', lambda ev: self._apply_area_box())
            sp.bind('<FocusOut>', lambda ev: self._apply_area_box())
        self.l_area = tk.Label(wa, text="Whole image", bg=bg, fg='#555555', font=('Arial', 8), anchor='w')
        self.l_area.pack(fill=tk.X, padx=6, pady=(0, 2))
        # The settings of the shape selections (the wand and the lasso start from the toolbox) and the commands that change a selection
        row = tk.Frame(wa, bg=bg)
        row.pack(fill=tk.X, padx=4, pady=(2, 0))
        tk.Label(row, text="Tolerance", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(2, 1))
        self.v_wand_tol = tk.IntVar(value=32)
        ttk.Spinbox(row, from_=0, to=441, increment=4, textvariable=self.v_wand_tol, width=4).pack(side=tk.LEFT)
        self.v_wand_contig = tk.BooleanVar(value=True)
        tk.Checkbutton(row, text="Contiguous", variable=self.v_wand_contig, bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(6, 0))
        row = tk.Frame(wa, bg=bg)
        row.pack(fill=tk.X, padx=4, pady=(0, 3))
        self.v_selpx = tk.IntVar(value=3)
        ttk.Spinbox(row, from_=1, to=200, increment=1, textvariable=self.v_selpx, width=4).pack(side=tk.LEFT, padx=(2, 1))
        tk.Label(row, text="px", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT)
        for text, cmd in (("Grow", lambda: self.grow_sel(1)), ("Shrink", lambda: self.grow_sel(-1)), ("Feather", self.feather_sel)):
            tk.Button(row, text=text, command=cmd).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        row = tk.Frame(panel, bg=bg)
        row.pack(fill=tk.X, padx=8, pady=(4, 0))
        self.v_content = tk.BooleanVar(value=False)
        tk.Checkbutton(row, text="Drag moves the content", variable=self.v_content, bg=bg, font=('Arial', 9)).pack(side=tk.LEFT)
        tk.Label(row, text="leaves", bg=bg, font=('Arial', 8)).pack(side=tk.LEFT, padx=(6, 2))
        self.v_fill = tk.StringVar(value='white')
        ttk.Combobox(row, textvariable=self.v_fill, values=('white', 'black', 'background', 'transparent'), state='readonly',
                     width=10).pack(side=tk.LEFT)
        tk.Label(panel, text="Ctrl does the opposite of the box, Shift copies.", bg=bg, fg='#555555', font=('Arial', 8),
                 anchor='w').pack(fill=tk.X, padx=10)

        self.nbk = ttk.Notebook(panel)
        self.nbk.pack(fill=tk.BOTH, expand=True, padx=4, pady=(6, 0))
        # Tools tab: the tool list on top and the parameters below, with a draggable divider between them (the list is
        # tall, so the parameters would otherwise sit below the fold).
        self.tools_pane = ttk.PanedWindow(self.nbk, orient='vertical')
        self.nbk.add(self.tools_pane, text="  Tools  ")
        tab_layers = tk.Frame(self.nbk, bg=bg)
        self.nbk.add(tab_layers, text="  Layers  ")
        tab_hist = tk.Frame(self.nbk, bg=bg)
        self.nbk.add(tab_hist, text="  History  ")
        self._build_history_tab(tab_hist, bg)
        self._build_layers_tab(tab_layers, bg)
        tf = tk.Frame(self.tools_pane, bg=bg)
        self.tools_pane.add(tf, weight=0)
        outer, tab = self._scrollable(self.tools_pane, bg)
        self.tools_pane.add(outer, weight=1)
        # The model of the tool list (every tool by group). It is never shown: the toolbox, the chips and the search below are its faces,
        # and everything else (menus, keys, scripts) selects a tool by selecting it here.
        self.tree = ttk.Treeview(tk.Frame(self.root), show='tree', selectmode='browse')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._on_tree_select())
        self.v_find = tk.StringVar()
        self.e_find = ttk.Entry(tf, textvariable=self.v_find)
        self.e_find.pack(fill=tk.X, padx=8, pady=(6, 2))
        self.v_find.trace_add('write', lambda *a: self._refill_ops())
        self.l_ph = tk.Label(self.e_find, text="Find a tool (Ctrl+K)", fg='#999999', bg='white', font=('Arial', 9))
        self.l_ph.place(x=3, rely=0.5, anchor='w')
        self.l_ph.bind('<Button-1>', lambda e: self.e_find.focus_set())
        self.v_find.trace_add('write', lambda *a: self.l_ph.place_forget() if self.v_find.get() else self.l_ph.place(x=3, rely=0.5, anchor='w'))
        self.e_find.bind('<Return>', lambda e: self._pick_first_op())
        self.e_find.bind('<Down>', lambda e: (self.ops.focus_set(), self._focus_first_op()))
        self.e_find.bind('<Escape>', lambda e: (self.v_find.set(""), self.canvas.focus_set(), "break")[-1])
        self.f_chips = tk.Frame(tf, bg=bg)
        self.f_chips.pack(fill=tk.X, padx=8)
        self.v_group = tk.StringVar()
        of = tk.Frame(tf, bg=bg)
        of.pack(fill=tk.BOTH, expand=True, padx=8, pady=(2, 4))
        self.ops = ttk.Treeview(of, show='tree', selectmode='browse', height=8)
        osb = ttk.Scrollbar(of, orient='vertical', command=self.ops.yview)
        self.ops.configure(yscrollcommand=osb.set)
        osb.pack(side=tk.RIGHT, fill=tk.Y)
        self.ops.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.ops.bind('<<TreeviewSelect>>', lambda e: self._on_ops_select())
        self._ops_sync = False
        self.root.after(500, self._set_tools_divider)

        self.l_doc = tk.Label(tab, text="", bg=bg, fg='#555555', font=('Arial', 8), wraplength=270, justify='left',
                              anchor='w')
        self.l_doc.pack(fill=tk.X, padx=10, pady=(4, 0))
        self.params_frame = tk.Frame(tab, bg=bg)
        self.params_frame.pack(fill=tk.X, padx=8, pady=2)
        c = self.canvas
        c.bind('<Configure>', lambda e: self._on_canvas_resize())
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
        c.bind('<x>', lambda e: self.swap_colours())
        c.bind('<d>', lambda e: self.reset_colours())
        c.bind('<Return>', lambda e: (self._lasso_close() if self.mode == 'lasso' else None, self._measure_enter()))
        c.bind('<Leave>', lambda e: (self._set_info(""), c.delete('brush')))
        if HAS_DND:
            for w in (self.canvas, panel):
                w.drop_target_register(DND_FILES)
                w.dnd_bind('<<Drop>>', self._on_drop)

    def _bind_keys(self):
        r = self.root
        r.bind('<KeyPress-space>', self._space_down)
        r.bind('<KeyPress-bracketleft>', lambda e: self._bracket(-1, e))
        r.bind('<KeyPress-bracketright>', lambda e: self._bracket(1, e))
        r.bind('<KeyPress-h>', self._hand_key)
        for key in ('Delete', 'BackSpace'):
            r.bind('<KeyPress-%s>' % key, self._delete_key)
            r.bind('<Shift-KeyPress-%s>' % key, lambda e: self._delete_key(e, True))
        r.bind('<KeyRelease-space>', self._space_up)
        for seq, fn in (("<Control-o>", self.open_dialog), ("<Control-s>", self.save),
                        ("<Control-S>", self.save_as), ("<Control-z>", self.undo), ("<Control-y>", self.redo),
                        ("<Control-a>", self.select_all), ("<Control-d>", self.deselect), ("<Control-i>", self.invert_sel),
                        ("<Control-e>", self.export_dialog), ("<Control-v>", self.paste_clipboard),
                        ("<Control-h>", self.show_histogram), ("<F1>", self.show_shortcuts), ("<Control-k>", self.focus_find), ("<Control-Z>", self.redo),
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

    @staticmethod
    def _typing(event):
        """True when a key goes to a box the user is typing in (the single-letter shortcuts must leave it alone)."""
        return isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Spinbox, ttk.Combobox, tk.Text, tk.Listbox))

    def _bracket(self, direction, event):
        """`[` and `]`: a smaller / larger brush for the tool that has a Size box (pencil, brushes, clone, eraser ...)."""
        if self._typing(event) or self.tool is None or 'size' not in self.pvars:
            return None
        prm = next(q for q in self.tool.params if q.key == 'size')
        try:
            cur = int(round(float(self.pvars['size'].get())))
        except (tk.TclError, ValueError):
            return None
        new = step_size(cur, direction, int(prm.lo or 1), int(prm.hi or 1000))
        if new != cur:
            self.pvars['size'].set(new)
            self._status("%s size %d px." % (self.tool.name, new), ok=True)
        return "break"

    def _hand_key(self, event):
        """H: the Hand (the left button moves the picture) on / off, as in most editors."""
        if self._typing(event):
            return None
        self.v_hand.set(not self.v_hand.get())
        self._hand_changed()
        return "break"

    def _delete_key(self, event, foreground=False):
        """Delete / Backspace: clear the selection (the background colour on the Background, transparent on an image layer);
        with Shift: fill it with the foreground colour."""
        if self._typing(event) or isinstance(event.widget, ttk.Treeview):
            return None
        self.clear_selection(foreground)
        return "break"

    def clear_selection(self, foreground=False):
        if not self._need_image():
            return
        if self.sel is None:
            self._status("Select an area first (Delete clears it with the background colour, Shift+Delete fills it with the foreground).")
            return
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        if L is None:
            out = clear_region(self.hist.current, self.fg if foreground else self.bg, self.sel)
            same = out is self.hist.current
        else:
            region = layer_region(self.sel, L)
            if region[2] <= region[0] or region[3] <= region[1]:
                self._status("The selection does not touch layer '%s'." % L.name)
                return
            out = clear_region(L.rgba, self.fg if foreground else None, region)
            same = out is L.rgba
        label = "Fill with the foreground colour" if foreground else "Clear the selection"
        if same:
            self._status("Nothing changed (already that colour, or outside the layer).")
            return
        if L is None:
            self._commit(label, out)
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self._commit_layers(label + " (layer)", layers)
        self._status("%s." % label, ok=True)

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
        """Fill the tool list (built-ins and plug-ins) in the order of TOOL_LAYOUT. The groups that hold mouse tools (a pencil, a clone
        stamp, text ...) go in the Tools menu, the others (one-shot operations with a preview) in the Image menu."""
        self.tree.delete(*self.tree.get_children())
        self.m_image.delete(0, tk.END)
        self.m_tools.delete(0, tk.END)
        groups = {}
        for t in TOOLS.values():
            groups.setdefault(t.group, []).append(t)
        order = [g for g in TOOL_LAYOUT if g in groups] + [g for g in groups if g not in TOOL_LAYOUT]
        for g in order:
            names = TOOL_LAYOUT.get(g, [])
            ts = sorted(groups[g], key=lambda t: names.index(t.name) if t.name in names else len(names))
            self.tree.insert('', tk.END, iid='g:' + g, text=g, open=True)
            menu = self.m_tools if any(t.interactive or t.layer for t in ts) else self.m_image
            sub = tk.Menu(menu, tearoff=0)
            menu.add_cascade(label=g, menu=sub)
            for t in ts:
                self.tree.insert('g:' + g, tk.END, iid=t.name, text=t.name)
                sub.add_command(label=t.name, command=lambda n=t.name: self.tree.selection_set(n))
        self.m_tools.add_separator()
        self.m_tools.add_command(label="AI programs folder...", command=self.choose_ai_dir)
        self._build_toolbox()
        self._build_chips(order)

    def choose_ai_dir(self, ask=True):
        """Tools > AI programs folder...: where Real-ESRGAN / waifu2x / the LaMa model are (the folder that holds them, or one of them). Remembered in the settings.
        Returns True when at least one of the programs is found there."""
        d = filedialog.askdirectory(parent=self.root, title="Folder with realesrgan-ncnn-vulkan and / or waifu2x-ncnn-vulkan",
                                    initialdir=AI_DIR or self.settings.get('last_dir') or os.path.expanduser("~")) if ask else None
        if not d:
            return False
        found = [AI_PROGRAMS[k] for k in AI_PROGRAMS if find_ai_program(k, d)] + (["the LaMa model"] if find_ai_model("lama", d) else [])
        if not found:
            messagebox.showwarning(APP_NAME, "Neither realesrgan-ncnn-vulkan, waifu2x-ncnn-vulkan nor a lama*.onnx model was found in that folder (or "
                                   "in the folders inside it).")
            return False
        self.settings['ai_dir'] = d
        set_ai_dir(d)
        save_settings(self.settings)                              # remembered at once, not only when the program is closed
        self._status("AI programs folder set: found %s." % " and ".join(found), ok=True)
        return True

    def _tip(self, widget, text):
        """A hint under `widget` after half a second of hovering."""
        state = {'after': None, 'win': None}

        def show():
            w = tk.Toplevel(widget)
            w.wm_overrideredirect(True)
            w.wm_geometry("+%d+%d" % (widget.winfo_rootx() + widget.winfo_width() + 4, widget.winfo_rooty() + 4))
            tk.Label(w, text=text, bg='#fff8c6', relief=tk.SOLID, borderwidth=1, font=('Arial', 9), padx=4).pack()
            state['win'] = w

        def hide(_=None):
            if state['after']:
                widget.after_cancel(state['after'])
                state['after'] = None
            if state['win']:
                state['win'].destroy()
                state['win'] = None
        widget.bind('<Enter>', lambda e: state.update(after=widget.after(500, show)), add='+')
        widget.bind('<Leave>', hide, add='+')
        widget.bind('<ButtonPress>', hide, add='+')

    def _build_toolbox(self):
        """The mouse tools as icon buttons, a group after the other, two to a line: one click selects the tool."""
        for w in self.toolbox.winfo_children():
            w.destroy()
        self._icons = {}
        self.v_toolsel = tk.StringVar(value="")
        self.b_modes = {}
        for gi, (group, names) in enumerate(TOOLBOX):
            tk.Label(self.toolbox, text=group, bg='#e8e8e8', fg='#666666', font=('Arial', 7)).pack(anchor='w', padx=4, pady=(6 if gi else 3, 0))
            grid = tk.Frame(self.toolbox, bg='#e8e8e8')
            grid.pack(padx=3)
            col = 0
            for name in names:
                if name in TOOLS or name in TOOLBOX_MODES or name == "Hand":
                    img = ImageTk.PhotoImage(tool_icon(name))
                    self._icons[name] = img
                    common = dict(image=img, width=30, height=30, bg='#f4f4f4', activebackground='#dfeaff', bd=1)
                    if name in TOOLS:
                        b = tk.Radiobutton(grid, variable=self.v_toolsel, value=name, indicatoron=False, selectcolor='#bcd6ff',
                                           command=lambda n=name: self.tree.selection_set(n), **common)
                        hint = "%s  (%s)" % (name, TOOLS[name].group)
                    elif name == "Hand":
                        b = tk.Checkbutton(grid, variable=self.v_hand, indicatoron=False, selectcolor='#bcd6ff', command=self._hand_changed, **common)
                        hint = "Hand: the left button moves the picture  (H, or hold Space)"
                    else:
                        b = tk.Button(grid, command={"Rectangle": self.start_area, "Magic wand": self.start_wand, "Lasso": self.start_lasso}[name], **common)
                        self.b_modes[name] = b
                        hint = {"Rectangle": "Rectangle: drag the working area (every tool then acts only inside it)",
                                "Magic wand": "Magic wand: click a colour to select it  (Shift adds, Ctrl removes)",
                                "Lasso": "Lasso: draw around what to select  (double-click closes)"}[name]
                    b.grid(row=col // 2, column=col % 2, padx=1, pady=1)
                    self._tip(b, hint)
                    col += 1
        self._sync_toolbox()

    def _sync_toolbox(self):
        """The toolbox follows the tool in use (a one-shot operation leaves it unselected) and the wand / lasso modes."""
        if getattr(self, 'v_toolsel', None) is None:
            return
        name = self.tool.name if self.tool is not None else ""
        self.v_toolsel.set(name if any(name in names for _, names in TOOLBOX) else "")
        for n, b in getattr(self, 'b_modes', {}).items():
            on = self.mode == TOOLBOX_MODES[n]
            b.config(relief=tk.SUNKEN if on else tk.RAISED, bg='#bcd6ff' if on else '#f4f4f4')

    def _build_chips(self, order):
        """One chip per group of one-shot operations; the list under them shows the chosen group (or the search result)."""
        for w in self.f_chips.winfo_children():
            w.destroy()
        self._chip_groups = [g for g in order if not any(t.interactive or t.layer for t in TOOLS.values() if t.group == g)]
        for g in self._chip_groups:
            b = tk.Radiobutton(self.f_chips, text=GROUP_SHORT.get(g, g), variable=self.v_group, value=g, indicatoron=False,
                               selectcolor='#bcd6ff', bg='#f4f4f4', font=('Arial', 8), padx=5, command=self._refill_ops)
            b.pack(side=tk.LEFT, padx=(0, 2))
            self._tip(b, g)
        if self._chip_groups and self.v_group.get() not in self._chip_groups:
            self.v_group.set(self._chip_groups[0])
        self._refill_ops()

    def _refill_ops(self):
        """The list under the chips: the tools of the chosen group, or what the search box finds (any tool, with its group)."""
        self._ops_sync = True
        try:
            self.ops.delete(*self.ops.get_children())
            q = self.v_find.get().strip()
            if q:
                for n in search_tools(q):
                    self.ops.insert('', tk.END, iid=n, text="%s   (%s)" % (n, TOOLS[n].group))
            else:
                g = self.v_group.get()
                for t in sorted((t for t in TOOLS.values() if t.group == g), key=lambda t: (TOOL_LAYOUT.get(g, []) + [t.name]).index(t.name)):
                    self.ops.insert('', tk.END, iid=t.name, text=t.name)
            if self.tool is not None and self.ops.exists(self.tool.name):
                self.ops.selection_set(self.tool.name)
        finally:
            self._ops_sync = False

    def _on_ops_select(self):
        if self._ops_sync:
            return
        sel = self.ops.selection()
        if sel and (self.tool is None or sel[0] != self.tool.name):
            self.tree.selection_set(sel[0])

    def _focus_first_op(self):
        kids = self.ops.get_children()
        if kids:
            self.ops.selection_set(kids[0])
            self.ops.focus(kids[0])

    def _pick_first_op(self):
        kids = self.ops.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.canvas.focus_set()

    def focus_find(self):
        self.nbk.select(0)
        self.e_find.focus_set()
        self.e_find.select_range(0, tk.END)

    # ---- tools and parameters --------------------------------------------------------------------------
    def _set_tools_divider(self, y=275):
        """Start with a tall tool list (about three times the first version); the divider can be dragged."""
        try:
            self.tools_pane.sashpos(0, y)
        except tk.TclError:
            pass

    def _on_tree_select(self):
        sel = self.tree.selection()
        if sel and sel[0] in TOOLS:
            self.select_tool(sel[0])

    def select_tool(self, name):
        self.cancel_mode()
        self._measure_cancel()
        self.tool = TOOLS[name]
        self._sync_toolbox()
        if not self.v_find.get().strip() and self.tool.group in getattr(self, '_chip_groups', []) and self.v_group.get() != self.tool.group:
            self.v_group.set(self.tool.group)
            self._refill_ops()
        elif self.ops.exists(name) and self.ops.selection() != (name,):
            self._ops_sync = True
            self.ops.selection_set(name)
            self._ops_sync = False
        self.canvas.delete('brush')
        self.canvas.delete('clone')
        self.l_doc.config(text=self.tool.doc + self._quick_pick_hint(self.tool))
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
                elif p.pick == 'faces':
                    r2 = tk.Frame(self.params_frame, bg=bg)
                    r2.pack(fill=tk.X, pady=(2, 0))
                    tk.Button(r2, text="Find faces", command=lambda k=p.key: self.find_faces_into(k), bg='#cdeefc').pack(side=tk.LEFT, expand=True, fill=tk.X)
                    tk.Button(r2, text="Mark faces", command=lambda k=p.key: self.start_pick('faces', k), bg='#cdeefc').pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
                    tk.Button(r2, text="Clear", command=lambda v=var: v.set("")).pack(side=tk.LEFT)
            else:
                var = tk.DoubleVar(value=p.default)
                tk.Label(self.params_frame, text=p.label, bg=bg, anchor='w', font=('Arial', 9)).pack(fill=tk.X)
                r = tk.Frame(self.params_frame, bg=bg)
                r.pack(fill=tk.X)
                lo = p.lo if p.lo is not None else -1e9
                hi = p.hi if p.hi is not None else 1e9
                if p.slider and p.progressive and p.lo is not None and p.hi is not None:
                    span = float(hi - lo)
                    pos = tk.DoubleVar(value=math.sqrt(max(0.0, (p.default - lo) / span)))

                    def _slide(v, p=p, var=var, lo=lo, span=span):
                        var.set(p.coerce(round(lo + span * float(v) ** 2, p.decimals)))

                    def _follow(*a, var=var, pos=pos, lo=lo, span=span):
                        try:
                            t = math.sqrt(min(1.0, max(0.0, (float(var.get()) - lo) / span)))
                        except (tk.TclError, ValueError):
                            return                      # mid-typing
                        if abs(pos.get() - t) > 1e-6:
                            pos.set(t)

                    var.trace_add('write', _follow)
                    ttk.Scale(r, from_=0.0, to=1.0, variable=pos, orient='horizontal', command=_slide
                              ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))
                elif p.slider and p.lo is not None and p.hi is not None:
                    ttk.Scale(r, from_=lo, to=hi, variable=var, orient='horizontal',
                              command=lambda v, p=p, var=var: var.set(p.coerce(round(float(v), p.decimals)))
                              ).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=(0, 6))
                ttk.Spinbox(r, from_=lo, to=hi, increment=p.step, textvariable=var, width=8,
                            format='%.' + str(p.decimals) + 'f').pack(side=tk.RIGHT)
                if p.pick == 'angle':
                    tk.Button(self.params_frame, text="Measure: click two points of a line to level",
                              command=lambda k=p.key: self.start_pick('angle', k)).pack(fill=tk.X, pady=(2, 0))
                elif p.pick == 'quad':
                    tk.Button(self.params_frame, text="Click the four corners on the picture",
                              command=lambda k=p.key: self.start_pick('quad', k), bg='#cdeefc').pack(fill=tk.X, pady=(2, 0))
                elif p.pick == 'point':
                    tk.Button(self.params_frame, text="Set source: click on the picture",
                              command=lambda k=p.key: self.start_pick('point', k), bg='#cdeefc').pack(fill=tk.X, pady=(2, 0))
            var.trace_add('write', lambda *a: self._params_changed())
            self.pvars[p.key] = var
        if self.tool.name == "Remove object (generative)":
            self._build_or_panel()
        self._params_ready = True

    def _build_or_panel(self):
        """Under the parameters of Remove object (generative): the OpenRouter credit as far as the program knows, a button to read it from the account, and
        the guide. The credit is read once per program run, when the tool is first selected and a key exists."""
        bg = '#f0f4f7'
        f = tk.Frame(self.params_frame, bg=bg)
        f.pack(fill=tk.X, pady=(6, 0))
        self.l_or = tk.Label(f, text="", bg=bg, anchor='w', font=('Arial', 9, 'bold'))
        self.l_or.pack(fill=tk.X)
        r = tk.Frame(f, bg=bg)
        r.pack(fill=tk.X)
        tk.Button(r, text="Refresh credit", command=lambda: self.refresh_or_credit(True)).pack(side=tk.LEFT)
        tk.Button(r, text="Guide...", command=self.show_object_guide, bg='#cdeefc').pack(side=tk.LEFT, padx=4)
        if OR_STATE['credit'] is None and openrouter_key() and not OR_STATE.get('asked'):
            OR_STATE['asked'] = True
            self.refresh_or_credit(False)
        self._update_or_label()

    def _update_or_label(self):
        lab = getattr(self, 'l_or', None)
        if lab is None or not lab.winfo_exists():
            return
        if not openrouter_key():
            lab.config(text="No OpenRouter key set (see the Guide).", fg='#b03a2e')
        elif OR_STATE['credit'] is None:
            lab.config(text="OpenRouter credit: unknown (Refresh).", fg='#444444')
        else:
            lab.config(text="OpenRouter credit: %.2f USD" % OR_STATE['credit'], fg='#1e6b2e')

    def refresh_or_credit(self, say):
        key = openrouter_key()
        if not key:
            if say:
                self._status("No OpenRouter key: set OPENROUTER_API_KEY (Help > Guide: removing objects).")
            self._update_or_label()
            return
        try:
            OR_STATE['credit'] = or_credit(key)
            if say:
                self._status("OpenRouter credit: %.4f USD (the account's count can lag behind by a few requests)." % OR_STATE['credit'], ok=True)
        except Exception as ex:
            if say:
                self._status("Could not read the credit: %s" % ex)
        self._update_or_label()

    def show_object_guide(self):
        """Help > Guide: removing objects: which of the four tools to use, how to use the generative one, tips, money and privacy, limits."""
        win = getattr(self, '_guide_win', None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        w = tk.Toplevel(self.root)
        w.title("Guide: removing objects - " + APP_NAME)
        w.geometry("860x700")
        self._guide_win = w
        txt = tk.Text(w, wrap='word', font=('Arial', 10), padx=12, pady=10, bg='#fbfbf8', relief=tk.FLAT)
        sb = ttk.Scrollbar(w, orient='vertical', command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        txt.pack(fill=tk.BOTH, expand=True)
        txt.tag_configure('h', font=('Arial', 11, 'bold'), spacing1=10, spacing3=3)
        for line in OBJECT_GUIDE.strip("\n").split("\n"):
            head = line.isupper() and len(line) > 6
            txt.insert(tk.END, line + "\n", 'h' if head else ())
        txt.config(state='disabled')

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
        self._hist_schedule()
        self._draw_shape_handles()
        self._draw_faces_marker()
        self._clone_off = None if self._clone_source() != self._clone_last else self._clone_off
        self._draw_clone_marker()
        if self._after is not None:
            self.root.after_cancel(self._after)
        self._after = self.root.after(60, self._update_preview)

    def _update_preview(self):
        self._after = None
        if (self.hist is None or self.tool is None or self._stroke is not None or self._mv is not None or self._grad is not None
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
        if self.tool.geometry and self.tool.layer_shift and self.hist.layers:      # Canvas size, aspect crop: the layers move along
            try:
                out = apply_tool(self.tool.name, self.hist.current, params, None)
            except Exception as ex:
                self._status("%s failed: %s" % (self.tool.name, ex))
                return
            if out is self.hist.current or (out.shape == self.hist.current.shape and np.array_equal(out, self.hist.current)):
                self._status("%s: no change with these parameters." % self.tool.name)
                return
            dx, dy = self.tool.layer_shift(params, self.hist.current.shape)
            self.sel = None
            self._commit(self.tool.name, out, shift_layers(self.hist.layers, dx, dy), tool_step(self.tool.name, params))
            self._status("%s applied (the layers keep their place on the picture)." % self.tool.name, ok=True)
            return
        cur = self.hist.current
        region = None if self.tool.geometry else self.sel
        flatten = bool(self.tool.geometry and self.hist.layers)
        if self._busy:
            self._status("%s is still running: Esc stops it." % self._busy[0])
            return
        if flatten:
            if not messagebox.askyesno(APP_NAME, "'%s' changes the whole picture, so the layers are merged into the "
                                       "background first. Continue?" % self.tool.name):
                return
            cur = self._flat()
        if self.tool.external:
            self._start_external(cur, region, params, flatten)
            return
        try:
            out = apply_tool(self.tool.name, cur, params, region)
        except Exception as ex:
            self._status("%s failed: %s" % (self.tool.name, ex))
            return
        if not flatten and (out is cur or (out.shape == cur.shape and np.array_equal(out, cur))):
            self.preview = None
            self._redraw()
            note = TOOL_NOTES.pop(self.tool.name, "")
            self._status("%s: no change with these parameters%s." % (self.tool.name, " (%s)" % note if note else ""))
            return
        if flatten:
            self.active = None
        self._commit(self.tool.name, out, () if flatten else None, self._recipe_step_for(params))
        extra = " (applied to the whole image: this tool ignores the selection)" if self.sel and self.tool.geometry else ""
        note = TOOL_NOTES.pop(self.tool.name, "")
        if note:
            extra += " (%s)" % note
        if self.hist.layers:
            extra += " (on the Background: select a layer to filter it)"
        self._status("%s applied%s." % (self.tool.name, extra), ok=True)

    def _start_external(self, cur, region, params, flatten):
        """Run an `external` tool (a program that can take minutes) in a worker thread: the window stays alive, the status line counts the
        seconds, Esc stops it (AI_CANCEL). The result is committed in the Tk thread when it is done, unless the history moved on meanwhile."""
        t = self.tool
        if t.takes_region and region is None:
            self._status("%s needs a selection: draw the object with the Lasso, the Magic wand or the Rectangle (the toolbox, top left), then press Apply." % t.name)
            return
        if t.name == "Remove object (AI)":
            try:
                import onnxruntime                                                  # noqa: F401
            except ImportError:
                messagebox.showwarning(APP_NAME, "Remove object (AI) needs the Python package onnxruntime. Install it with:\n\n    pip install onnxruntime")
                return
            if find_ai_model("lama") is None:
                if not messagebox.askyesno(APP_NAME, "The LaMa model (lama_fp32.onnx) is not set up yet. Choose the folder that holds it now?"):
                    return
                if not self.choose_ai_dir() or find_ai_model("lama") is None:
                    return
        if t.name == "Remove object (generative)":
            if not str(params.get('what', "")).strip():
                self._status("Describe what to remove in the box 'What to remove' (for example: the green water bottle), then press Apply.")
                return
            key = openrouter_key()
            if not key:
                messagebox.showwarning(APP_NAME, "No OpenRouter key was found. Set the environment variable OPENROUTER_API_KEY (once, in PowerShell):\n\n"
                                       "    [Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY','your-key','User')\n\nand restart ImageForge. "
                                       "Help > Guide: removing objects explains it.")
                return
            try:
                pieces = gen_pieces(cur, region, int(params.get('grow', 8)))
            except ValueError as ex:
                self._status(str(ex))
                return
            if not pieces:
                self._status("The selection is too small to remove.")
                return
            model = params.get('model', GEN_MODELS[0])
            est = gen_estimate(model, len(pieces))
            if OR_STATE['credit'] is None:
                self.refresh_or_credit(False)
            faces = sum(len(find_faces(cur[pc['y0']:pc['y0'] + pc['side'], pc['x0']:pc['x0'] + pc['side']])) for pc in pieces)
            lines = ["This will send %d crop%s of your picture (%s) to OpenRouter, which passes %s to %s, with the words: \"%s\"." % (
                len(pieces), "" if len(pieces) == 1 else "s", ", ".join("%d x %d px" % (pc['sent'], pc['sent']) for pc in pieces),
                "it" if len(pieces) == 1 else "them", model, str(params['what']).strip()),
                "Only the crop leaves this computer, not the whole picture.", "",
                "Estimated cost: about %.3f USD (%d request%s)." % (est, len(pieces), "" if len(pieces) == 1 else "s"),
                ("Credit: about %.2f USD." % OR_STATE['credit']) if OR_STATE['credit'] is not None else "Credit: unknown."]
            if faces:
                lines += ["", "WARNING: %d face%s found in the part to be sent." % (faces, "" if faces == 1 else "s")]
            if len(pieces) > 1:
                lines += ["", "Each separate piece is one request, all with the same description."]
            lines += ["", "Send it and pay?"]
            if not messagebox.askyesno(APP_NAME, "\n".join(lines)):
                self._status("Nothing was sent.")
                return
        if t.name == "Enlarge (AI)":
            kind = AI_METHODS.get(params.get('method'), ('realesrgan',))[0]
            if find_ai_program(kind) is None:
                if not messagebox.askyesno(APP_NAME, "%s is not set up yet. Choose the folder that holds it now?" % AI_PROGRAMS[kind]):
                    return
                if not self.choose_ai_dir() or find_ai_program(kind) is None:
                    return
            mp = cur.shape[0] * cur.shape[1] / 1e6
            k = int(str(params.get('scale', '2x'))[0])
            if mp * k * k > 12 and not messagebox.askyesno(
                    APP_NAME, "This will make a picture of about %.0f megapixels (%.1f MP x %d x %d). The programs work on the GPU and this can "
                    "take a very long time (on an integrated Intel GPU: about 2.5 minutes per megapixel of the original with Real-ESRGAN). "
                    "Esc stops it. Continue?" % (mp * k * k, mp, k, k)):
                return
        AI_CANCEL.clear()
        box = {}
        base = self.hist.current

        def work():
            try:
                box['out'] = apply_tool(t.name, cur, params, region)
            except Cancelled:
                box['cancelled'] = True
            except Exception as ex:
                box['error'] = ex

        th = threading.Thread(target=work, daemon=True)
        self._busy = (t.name, th, time.time())
        self.canvas.config(cursor='watch')
        self.b_apply.config(state='disabled')
        th.start()
        self._poll_external(box, base, flatten, params)

    def _poll_external(self, box, base, flatten, params):
        name, th, t0 = self._busy
        if th.is_alive():
            hint = " The model is being loaded: about a minute the first time." if name == "Remove object (AI)" and not _LAMA else ""
            self._status("%s is running... %d s.%s (Esc stops it; the window stays usable)" % (name, time.time() - t0, hint))
            self._busy_after = self.root.after(500, lambda: self._poll_external(box, base, flatten, params))
            return
        self._busy, self._busy_after = None, None
        self.b_apply.config(state='normal')
        self.canvas.config(cursor='crosshair')
        if 'cancelled' in box:
            self._status("%s stopped." % name)
        elif 'error' in box:
            self._status("%s failed: %s" % (name, box['error']))
            if isinstance(box['error'], ToolUnavailable):
                messagebox.showwarning(APP_NAME, str(box['error']))
        elif self.hist is None or self.hist.current is not base:
            self._status("%s finished, but the picture changed meanwhile: the result was dropped." % name)
        else:
            out = box['out']
            if flatten:
                self.active = None
            self._commit(name, out, () if flatten else None, self._recipe_step_for(params))
            note = TOOL_NOTES.pop(name, "")
            self._status("%s applied%s in %d s." % (name, " (%s)" % note if note else "", time.time() - t0), ok=True)
            self._update_or_label()

    def _commit(self, label, img, layers=None, step=None):
        """Put a new state on the history (`layers` None keeps the current ones). Size changes drop the selection and
        refit the view. `step` is the recipe step that makes this state, when a recipe can repeat it."""
        resized = img.shape[:2] != self.hist.current.shape[:2]
        self.hist.push(label, img, layers, step)
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

    def _saved_rgb(self, key, default):
        v = self.settings.get(key)
        try:
            return tuple(max(0, min(255, int(c))) for c in v)[:3] if isinstance(v, (list, tuple)) and len(v) == 3 else default
        except (TypeError, ValueError):
            return default

    def _draw_swatches(self):
        if getattr(self, 'b_fg', None) is not None:
            self.b_fg.config(bg="#%02x%02x%02x" % self.fg, activebackground="#%02x%02x%02x" % self.fg)
            self.b_bg.config(bg="#%02x%02x%02x" % self.bg, activebackground="#%02x%02x%02x" % self.bg)

    def set_colour(self, which, rgb):
        if which == 'fg':
            self.fg = tuple(int(v) for v in rgb)
        else:
            self.bg = tuple(int(v) for v in rgb)
        self._draw_swatches()

    def choose_colour(self, which):
        cur = self.fg if which == 'fg' else self.bg
        _, hexv = colorchooser.askcolor(color="#%02x%02x%02x" % cur, parent=self.root,
                                        title="Foreground colour" if which == 'fg' else "Background colour")
        if hexv:
            self.set_colour(which, parse_colors(hexv)[0])

    def swap_colours(self):
        self.fg, self.bg = self.bg, self.fg
        self._draw_swatches()

    def reset_colours(self):
        self.fg, self.bg = (0, 0, 0), (255, 255, 255)
        self._draw_swatches()

    def _dir_for(self, key='dir'):
        d = self.settings.get(key)
        return d if isinstance(d, str) and os.path.isdir(d) else None

    def _remember_dir(self, path, key='dir'):
        self.settings[key] = os.path.dirname(os.path.abspath(path))

    def _rebuild_recent(self):
        self.m_recent.delete(0, tk.END)
        files = recent_files(self.settings)
        for q in files:
            self.m_recent.add_command(label=q if len(q) < 70 else "..." + q[-67:], command=lambda q=q: self.open_path(q))
        if files:
            self.m_recent.add_separator()
            self.m_recent.add_command(label="Clear the list", command=self._clear_recent)
        else:
            self.m_recent.add_command(label="(none yet)", state=tk.DISABLED)

    def _clear_recent(self):
        self.settings['recent'] = []
        self._rebuild_recent()

    def open_dialog(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=OPEN_TYPES, initialdir=self._dir_for())
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
        add_recent(self.settings, path)
        self._remember_dir(path)
        self._rebuild_recent()
        self.scale = project_scale(path) if project else None
        self.measures, self._mpts, self._mdrag = [], [], False
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
        path = filedialog.asksaveasfilename(parent=self.root, filetypes=types, initialdir=self._dir_for(),
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
                save_project(path, self.hist.current, self.hist.layers, self.dpi, self.scale)
            else:
                save_image(self._flat(), path, self.dpi)
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot save %s:\n%s" % (path, ex))
            return
        add_recent(self.settings, path)
        self._remember_dir(path)
        self._rebuild_recent()
        if project or not self.hist.layers:
            self.save_path, self.dirty = path, False
            self._status("Saved %s." % path, ok=True)
        else:
            self._status("Exported the flattened picture to %s. The layers are NOT saved: use a .ifl project to keep them." % path)
        self._update_state()

    def on_close(self):
        if self._busy:
            AI_CANCEL.set()                                   # do not leave the AI program running behind a closed window
        if self._confirm_discard():
            if self._after is not None:
                self.root.after_cancel(self._after)
            try:
                self.settings['zoomed'] = self.root.state() == 'zoomed'
                if not self.settings['zoomed']:
                    self.settings['geometry'] = self.root.geometry()
            except tk.TclError:
                pass
            self.settings.update(fg=list(self.fg), bg=list(self.bg))
            save_settings(self.settings)
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
        """Esc: stop a running tool, else end a pick in progress, else drop the selection."""
        if self._busy:
            AI_CANCEL.set()
            self._status("Stopping %s..." % self._busy[0])
        elif self._grad is not None:
            self._grad = self.preview = self.lprev = None
            self._redraw()
            self._status("Gradient cancelled.")
        elif self._mpts:
            self._measure_cancel()
            self._status("Measurement cancelled.")
        elif self.mode == 'lasso' and self._lasso:
            self._lasso = []
            self.canvas.delete('lasso')
            self._status("Lasso cleared. Click to start again, Esc to leave the lasso.")
        elif self.mode:
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

    def _build_history_tab(self, tab, bg):
        tk.Label(tab, text="Every step of this picture. Click one to go back (or forward) to it; a new step then replaces the "
                 "steps after it. \u25cf = a recipe can repeat it on other pictures.", bg=bg, fg='#555555', font=('Arial', 8),
                 wraplength=290, justify='left', anchor='w').pack(fill=tk.X, padx=8, pady=(6, 2))
        row = tk.Frame(tab, bg=bg)
        row.pack(fill=tk.X, side=tk.BOTTOM, padx=6, pady=6)
        tk.Button(row, text="Save recipe...", command=self.save_recipe_dialog).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(row, text="Run on a folder...", command=self.batch_dialog).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        self.htree = ttk.Treeview(tab, columns=('rec',), show='tree headings', selectmode='browse')
        self.htree.heading('#0', text="Step")
        self.htree.heading('rec', text="Recipe")
        self.htree.column('#0', width=210)
        self.htree.column('rec', width=50, anchor='center')
        self.htree.tag_configure('future', foreground='#999999')
        self.htree.pack(fill=tk.BOTH, expand=True, padx=6)
        self.htree.bind('<<TreeviewSelect>>', lambda e: self._on_history_select())
        self._hkey = None

    def _refresh_history(self):
        h = self.hist
        key = None if h is None else (tuple(st[0] for st in h.states), h.index, tuple(st is not None for st in h.steps))
        if key == self._hkey:
            return
        self._hkey = key
        self._hsync = True
        try:
            self.htree.delete(*self.htree.get_children())
            if h is not None:
                for i, st in enumerate(h.states):
                    mark = "" if i == 0 else "\u25cf" if h.steps[i] is not None else "\u2013"
                    self.htree.insert('', tk.END, iid=str(i), text="%d  %s" % (i, st[0]), values=(mark,), tags=('future',) if i > h.index else ())
                self.htree.selection_set(str(h.index))
                self.htree.see(str(h.index))
        finally:
            self._hsync = False

    def _on_history_select(self):
        if getattr(self, '_hsync', False) or self.hist is None:
            return
        sel = self.htree.selection()
        if sel and int(sel[0]) != self.hist.index:
            self.jump_to(int(sel[0]))

    def jump_to(self, i):
        """Go to step `i` of the history (as many Undos or Redos at once)."""
        if self.hist is None or not 0 <= i < len(self.hist.states) or i == self.hist.index:
            return
        before = self.hist.current.shape[:2]
        label = self.hist.states[i][0]
        self.hist.index = i
        self.preview, self.lprev, self.dirty = None, None, True
        if self.hist.current.shape[:2] != before:
            self.sel = None
            self.fit_view()
        self._refresh_layers()
        self._update_state()
        self._redraw()
        self._status("Went to step %d: %s." % (i, label), ok=True)

    # ---- recipes and batch -----------------------------------------------------------------------------------------------
    def _current_recipe(self):
        steps, skipped = self.hist.recipe()
        return steps, skipped

    def save_recipe_dialog(self):
        if not self._need_image():
            return
        steps, skipped = self._current_recipe()
        if not steps:
            self._status("No step that a recipe can repeat yet: apply tools to the whole picture (or a rectangle) first.")
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=RECIPE_EXT, initialdir=self._dir_for('recipe_dir'),
                                            filetypes=[("ImageForge recipe", "*" + RECIPE_EXT)], title="Save the recipe")
        if not path:
            return
        try:
            save_recipe(path, steps, "from %s" % os.path.basename(self.path or "a picture"))
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot save the recipe:\n%s" % ex)
            return
        self._remember_dir(path, 'recipe_dir')
        extra = ""
        if skipped:
            extra += " Not in it (cannot be repeated on another picture): %s." % ", ".join(sorted(set(skipped)))
        if self.hist.lost:
            extra += " The oldest steps were dropped by the memory limit: the recipe starts later than the picture did."
        self._status("Recipe of %d step(s) saved to %s.%s" % (len(steps), path, extra), ok=True)

    def run_recipe_here(self):
        if not self._need_image():
            return
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("ImageForge recipe", "*" + RECIPE_EXT), ("All files", "*.*")],
                                          initialdir=self._dir_for('recipe_dir'), title="Run a recipe on this picture")
        if not path:
            return
        try:
            steps = load_recipe(path)
        except ValueError as ex:
            messagebox.showerror(APP_NAME, "Cannot use %s:\n%s" % (path, ex))
            return
        if self.hist.layers and not messagebox.askyesno(APP_NAME, "The layers are merged into the background first. Continue?"):
            return
        try:
            out = run_recipe(self._flat(), steps)
        except Exception as ex:
            self._status("The recipe failed: %s" % ex)
            return
        self.active = None
        self._commit("Recipe: " + os.path.splitext(os.path.basename(path))[0], out, ())
        self._status("Ran %d step(s) of %s." % (len(steps), os.path.basename(path)), ok=True)

    def batch_dialog(self):
        """Recipe > Run a recipe on a folder: every picture of the folder goes through the recipe and is saved in another folder."""
        steps_now = self.hist.recipe()[0] if self.hist is not None else []
        w = tk.Toplevel(self.root)
        w.title("Run a recipe on a folder - " + APP_NAME)
        w.transient(self.root)
        v = dict(use_now=tk.BooleanVar(value=bool(steps_now)), recipe=tk.StringVar(), src=tk.StringVar(), out=tk.StringVar(),
                 sub=tk.BooleanVar(value=False), fmt=tk.StringVar(value='same'), q=tk.IntVar(value=95), suffix=tk.StringVar(value="_edited"),
                 over=tk.BooleanVar(value=False))
        pad = dict(padx=6, pady=3)
        r = 0
        tk.Radiobutton(w, text="the %d step(s) of this picture so far" % len(steps_now), variable=v['use_now'], value=True,
                       state=tk.NORMAL if steps_now else tk.DISABLED).grid(row=r, column=0, columnspan=3, sticky='w', **pad)
        r += 1
        tk.Radiobutton(w, text="a recipe file:", variable=v['use_now'], value=False).grid(row=r, column=0, sticky='w', **pad)
        tk.Entry(w, textvariable=v['recipe'], width=44).grid(row=r, column=1, **pad)
        tk.Button(w, text="Browse...", command=lambda: self._browse_file(v['recipe'], v['use_now'])).grid(row=r, column=2, **pad)
        r += 1
        tk.Label(w, text="Pictures in:").grid(row=r, column=0, sticky='w', **pad)
        tk.Entry(w, textvariable=v['src'], width=44).grid(row=r, column=1, **pad)
        tk.Button(w, text="Browse...", command=lambda: self._browse_dir(v['src'], v['out'])).grid(row=r, column=2, **pad)
        r += 1
        tk.Checkbutton(w, text="also the sub-folders", variable=v['sub']).grid(row=r, column=1, sticky='w', **pad)
        r += 1
        tk.Label(w, text="Save in:").grid(row=r, column=0, sticky='w', **pad)
        tk.Entry(w, textvariable=v['out'], width=44).grid(row=r, column=1, **pad)
        tk.Button(w, text="Browse...", command=lambda: self._browse_dir(v['out'])).grid(row=r, column=2, **pad)
        r += 1
        row = tk.Frame(w)
        row.grid(row=r, column=0, columnspan=3, sticky='w', **pad)
        tk.Label(row, text="Format").pack(side=tk.LEFT)
        ttk.Combobox(row, textvariable=v['fmt'], values=('same', 'png', 'jpg', 'tif'), width=6, state='readonly').pack(side=tk.LEFT, padx=4)
        tk.Label(row, text="JPEG quality").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Spinbox(row, from_=10, to=100, textvariable=v['q'], width=4).pack(side=tk.LEFT, padx=4)
        tk.Label(row, text="Add to the name").pack(side=tk.LEFT, padx=(8, 0))
        tk.Entry(row, textvariable=v['suffix'], width=10).pack(side=tk.LEFT, padx=4)
        r += 1
        tk.Checkbutton(w, text="replace files that already exist in the output folder (otherwise a free name is used)",
                       variable=v['over']).grid(row=r, column=0, columnspan=3, sticky='w', **pad)
        r += 1
        self.b_batch = tk.Button(w, text="Run", bg='#2ecc71', fg='white', font=('Arial', 10, 'bold'), width=10)
        self.b_batch.grid(row=r, column=0, **pad)
        self.l_batch = tk.Label(w, text="", anchor='w')
        self.l_batch.grid(row=r, column=1, columnspan=2, sticky='we', **pad)
        r += 1
        log = tk.Text(w, height=10, width=78, state=tk.DISABLED, font=('Consolas', 8))
        log.grid(row=r, column=0, columnspan=3, padx=6, pady=(0, 6))
        state = dict(stop=False, running=False)
        w.protocol('WM_DELETE_WINDOW', lambda: (state.update(stop=True), w.destroy()))
        self.b_batch.config(command=lambda: self._batch_run(w, v, steps_now, log, state))

    def _browse_file(self, var, flag):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("ImageForge recipe", "*" + RECIPE_EXT), ("All files", "*.*")],
                                          initialdir=self._dir_for('recipe_dir'))
        if path:
            var.set(path)
            flag.set(False)

    def _browse_dir(self, var, follow=None):
        d = filedialog.askdirectory(parent=self.root, initialdir=self._dir_for('batch_dir'))
        if d:
            var.set(d)
            self.settings['batch_dir'] = d
            if follow is not None and not follow.get():
                follow.set(os.path.join(d, "processed"))

    def _batch_run(self, w, v, steps_now, log, state):
        def say(text):
            log.config(state=tk.NORMAL)
            log.insert(tk.END, text + "\n")
            log.see(tk.END)
            log.config(state=tk.DISABLED)
        if state['running']:
            return
        try:
            steps = steps_now if v['use_now'].get() else load_recipe(v['recipe'].get())
        except ValueError as ex:
            messagebox.showerror(APP_NAME, "The recipe cannot be used:\n%s" % ex, parent=w)
            return
        src, out = v['src'].get().strip(), v['out'].get().strip()
        if not steps or not os.path.isdir(src) or not out:
            messagebox.showerror(APP_NAME, "Choose a recipe (or do some steps first), a folder of pictures and a folder to save in.", parent=w)
            return
        out_abs = os.path.abspath(out)
        files = [q for q in list_images(src, v['sub'].get()) if not os.path.abspath(q).startswith(out_abs + os.sep)]
        if not files:
            messagebox.showinfo(APP_NAME, "There are no pictures in that folder.", parent=w)
            return
        state.update(running=True, stop=False)
        self.b_batch.config(state=tk.DISABLED)
        counts = dict(ok=0, bad=0)

        def one(i):
            if state['stop'] or not w.winfo_exists():
                return
            if i >= len(files):
                state['running'] = False
                self.b_batch.config(state=tk.NORMAL)
                self.l_batch.config(text="Done: %d saved, %d failed." % (counts['ok'], counts['bad']))
                say("Done: %d saved, %d failed." % (counts['ok'], counts['bad']))
                return
            self.l_batch.config(text="%d / %d  %s" % (i + 1, len(files), os.path.basename(files[i])))
            w.update_idletasks()
            res = run_batch(steps, [files[i]], out, v['fmt'].get(), v['suffix'].get(), v['over'].get(), int(v['q'].get()),
                            src if v['sub'].get() else None)[0]
            counts['ok' if res[1] else 'bad'] += 1
            say(("ok   %s  ->  %s" % (res[0], res[1])) if res[1] else ("FAIL %s  (%s)" % (res[0], res[2])))
            w.after(5, lambda: one(i + 1))

        say("%d picture(s), %d step(s)." % (len(files), len(steps)))
        one(0)

    # ---- clipboard and export ---------------------------------------------------------------------------------------------------
    def paste_clipboard(self):
        """Ctrl+V: a picture on the clipboard becomes a new layer (or, with nothing open, the picture itself); copied files are opened."""
        try:
            from PIL import ImageGrab
            data = ImageGrab.grabclipboard()
        except Exception as ex:
            self._status("The clipboard cannot be read here: %s" % ex)
            return
        if data is None:
            self._status("There is no picture on the clipboard.")
            return
        if isinstance(data, list):
            files = [q for q in data if isinstance(q, str) and q.lower().endswith(IMAGE_EXTS + (PROJECT_EXT,))]
            if files:
                self.open_path(files[0])
            else:
                self._status("The clipboard holds files that are not pictures.")
            return
        rgba = np.array(data.convert('RGBA'))
        if self.hist is None:
            h, w = rgba.shape[:2]
            bg = np.full((h, w, 3), 255, np.uint8)
            blend_rgba(bg, rgba, 0, 0)
            self.hist, self.path, self.dpi, self.save_path = History(bg), None, None, None
            self.sel, self.preview, self.dirty, self.active, self.lprev, self._comp = None, None, True, None, None, None
            self._refresh_layers()
            self.root.update_idletasks()
            self.fit_view()
            self._update_state()
            self._redraw()
            self._status("Opened the picture from the clipboard.", ok=True)
            return
        h, w = self.hist.current.shape[:2]
        L = Layer('image', "Pasted", (w - rgba.shape[1]) // 2, (h - rgba.shape[0]) // 2, rgba=rgba)
        self._commit_layers("Paste", list(self.hist.layers) + [L], len(self.hist.layers))
        self.tree.selection_set("Move layer")
        self._status("Pasted as a new layer (%d x %d): drag it into place." % (rgba.shape[1], rgba.shape[0]), ok=True)

    def export_dialog(self):
        """File > Export: the picture as seen (layers merged) in a chosen format with its quality, size and dpi; the project is untouched."""
        if not self._need_image():
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="Export the picture", initialdir=self._dir_for('export_dir'),
                                            filetypes=SAVE_TYPES, defaultextension=".png",
                                            initialfile=os.path.splitext(os.path.basename(self.path or "image"))[0])
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        img = self._flat()
        h, w = img.shape[:2]
        w_ = tk.Toplevel(self.root)
        w_.title("Export options")
        w_.transient(self.root)
        w_.grab_set()
        q, lvl = tk.IntVar(value=self.settings.get('jpeg_quality', 92)), tk.IntVar(value=self.settings.get('png_level', 6))
        longest = tk.IntVar(value=0)
        dpi = tk.IntVar(value=int(self.dpi[0]) if self.dpi else 0)
        pad = dict(padx=8, pady=3)
        tk.Label(w_, text="%s   (%d x %d px)" % (os.path.basename(path), w, h), font=('Arial', 9, 'bold')).grid(row=0, column=0, columnspan=2, **pad)
        tk.Label(w_, text="JPEG / WebP quality (higher = bigger file)").grid(row=1, column=0, sticky='w', **pad)
        ttk.Spinbox(w_, from_=10, to=100, textvariable=q, width=5, state=tk.NORMAL if ext in ('.jpg', '.jpeg', '.webp') else tk.DISABLED).grid(row=1, column=1, **pad)
        tk.Label(w_, text="PNG compression 0-9 (same picture, smaller file, slower)").grid(row=2, column=0, sticky='w', **pad)
        ttk.Spinbox(w_, from_=0, to=9, textvariable=lvl, width=5, state=tk.NORMAL if ext == '.png' else tk.DISABLED).grid(row=2, column=1, **pad)
        tk.Label(w_, text="Longest side in pixels (0 = as it is)").grid(row=3, column=0, sticky='w', **pad)
        ttk.Spinbox(w_, from_=0, to=100000, textvariable=longest, width=7).grid(row=3, column=1, **pad)
        tk.Label(w_, text="Resolution in dpi written in the file (0 = none)").grid(row=4, column=0, sticky='w', **pad)
        ttk.Spinbox(w_, from_=0, to=5000, textvariable=dpi, width=7).grid(row=4, column=1, **pad)
        done = []

        def go():
            done.append(True)
            w_.destroy()
        tk.Button(w_, text="Export", command=go, bg='#2ecc71', fg='white', width=10).grid(row=5, column=0, columnspan=2, pady=8)
        self.root.wait_window(w_)
        if not done:
            return
        try:
            if longest.get() and max(h, w) != longest.get():
                k = longest.get() / float(max(h, w))
                img = cv2.resize(img, (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)
            d = (float(dpi.get()), float(dpi.get())) if dpi.get() else None
            save_image(img, path, d, int(q.get()), int(lvl.get()))
        except Exception as ex:
            messagebox.showerror(APP_NAME, "Cannot export %s:\n%s" % (path, ex))
            return
        self.settings.update(jpeg_quality=int(q.get()), png_level=int(lvl.get()))
        self._remember_dir(path, 'export_dir')
        self._status("Exported %s (%d x %d px). The project and its layers are unchanged." % (path, img.shape[1], img.shape[0]), ok=True)

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

    _TEXT_LAYER_MSG = ("A text, shape or scale-bar layer is not made of pixels: select the Background or an image layer, or "
                       "merge it down first. (It is edited with its own tool: Add text, Shapes, Scale bar.)")

    def _tool_target(self):
        """What an ordinary tool works on: ('base', None), ('layer', image layer) or ('text', text layer)."""
        L = self._active_layer()
        if L is None:
            return 'base', None
        return ('text' if L.kind in LAYER_KINDS else 'layer'), L          # 'text' = any layer drawn from parameters

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
                                  text=("\u25cf " if L.visible else "\u25cb ") + L.name + ("  [%s]" % LAYER_KINDS[L.kind]['tag'] if L.kind in LAYER_KINDS else ""))
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
        name = LAYER_KINDS[L.kind]['tool'] if L.kind in LAYER_KINDS else "Move layer"
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

    def _commit_layers(self, label, layers, active=-1, base=None, step=None):
        """A history step that changes the layers (and, for a merge, the background)."""
        if active != -1:
            self.active = active
        self._commit(label, self.hist.current if base is None else base, tuple(layers), step)

    def _recipe_step_for(self, params):
        """The recipe step for the tool just applied to the Background, or None when a recipe could not repeat it on another
        picture (a shape selection belongs to this picture only)."""
        if self.tool is None or self.tool.interactive or self.tool.layer or self.tool.takes_region:
            return None
        if self.sel is not None and region_mask(self.sel, self.hist.current.shape) is not None:
            return None
        return tool_step(self.tool.name, params, None if self.tool.geometry else self.sel)

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
        m = region_mask(self.sel, base.shape)
        alpha = np.full((y1 - y0, x1 - x0), 255, np.uint8) if m is None else \
            np.clip(np.rint(m[y0:y1, x0:x1] * 255), 0, 255).astype(np.uint8)          # a shape: its mask is the layer's alpha
        rgba = np.dstack([base[y0:y1, x0:x1], alpha])
        L = Layer('image', "Selection", x0, y0, rgba=rgba)
        new_base = base
        if cut:
            new_base = base.copy()
            new_base[y0:y1, x0:x1] = self._fill_color(base)
            if m is not None:
                new_base = masked_merge(base, new_base, self.sel)
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
        """Put the settings of a text / shape / scale-bar layer into the boxes of its tool."""
        self._params_ready = False
        for key, val in LAYER_KINDS[L.kind]['boxes'](L, self.scale).items():
            if key in self.pvars:
                self.pvars[key].set(val)
        self._params_ready = True
        self._params_changed()

    def _layer_from_params(self, params, old=None):
        return LAYER_KINDS[self.tool.layer]['build'](params, old, self.scale)

    def _apply_layer_tool(self, params):
        ei = self._edit_index()
        old = self.hist.layers[ei] if ei is not None else None
        new = self._layer_from_params(params, old)
        kind = LAYER_KINDS[self.tool.layer]
        if new is None:
            self._status(kind['empty'])
            return
        layers = list(self.hist.layers)
        if ei is not None:
            layers[ei] = new
            self._commit_layers("Edit %s layer" % kind['tag'], layers)
            self._status("The %s layer is updated. Select 'Background' in the Layers tab to add another one." % kind['tag'], ok=True)
        else:
            layers.append(new)
            self._commit_layers("Add %s layer" % kind['tag'], layers, len(layers) - 1, step=layer_step(self.tool.name, params, self.scale))
            self._status("Added as a %s layer: change the boxes and press Update layer to edit it." % kind['tag'], ok=True)

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

    def _set_autofit(self, on):
        self.autofit = on
        if getattr(self, 'b_fit', None) is not None:
            self.b_fit.config(relief=tk.SUNKEN if on else tk.RAISED)

    def _on_canvas_resize(self):
        """The window or the panel changed size: while the view is on 'Fit' the picture is fitted again, else it is just redrawn."""
        if self.autofit and self.hist is not None:
            self.fit_view()
        else:
            self._redraw()

    def fit_view(self):
        """Fill the window with the picture, and keep doing so when the window is resized, until the view is moved or zoomed by hand."""
        if self.hist is None:
            return
        self._set_autofit(True)
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
        self._set_autofit(False)
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
        self._draw_faces_marker()
        self._draw_clone_marker()
        self._set_info()
        self._draw_measures()
        self._draw_shape_handles()
        self._hist_schedule()

    def _draw_selection(self, rect=None):
        c = self.canvas
        c.delete('sel')
        rect = rect or self.sel
        if rect is None or self.hist is None:
            return
        x0, y0, x1, y1 = rect
        z = self.zoom
        if rect is self.sel and region_mask(rect, self.hist.current.shape) is not None:      # a shape: draw its outline
            m = rect.mask
            if self._outline[0] is not m:
                lines = sorted(mask_outline(m), key=len, reverse=True)[:300]
                self._outline = (m, lines)
            for line in self._outline[1]:
                flat = [v for px, py in line for v in (px * z + self.ox, py * z + self.oy)]
                if len(flat) >= 4:
                    c.create_line(*flat, *flat[:2], fill='white', tags='sel')
                    c.create_line(*flat, *flat[:2], fill='black', dash=(4, 4), tags='sel')
            return
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
                      'point': "Click the point of the picture to copy FROM. Esc cancels.",
                      'angle': "Click two points of a line that should be level (or vertical). Esc cancels.",
                      'quad': "Click the four corners of the area to straighten, in any order. Esc cancels.",
                      'faces': "Click on each face to add it to the list (as many as needed), Esc to finish."}[what])

    def find_faces_into(self, key):
        """The 'Find faces' button: write the faces the finder sees in the list, so that they can be corrected."""
        if not self._need_image():
            return
        found = find_faces(self._flat())
        self.pvars[key].set(format_faces(found))
        self._status(("%d face%s found and written in the list: add the missing ones with 'Mark faces', or edit the numbers."
                      % (len(found), "" if len(found) == 1 else "s")) if found else
                     "No face found: press 'Mark faces' and click on each one.", ok=bool(found))

    def _draw_faces_marker(self):
        """The faces of the Portrait retouch tool as ellipses on the picture (the list, or what the finder sees while the list is empty,
        dashed); removed when another tool is selected."""
        c = self.canvas
        c.delete('faces')
        if self.tool is None or self.hist is None or 'faces' not in self.pvars or self.tool.name != "Portrait retouch":
            return
        try:
            img = self._flat()
            p = self._read_params() or {}
            listed = parse_faces(p.get('faces', ""), p.get('size', 0), img.shape)
            faces, dash = (listed, None) if listed else (find_faces(img), (4, 3))
        except Exception:
            return
        for i, (fx, fy, fw) in enumerate(faces):
            sx, sy, rx, ry = fx * self.zoom + self.ox, (fy + 0.10 * fw) * self.zoom + self.oy, 0.62 * fw * self.zoom, 0.80 * fw * self.zoom
            c.create_oval(sx - rx, sy - ry, sx + rx, sy + ry, outline='#ffd400', width=2, dash=dash, tags='faces')
            if listed:
                c.create_text(sx, sy - ry - 8, text=str(i + 1), fill='#ffd400', font=('Arial', 10, 'bold'), tags='faces')

    def choose_color(self, key):
        var = self.pvars[key]
        first = parse_colors(var.get())
        start = "#%02x%02x%02x" % first[0] if first else "#000000"
        _, hexv = colorchooser.askcolor(color=start, parent=self.root)
        if hexv:
            var.set(hexv)

    def start_area(self):
        """'Define working area': the next drag on the picture draws the rectangle every tool is limited to, whatever tool is
        selected; afterwards everything is as before. Esc cancels."""
        if not self._need_image():
            return
        self.cancel_mode()
        self.mode = 'area'
        self._sync_toolbox()
        self.canvas.config(cursor='tcross')
        self._status("Drag a rectangle on the picture: it becomes the working area, and every tool acts only inside it. "
                     "Esc cancels.")

    def _rect_sel(self):
        """The selection when it is a plain rectangle (handles, nudging and content moves only exist for those), else None."""
        if self.sel is None or self.hist is None or region_mask(self.sel, self.hist.current.shape) is not None:
            return None
        return self.sel

    @staticmethod
    def _sel_mode(e):
        """How a click combines with the selection: Shift adds, Ctrl removes, both keep only the overlap."""
        shift, ctrl = bool(e.state & 0x1), bool(e.state & 0x4)
        return 'intersect' if shift and ctrl else 'add' if shift else 'subtract' if ctrl else 'replace'

    def _set_shape(self, sel, what):
        self.sel = sel
        self._after_selection()
        if sel is None:
            self._status("%s: nothing is selected now." % what)
        else:
            self._status("%s: %d x %d px box, %d%% of it selected. Every tool acts only on the shape." % (
                what, sel[2] - sel[0], sel[3] - sel[1], round(100.0 * float((sel.mask > 0.5).sum()) / max(1, (sel[2] - sel[0]) * (sel[3] - sel[1])))),
                ok=True)

    def start_wand(self):
        """'Magic wand': each click on the picture selects the area of that colour (tolerance and Contiguous in the bar);
        Shift adds, Ctrl removes, Shift+Ctrl keeps the overlap; Esc ends."""
        if not self._need_image():
            return
        self.cancel_mode()
        self.mode = 'wand'
        self._sync_toolbox()
        self.canvas.config(cursor='tcross')
        self._status("Magic wand: click a colour on the picture (Shift adds to the selection, Ctrl removes, Esc ends).")

    def _wand_click(self, e):
        x, y = self._img_xy(e.x, e.y)
        img = self._flat()                                      # as seen, layers included
        try:
            tol = int(self.v_wand_tol.get())
        except (tk.TclError, ValueError):
            tol = 32
        mask = wand_mask(img, int(math.floor(x)), int(math.floor(y)), tol, self.v_wand_contig.get())
        if not mask.any():
            self._status("Magic wand: click inside the picture.")
            return
        self._set_shape(combine_selection(self.sel, mask, self._sel_mode(e), img.shape[:2]), "Magic wand")

    def start_lasso(self):
        """'Lasso': click the corners of a polygon, or drag to draw freehand; double-click or Enter closes it, Esc clears it.
        Shift adds to the selection, Ctrl removes."""
        if not self._need_image():
            return
        self.cancel_mode()
        self.mode = 'lasso'
        self._sync_toolbox()
        self.canvas.config(cursor='tcross')
        self.canvas.focus_set()
        self._status("Lasso: click the corners (or drag freehand); double-click or Enter closes it, Esc clears. "
                     "Shift adds, Ctrl removes.")

    def _lasso_press(self, e):
        x, y = self._img_xy(e.x, e.y)
        now = time.time()
        quick = self._lasso and now - self._lasso_t < 0.45 and math.hypot(x - self._lasso[-1][0], y - self._lasso[-1][1]) * self.zoom < 6
        self._lasso_t = now
        if not self._lasso:
            self._lasso_mod = self._sel_mode(e)
        elif len(self._lasso) >= 3 and (quick or math.hypot(x - self._lasso[0][0], y - self._lasso[0][1]) * self.zoom < 6):
            self._lasso_close()                       # a double click, or a click on the first point, closes it
            return
        self._lasso.append((x, y))
        self._lasso_down = True
        self._draw_lasso(e)

    def _lasso_add(self, e, freehand=False):
        x, y = self._img_xy(e.x, e.y)
        lx, ly = self._lasso[-1]
        if math.hypot(x - lx, y - ly) * self.zoom >= 3:
            self._lasso.append((x, y))
        self._draw_lasso(e)

    def _draw_lasso(self, e):
        c = self.canvas
        c.delete('lasso')
        if not self._lasso:
            return
        pts = [v for x, y in self._lasso for v in (x * self.zoom + self.ox, y * self.zoom + self.oy)]
        if len(pts) >= 4:
            c.create_line(*pts, fill='yellow', width=2, tags='lasso')
        c.create_line(pts[-2], pts[-1], e.x, e.y, fill='yellow', dash=(3, 3), tags='lasso')
        c.create_oval(pts[0] - 4, pts[1] - 4, pts[0] + 4, pts[1] + 4, outline='yellow', width=2, tags='lasso')

    def _lasso_close(self):
        if self.mode != 'lasso' or self.hist is None:
            return
        pts, mod = self._lasso, self._lasso_mod
        if len(pts) > 2 and pts[-1] == pts[-2]:
            pts = pts[:-1]
        self._lasso, self._lasso_down = [], False
        self.canvas.delete('lasso')
        if len(pts) < 3:
            self._status("A lasso needs at least three points.")
            return
        shape = self.hist.current.shape[:2]
        mask = polygon_mask(shape, pts)
        if not mask.any():
            self._status("The lasso is outside the picture.")
            return
        self._set_shape(combine_selection(self.sel, mask, mod, shape), "Lasso")

    def invert_sel(self):
        if not self._need_image():
            return
        if self.sel is None:
            self._status("Nothing is selected: select something first, then Invert (Ctrl+I) selects everything else.")
            return
        self._set_shape(invert_selection(self.sel, self.hist.current.shape[:2]), "Invert")

    def grow_sel(self, sign):
        if not self._need_image():
            return
        if self.sel is None:
            self._status("Nothing is selected.")
            return
        try:
            px = max(1, int(self.v_selpx.get()))
        except (tk.TclError, ValueError):
            px = 3
        self._set_shape(grow_selection(self.sel, sign * px, self.hist.current.shape[:2]), "Grow" if sign > 0 else "Shrink")

    def feather_sel(self):
        if not self._need_image():
            return
        if self.sel is None:
            self._status("Nothing is selected.")
            return
        try:
            px = max(1, int(self.v_selpx.get()))
        except (tk.TclError, ValueError):
            px = 3
        self._set_shape(feather_selection(self.sel, px, self.hist.current.shape[:2]), "Feather")

    def clear_area(self):
        """Back to the whole image."""
        if self.mode == 'area':
            self.cancel_mode()
        if self.sel is not None:
            self.sel = None
            self._after_selection()
            self._status("Working area cleared: the tools act on the whole image.", ok=True)

    def _sync_area_box(self):
        """Show the working area in the X / Y / W / H boxes and the label (and the clear / crop buttons' state)."""
        self._area_sync = True
        try:
            if self.sel is None:
                for v in self.area_vars.values():
                    v.set("")
                self.l_area.config(text="Whole image: nothing limits the tools", fg='#555555')
            else:
                x0, y0, x1, y1 = self.sel
                for k, val in zip('XYWH', (x0, y0, x1 - x0, y1 - y0)):
                    self.area_vars[k].set(str(val))
                shape = " (a shape)" if region_mask(self.sel, self.hist.current.shape) is not None else ""
                self.l_area.config(text="%d x %d px at (%d, %d)%s" % (x1 - x0, y1 - y0, x0, y0, shape), fg='#8a5a00')
        finally:
            self._area_sync = False

    def _apply_area_box(self):
        """The numbers typed in X / Y / W / H become the working area (clipped to the picture)."""
        if self._area_sync or self.hist is None:
            return
        try:
            x, y, w, h = (int(round(float(self.area_vars[k].get()))) for k in 'XYWH')
        except ValueError:
            return
        H, W = self.hist.current.shape[:2]
        rect = (max(0, x), max(0, y), min(W, x + w), min(H, y + h))
        if rect[2] <= rect[0] or rect[3] <= rect[1] or rect == self.sel:
            self._sync_area_box()
            return
        self.sel = rect
        self._after_selection()

    def cancel_mode(self):
        self.mode, self._pick_pts = None, []
        if self._area_start is not None:
            self._area_start = self._area_cur = None
            self._draw_selection()
        self.canvas.delete('pick')
        self.canvas.delete('lasso')
        self._sync_toolbox()
        self._lasso, self._lasso_down = [], False
        self.canvas.config(cursor='crosshair')

    @staticmethod
    def _quick_pick_hint(tool):
        """The line added to a tool's description when Ctrl+click can pick for it."""
        kinds = [q.pick for q in tool.params if q.pick in ('point', 'color', 'color1', 'faces')]
        if 'faces' in kinds:
            return "\n\nCtrl+click on the picture adds a face to the list."
        if 'point' in kinds:
            return "\n\nCtrl+click on the picture sets the source."
        if 'color' in kinds and 'color1' in kinds:
            return "\n\nCtrl+click on the picture adds a colour to find; Ctrl+Shift+click picks the colour to paint with."
        if kinds:
            return "\n\nCtrl+click on the picture picks the colour."
        return ""

    def _quick_pick(self, e):
        """Ctrl+click with a tool that has a pick button does what the button does, without pressing it: the clone source, a
        colour to find ('color': added to the list), or the colour to paint with ('color1'; Shift chooses it when the tool has
        both). Returns True when the click was used."""
        if self.tool is None or not (e.state & 0x4):
            return False
        if self.tool.interactive in ('paint', 'fill', 'gradient') and self.tool.name != "Dodge / burn":     # the drawing tools use the two colours above the picture
            x, y = self._img_xy(e.x, e.y)
            img = self._flat()
            ix, iy = int(math.floor(x)), int(math.floor(y))
            if 0 <= ix < img.shape[1] and 0 <= iy < img.shape[0]:
                patch = img[max(0, iy - 1):iy + 2, max(0, ix - 1):ix + 2].reshape(-1, 3)
                rgb = tuple(int(v) for v in np.median(patch, axis=0))
                which = 'bg' if (e.state & 0x1) else 'fg'
                self.set_colour(which, rgb)
                self._status("%s colour #%02x%02x%02x picked." % ("Background" if which == 'bg' else "Foreground", *rgb), ok=True)
            return True
        params = [q for q in self.tool.params if q.pick in ('point', 'color', 'color1', 'faces')]
        if not params:
            return False
        shift = bool(e.state & 0x1)
        point = [q for q in params if q.pick == 'point']
        find = [q for q in params if q.pick == 'color']
        paint = [q for q in params if q.pick == 'color1']
        if [q for q in params if q.pick == 'faces']:
            chosen = [q for q in params if q.pick == 'faces'][0]
        elif point:
            chosen = point[0]
        elif find and paint:
            chosen = paint[0] if shift else find[0]
        else:
            chosen = (find or paint)[0]
        self.mode, self._pick_key = chosen.pick, chosen.key
        try:
            self._pick_click(e, quick=True)
        finally:
            self.cancel_mode()
        if point:
            self._draw_brush_circle(e)
        return True

    def _pick_click(self, e, quick=False):
        x, y = self._img_xy(e.x, e.y)
        img = self._flat()                       # colours are picked from the picture as seen, layers included
        h, w = img.shape[:2]
        ix, iy = int(math.floor(x)), int(math.floor(y))
        if not (0 <= ix < w and 0 <= iy < h):
            return
        if self.mode == 'point':                                 # the clone source: x here, y in the paired parameter
            self.pvars[self._pick_key].set(ix)
            self.pvars[self._pick_key.replace('_x', '_y')].set(iy)
            self.cancel_mode()
            self._clone_off = None
            self._draw_clone_marker()
            self._status("Clone source set at (%d, %d). Now paint where the copy should go." % (ix, iy), ok=True)
            return
        if self.mode in ('color', 'color1'):
            patch = img[max(0, iy - 1):iy + 2, max(0, ix - 1):ix + 2].reshape(-1, 3)
            r, g, b = (int(v) for v in np.median(patch, axis=0))             # median of 3x3: robust to a stray pixel
            var = self.pvars[self._pick_key]
            if self.mode == 'color1':
                var.set("#%02x%02x%02x" % (r, g, b))
                fill = self.pvars.get('fill')
                if fill is not None and 'picked colour' in next((q.choices for q in self.tool.params if q.key == 'fill'), ()):
                    fill.set('picked colour')                      # the colour was picked to be used: use it
                self.cancel_mode()
                self._status("Colour #%02x%02x%02x set." % (r, g, b), ok=True)
            else:
                var.set((var.get().strip() + " #%02x%02x%02x" % (r, g, b)).strip())
                self._status("Added #%02x%02x%02x. %s" % (r, g, b, "Ctrl+click adds more." if quick else
                                                           "Click more colours, or Esc to finish."), ok=True)
        elif self.mode == 'faces':
            var = self.pvars[self._pick_key]
            size = int((self._read_params() or {}).get('size', 0)) or int(0.22 * min(h, w))
            var.set((var.get().strip().rstrip(";") + "; " if var.get().strip() else "") + "%d %d %d" % (ix, iy, size))
            self._status("Face added at (%d, %d), width %d: edit the list to change its size. Click more faces, or Esc to finish."
                         % (ix, iy, size), ok=True)
        elif self.mode == 'quad':
            self._pick_pts.append((x, y))
            self._draw_pick_points()
            if len(self._pick_pts) == 4:
                q = order_corners(self._pick_pts)
                self._params_ready = False
                for (kx, ky), (qx, qy) in zip((('tl_x', 'tl_y'), ('tr_x', 'tr_y'), ('br_x', 'br_y'), ('bl_x', 'bl_y')), q):
                    self.pvars[kx].set(int(round(qx)))
                    self.pvars[ky].set(int(round(qy)))
                self._params_ready = True
                self.cancel_mode()
                self._params_changed()
                self._status("Corners set; check the preview, adjust the boxes if needed, and press Apply.", ok=True)
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

    def _draw_pick_points(self):
        c = self.canvas
        c.delete('pick')
        pts = [(x * self.zoom + self.ox, y * self.zoom + self.oy) for x, y in self._pick_pts]
        for i, (sx, sy) in enumerate(pts):
            c.create_oval(sx - 4, sy - 4, sx + 4, sy + 4, outline='yellow', width=2, tags='pick')
            c.create_text(sx + 8, sy - 8, text=str(i + 1), fill='yellow', tags='pick', font=('Arial', 9, 'bold'))
        if len(pts) > 1:
            c.create_line(*[v for p in pts for v in p], fill='yellow', dash=(3, 3), tags='pick')

    def _is_brush(self):
        """Eraser brush, Clone stamp and Clean brush: tools that paint with strokes."""
        return self.tool is not None and self.tool.interactive in ('brush', 'clone', 'clean', 'paint')

    def _brush_params(self):
        p = self._read_params() or {}
        return int(p.get('size', 15)), p.get('fill', 'white')

    def _brush_scope(self, L):
        """What limits the brush right now, in words ('' when nothing does: it paints the whole Background)."""
        parts = []
        if L is not None:
            parts.append("painting on layer '%s'" % L.name)
        if self.sel is not None:
            parts.append("only inside the working area")
        return ("%s: " % self.tool.name + ", ".join(parts)) if parts else ""

    def _brush_start(self, e):
        mode = self.tool.interactive
        x, y = self._img_xy(e.x, e.y)
        pt = (int(math.floor(x)), int(math.floor(y)))
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        if mode == 'clone':
            src = self._clone_source()
            if src is None:
                self._status("Define the source first: press 'Set source' and click on the picture (or Ctrl+click).")
                return
            if self._clone_off is None or src != self._clone_last or not (self._read_params() or {}).get('aligned', True):
                self._clone_off = (src[0] - pt[0], src[1] - pt[1])
                self._clone_last = src
        self._stroke_layer = L if kind == 'layer' else None
        self._stroke_orig = L.rgba if L is not None else self.hist.current
        self._stroke = self._stroke_orig.copy()
        self._stroke_mask = np.zeros(self._stroke.shape[:2], np.uint8) if mode == 'clean' else None
        self._cover = np.zeros(self._stroke.shape[:2], np.float32) if mode == 'paint' else None
        self._last = None
        where = self._brush_scope(L)
        if where:                                      # the two silent traps: a forgotten selection, a selected layer
            self._status("%s (press 'Whole image' to remove the area; click Background in the Layers tab to paint the picture)." % where)
        if mode == 'paint' and (e.state & 0x1) and self._paint_prev is not None:            # Shift+click: a straight line from the last end
            self._last = self._paint_prev
        self._brush_to(e)

    def _brush_to(self, e):
        if self._stroke is None:
            return
        mode = self.tool.interactive
        p = self._read_params() or {}
        size, fill = self._brush_params()
        L = self._stroke_layer
        x, y = self._img_xy(e.x, e.y)
        pt = (int(math.floor(x)), int(math.floor(y)))
        ox, oy = (L.x, L.y) if L is not None else (0, 0)
        tp = (pt[0] - ox, pt[1] - oy)                          # in the coordinates of the array being painted
        lp = (self._last[0] - ox, self._last[1] - oy) if self._last else tp
        region = layer_region(self.sel, L) if L is not None else self.sel
        if mode == 'brush':
            rgb = _picked_rgb(p.get('color')) if fill == 'picked colour' else FILLS.get(fill, FILLS['white'])
            if L is None:
                color = tuple(rgb)
            else:
                color = (0, 0, 0, 0) if fill == 'transparent' else tuple(rgb) + (255,)
            paint_segment(self._stroke, lp, tp, size, color, region)
        elif mode == 'clone':
            ofx, ofy = self._clone_off
            n = max(1, int(math.ceil(math.hypot(tp[0] - lp[0], tp[1] - lp[1]) / max(1.0, size * 0.2))))
            for i in range(1, n + 1):                          # stamps spaced along the segment, so a stroke is continuous
                cx, cy = lp[0] + (tp[0] - lp[0]) * i / n, lp[1] + (tp[1] - lp[1]) * i / n
                stamp_clone(self._stroke, self._stroke_orig, cx, cy, ofx, ofy, size, int(p.get('hardness', 60)),
                            int(p.get('opacity', 100)), region)
        elif mode == 'paint':
            aliased = 'shape' in p                              # the pencil: hard, jagged pixels
            box = stamp_cover(self._cover, lp, tp, size, 100 if aliased else int(p.get('hardness', 60)), p.get('shape') == 'square', aliased)
            if box is not None:
                weight = self._cover[box[1]:box[3], box[0]:box[2]] * (int(p.get('opacity', 100)) / 100.0)
                rw = region_weight_box(region, self._stroke.shape, box)
                if rw is not None:
                    weight = weight * rw
                part = self._stroke_orig[box[1]:box[3], box[0]:box[2]]
                if 'effect' in p:                               # Dodge / burn: lightens or darkens instead of painting the colour
                    self._stroke[box[1]:box[3], box[0]:box[2]] = tone_paint(part, weight, p['effect'], p['tones'], int(p['exposure']))
                else:
                    self._stroke[box[1]:box[3], box[0]:box[2]] = paint_over(part, self.fg, weight)
        else:                                                  # 'clean': the mask, and a red trace to see it
            paint_segment(self._stroke_mask, lp, tp, size, (255,), region)
            paint_segment(self._stroke, lp, tp, size, (255, 60, 60) + ((255,) if L is not None else ()), region)
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
        mode = self.tool.interactive
        stroke, orig, L, mask = self._stroke, self._stroke_orig, self._stroke_layer, self._stroke_mask
        if mode == 'paint' and self._last is not None:
            self._paint_prev = self._last
        self._stroke = self._stroke_layer = self._stroke_orig = self._stroke_mask = self._last = self._cover = None
        self.preview = self.lprev = None
        if mode == 'clean':                                    # now rebuild what was painted over
            radius = int((self._read_params() or {}).get('radius', 3))
            hole = mask > 0
            if L is None:
                stroke = inpaint_mask(orig, hole, radius)
            else:
                stroke = orig.copy()
                stroke[..., :3] = inpaint_mask(np.ascontiguousarray(orig[..., :3]), hole, radius, orig[..., 3] > 0)
        changed = not np.array_equal(stroke, orig)
        if not changed:
            self._redraw()
            why = []
            if self.sel is not None:
                why.append("the stroke was outside the working area (press 'Whole image' to remove it)")
            if L is not None:
                why.append("layer '%s' is not under the stroke or already looks like that (select Background to paint the "
                           "picture)" % L.name)
            self._status("%s changed nothing: " % self.tool.name + ("; ".join(why) if why else "nothing to change there."))
        elif L is None:
            self._commit(self.tool.name, stroke)
            self._status("%s stroke done%s." % (self.tool.name, " (inside the selection)" if self.sel is not None else ""), ok=True)
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=stroke)
            self._commit_layers("%s (layer)" % self.tool.name, layers)
            self._status("%s stroke done on layer '%s'." % (self.tool.name, L.name), ok=True)

    def _fill_click(self, e):
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        x, y = self._img_xy(e.x, e.y)
        ox, oy = (L.x, L.y) if L is not None else (0, 0)
        px, py = int(math.floor(x)) - ox, int(math.floor(y)) - oy
        p = self._read_params() or {}
        target = L.rgba if L is not None else self.hist.current
        region = layer_region(self.sel, L) if L is not None else self.sel
        color = self.bg if p.get('use') == 'background' else self.fg
        out = flood_fill(target, px, py, color, int(p.get('tolerance', 32)), bool(p.get('contiguous', True)), int(p.get('opacity', 100)),
                         int(p.get('grow', 1)), region)
        if out is target:
            self._status("Fill bucket: nothing to fill there (outside the picture or the working area, or already that colour).")
            return
        if L is None:
            self._commit("Fill bucket", out)
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self._commit_layers("Fill bucket (layer)", layers)
        self._status("Filled with #%02x%02x%02x." % tuple(color), ok=True)

    def _grad_start(self, e):
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        x, y = self._img_xy(e.x, e.y)
        ox, oy = (L.x, L.y) if L is not None else (0, 0)
        target = L.rgba if L is not None else self.hist.current
        self._grad = dict(p0=(x - ox, y - oy), L=L, orig=target, moved=False)

    def _grad_make(self, e, g=None):
        g = g or self._grad
        x, y = self._img_xy(e.x, e.y)
        L = g['L']
        ox, oy = (L.x, L.y) if L is not None else (0, 0)
        p = self._read_params() or {}
        region = layer_region(self.sel, L) if L is not None else self.sel
        return gradient_fill(g['orig'], g['p0'], (x - ox, y - oy), self.fg, self.bg, p.get('kind', 'linear'),
                             p.get('colours', GRADIENT_COLOURS[0]), int(p.get('opacity', 100)), region)

    def _grad_to(self, e):
        g = self._grad
        x, y = self._img_xy(e.x, e.y)
        if not g['moved'] and math.hypot(x - g['p0'][0] - (g['L'].x if g['L'] else 0), y - g['p0'][1] - (g['L'].y if g['L'] else 0)) * self.zoom < 4:
            return
        g['moved'] = True
        out = self._grad_make(e)
        L = g['L']
        if L is None:
            self.preview = None if out is g['orig'] else out
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self.lprev = tuple(layers)
        self._redraw()
        c = self.canvas
        c.delete('mdraft')
        ox, oy = (L.x, L.y) if L is not None else (0, 0)
        c.create_line((g['p0'][0] + ox) * self.zoom + self.ox, (g['p0'][1] + oy) * self.zoom + self.oy, e.x, e.y, fill='#00e5ff', dash=(4, 3), width=2, tags='mdraft')

    def _grad_end(self, e):
        g, self._grad = self._grad, None
        self.canvas.delete('mdraft')
        self.preview = self.lprev = None
        if not g['moved']:
            self._redraw()
            return
        out = self._grad_make(e, g)
        L = g['L']
        if out is g['orig']:
            self._redraw()
            self._status("Gradient: nothing changed (outside the working area?).")
            return
        if L is None:
            self._commit("Gradient", out)
        else:
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self._commit_layers("Gradient (layer)", layers)
        self._status("Gradient painted.", ok=True)

    def _clone_source(self):
        """The clone source (x, y) in image pixels from the two boxes, or None while it is not set."""
        if self.tool is None or self.tool.interactive != 'clone' or 'src_x' not in self.pvars:
            return None
        try:
            x, y = int(round(float(self.pvars['src_x'].get()))), int(round(float(self.pvars['src_y'].get())))
        except (tk.TclError, ValueError):
            return None
        return (x, y) if x >= 0 and y >= 0 else None

    def _cross(self, sx, sy, tag):
        for dx, dy in ((10, 0), (-10, 0), (0, 10), (0, -10)):
            self.canvas.create_line(sx, sy, sx + dx, sy + dy, fill='#00e5ff', width=2, tags=tag)
        self.canvas.create_oval(sx - 3, sy - 3, sx + 3, sy + 3, outline='#00e5ff', width=2, tags=tag)

    def _draw_clone_marker(self):
        """A cross on the clone source, kept while the Clone stamp tool is selected (follows pan and zoom)."""
        self.canvas.delete('clone')
        src = self._clone_source()
        if src is not None and self.hist is not None:
            self._cross((src[0] + 0.5) * self.zoom + self.ox, (src[1] + 0.5) * self.zoom + self.oy, 'clone')

    def _draw_brush_circle(self, e):
        self.canvas.delete('brush')
        if self._is_brush() and self.hist is not None:
            r = self._brush_params()[0] / 2.0 * self.zoom
            self.canvas.create_oval(e.x - r, e.y - r, e.x + r, e.y + r, outline='#ff3366', tags='brush')
            if self.tool.interactive == 'clone' and self._clone_source() is not None and self._clone_off is not None \
                    and self._clone_source() == self._clone_last:   # the source follows the pointer by the kept offset
                ix, iy = self._img_xy(e.x, e.y)
                sx, sy = (math.floor(ix) + self._clone_off[0] + 0.5) * self.zoom + self.ox, \
                    (math.floor(iy) + self._clone_off[1] + 0.5) * self.zoom + self.oy
                self._cross(sx, sy, 'brush')

    def _on_press(self, e):
        if self.hist is None:
            self._press = None
            return
        if self.mode == 'area':                        # 'Define working area': this drag draws it, with any tool selected
            self._press, self._area_start, self._area_cur = None, (e.x, e.y), None
            return
        if self.mode == 'wand':                        # magic wand: a click selects the area of that colour
            self._press = None
            self._wand_click(e)
            return
        if self.mode == 'lasso':
            self._press = None
            self._lasso_press(e)
            return
        if self._hand_active():                        # the left button moves the picture, whatever the tool
            self._press, self._pan_left = None, True
            self._on_pan_start(e)
            return
        if not self.mode and self._quick_pick(e):      # Ctrl+click: the tool's Pick / Set source button, without pressing it
            self._press = None
            return
        if self.mode:
            self._press = None
            self._pick_click(e)
        elif self._is_brush():
            self._press = None
            self._brush_start(e)
        elif self.tool is not None and self.tool.interactive == 'rect-erase':
            self._press, self._rect_start, self._rect_cur = None, (e.x, e.y), None
        elif self.tool is not None and self.tool.interactive == 'layer':
            self._press = None
            self._lmove_start(e)
        elif self.tool is not None and self.tool.interactive == 'measure':
            self._press = None
            self._measure_press(e)
        elif self.tool is not None and self.tool.interactive == 'fill':
            self._press = None
            self._fill_click(e)
        elif self.tool is not None and self.tool.interactive == 'gradient':
            self._press = None
            self._grad_start(e)
        elif self.tool is not None and self.tool.place == 'segment' and 'x0' in self.pvars:
            self._press, self._placing = None, True
            self._seg_start(e)
        elif self.tool is not None and self.tool.place and 'x' in self.pvars:
            self._press, self._placing = None, True
            self._place_at(e)
        else:
            grab = hit_handle(self.sel, self.zoom, self.ox, self.oy, e.x, e.y) if self._rect_sel() else None
            if grab == 'move' and self._moves_content(e):
                self._press = None
                self._start_content_move(e)
            elif grab:
                self._press = None
                self._grab = (grab, self._img_xy(e.x, e.y), self.sel)
            else:
                self._press, self._drag_sel = (e.x, e.y), None

    def _place_at(self, e):
        """A 'place' tool (text, scale bar): the pixel under the pointer becomes its x, y; for a 'segment' tool (Shapes) it becomes
        the end that is being dragged."""
        x, y = self._img_xy(e.x, e.y)
        if self.tool.place == 'segment':
            kx, ky = ('x1', 'y1') if self._seg_end == 'p1' else ('x0', 'y0')
            self.pvars[kx].set(int(math.floor(x)))
            self.pvars[ky].set(int(math.floor(y)))
            return
        self.pvars['x'].set(int(math.floor(x)))
        self.pvars['y'].set(int(math.floor(y)))

    def _moves_content(self, e):
        if self.tool is not None and self.tool.interactive == 'rect-move':
            return True                                               # the 'Move area' tool: the drag always moves the content
        return self.v_content.get() != bool(e.state & 0x4)            # the box, flipped by Ctrl

    def _fill_color(self, img, layer=False):
        """What a move leaves behind. On the Background 'transparent' is white; on a layer (RGBA) it is see-through."""
        f = self.v_fill.get()
        if not layer:
            return border_color(img) if f == 'background' else FILLS.get(f, FILLS['white'])
        if f == 'transparent':
            return (0, 0, 0, 0)
        return tuple(border_color(np.ascontiguousarray(img[..., :3])) if f == 'background' else FILLS[f]) + (255,)

    def _start_content_move(self, e):
        copy = bool(e.state & 0x1) or bool(self.tool is not None and self.tool.interactive == 'rect-move'
                                           and self.pvars.get('copy') is not None and self.pvars['copy'].get())
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        pt = self._img_xy(e.x, e.y)
        if kind == 'layer':
            rect = layer_region(self.sel, L)
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                self._status("The rectangle does not touch layer '%s'." % L.name)
                return
            self._mv = (pt, rect, copy, L.rgba, L)
        else:
            self._mv = (pt, self.sel, copy, self.hist.current, None)

    def _mv_to(self, e):
        """Show the selected pixels moved by the distance dragged so far (one history step is made on release)."""
        (x, y), rect, copy, orig, L = self._mv
        nx, ny = self._img_xy(e.x, e.y)
        out, new = move_region(orig, rect, int(round(nx - x)), int(round(ny - y)), self._fill_color(orig, L is not None), copy)
        if L is None:
            self.preview = None if out is orig else out
            self.sel = new
        else:                                                         # inside the layer: its box is the limit
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self.lprev = None if out is orig else tuple(layers)
            self.sel = (new[0] + L.x, new[1] + L.y, new[2] + L.x, new[3] + L.y)
        self._redraw()
        x0, y0, x1, y1 = self.sel
        self._set_info("%s %d x %d  at (%d, %d)" % ("copy" if copy else "move", x1 - x0, y1 - y0, x0, y0))

    def _mv_end(self):
        _, rect, copy, orig, L = self._mv
        self._mv = None
        out, self.preview = self.preview, None
        layers, self.lprev = self.lprev, None
        label = "Copy content" if copy else "Move content"
        if L is None and out is not None:
            self._commit(label, out)
        elif L is not None and layers is not None:
            self._commit_layers(label + " (layer)", layers)
        self._after_selection()
        self._redraw()

    # ---- 'Erase rectangle' ------------------------------------------------------------------------------
    def _rect_erase_apply(self, rect):
        shape_mask = region_mask(self.sel, self.hist.current.shape) if self.sel is not None else None
        if self.sel is not None:                       # the working area limits this tool like every other
            rect = intersect_rect(rect, self.sel)
            if rect[2] <= rect[0] or rect[3] <= rect[1]:
                self._status("That rectangle is outside the working area: nothing erased ('Whole image' removes the area).")
                return
        p = self._read_params() or {}
        fill, radius, color = p.get('fill', 'white'), int(p.get('radius', 3)), p.get('color', '#ffffff')
        kind, L = self._tool_target()
        if kind == 'text':
            self._status(self._TEXT_LAYER_MSG)
            return
        if kind == 'layer':
            out = erase_rect_layer(L.rgba, layer_region(rect, L), fill, radius, color)
            if shape_mask is not None and out is not L.rgba:
                out = masked_merge(L.rgba, out, layer_region(MaskRegion(rect, shape_mask), L))
            if out is L.rgba:
                self._status("Nothing to erase there on layer '%s'." % L.name)
                return
            layers = list(self.hist.layers)
            layers[self.active] = L.replace(rgba=out)
            self._commit_layers("Erase rectangle (layer)", layers)
        else:
            out = erase_rect(self.hist.current, rect, fill, radius, color)
            if shape_mask is not None and out is not self.hist.current:
                out = masked_merge(self.hist.current, out, MaskRegion(rect, shape_mask))
            if out is self.hist.current:
                self._status("Nothing changed (the rectangle is already that colour).")
                return
            self._commit("Erase rectangle", out)
        self._status("Erased %d x %d px." % (rect[2] - rect[0], rect[3] - rect[1]), ok=True)

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
        if self._rect_sel() and self.hist is not None and not self.mode and not self._is_brush():
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
        if self.tool is not None and (self.tool.place or self.tool.interactive in ('measure', 'fill', 'gradient')):
            self.canvas.config(cursor='crosshair')
            return
        grab = hit_handle(self.sel, self.zoom, self.ox, self.oy, e.x, e.y) if self._rect_sel() else None
        cursor = HANDLE_CURSORS[grab] if grab else 'crosshair'
        if grab == 'move' and self._moves_content(e):
            cursor = 'hand2'
        self.canvas.config(cursor=cursor)

    def _on_drag(self, e):
        if self._pan_left:
            self._on_pan(e)
            return
        if self._grad is not None:
            self._grad_to(e)
            return
        if self._mdrag and self._mpts:
            self._draw_measure_draft(e)
            return
        if self._lasso_down:
            self._lasso_add(e, freehand=True)
            return
        if self._area_start is not None:
            if self._area_cur is None and math.hypot(e.x - self._area_start[0], e.y - self._area_start[1]) < 4:
                return
            self._area_cur = self._rect_from(self._area_start, (e.x, e.y))
            self._draw_selection(self._area_cur)
            if self._area_cur:
                x0, y0, x1, y1 = self._area_cur
                self._set_info("working area %d x %d  at (%d, %d)" % (x1 - x0, y1 - y0, x0, y0))
            return
        if self._rect_start is not None:
            if self._rect_cur is None and math.hypot(e.x - self._rect_start[0], e.y - self._rect_start[1]) < 4:
                return
            self._rect_cur = self._rect_from(self._rect_start, (e.x, e.y))
            self._draw_selection(self._rect_cur)
            if self._rect_cur:
                x0, y0, x1, y1 = self._rect_cur
                self._set_info("erase %d x %d  at (%d, %d)" % (x1 - x0, y1 - y0, x0, y0))
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
        if self._grad is not None:
            self._grad_end(e)
            return
        if self._mdrag:
            self._measure_release(e)
            return
        if self._lasso_down:
            self._lasso_down = False
            return
        if self._area_start is not None:
            rect, self._area_start, self._area_cur = self._area_cur, None, None
            if rect:                                   # a click without a drag keeps waiting for a real rectangle
                self.sel = rect
                self.cancel_mode()
                self._after_selection()
                self._status("Working area set: %d x %d px at (%d, %d). Every tool now acts only inside it; "
                             "'Whole image' removes it." % (rect[2] - rect[0], rect[3] - rect[1], rect[0], rect[1]), ok=True)
            else:
                self._draw_selection()
            return
        if self._rect_start is not None:
            rect, self._rect_start, self._rect_cur = self._rect_cur, None, None
            self._draw_selection()
            if rect:
                self._rect_erase_apply(rect)
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
            self._set_autofit(False)
            self.ox, self.oy = self._pan[2] + e.x - self._pan[0], self._pan[3] + e.y - self._pan[1]
            self._redraw()

    def _on_wheel(self, e):
        if e.state & 0x1:                                  # Shift + wheel: scroll sideways, as in most editors
            self._set_autofit(False)
            self.ox += max(-4.0, min(4.0, e.delta / 120.0)) * 60
            self._redraw()
            return
        self._zoom_at(1.25 ** max(-4.0, min(4.0, e.delta / 120.0)), e.x, e.y)

    def _on_motion(self, e):
        self._draw_brush_circle(e)
        if self.mode == 'lasso' and self._lasso:
            self._draw_lasso(e)
        if self._mpts and not self._mdrag:
            self._draw_measure_draft(e)
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
    # ---- shapes: the two ends drawn on the picture ------------------------------------------------------------------
    def _seg_start(self, e):
        """Press with the Shapes tool: near an end of the shape being edited it grabs that end; elsewhere it starts a new one."""
        x, y = self._img_xy(e.x, e.y)
        ix, iy = int(math.floor(x)), int(math.floor(y))
        if self._edit_index() is not None:
            try:
                ends = {'p0': (float(self.pvars['x0'].get()), float(self.pvars['y0'].get())),
                        'p1': (float(self.pvars['x1'].get()), float(self.pvars['y1'].get()))}
            except (tk.TclError, ValueError):
                ends = {}
            for end, (ex, ey) in ends.items():
                if math.hypot(ex * self.zoom + self.ox - e.x, ey * self.zoom + self.oy - e.y) <= 10:
                    self._seg_end = end
                    return
        self._seg_end = 'p1'
        for kx, ky in (('x0', 'y0'), ('x1', 'y1')):
            self.pvars[kx].set(ix)
            self.pvars[ky].set(iy)

    def _draw_shape_handles(self):
        """Small squares on the two ends of the shape the Shapes tool is about to make (or is editing); the outline of the area
        the Perspective correction tool will straighten."""
        c = self.canvas
        c.delete('shapeh')
        if self.hist is not None and self.tool is not None and 'tl_x' in self.pvars:
            try:
                q = [(float(self.pvars[k + '_x'].get()), float(self.pvars[k + '_y'].get())) for k in ('tl', 'tr', 'br', 'bl')]
            except (tk.TclError, ValueError):
                return
            if any(v for pt in q for v in pt):
                flat = [v for x, y in q for v in (x * self.zoom + self.ox, y * self.zoom + self.oy)]
                c.create_polygon(*flat, outline='#ffe28a', fill='', width=2, dash=(5, 3), tags='shapeh')
                for i in range(4):
                    sx, sy = flat[2 * i], flat[2 * i + 1]
                    c.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, fill='#ffe28a', outline='black', tags='shapeh')
                    c.create_text(sx + 9, sy - 9, text=str(i + 1), fill='#ffe28a', tags='shapeh', font=('Arial', 9, 'bold'))
            return
        if self.hist is None or self.tool is None or self.tool.place != 'segment' or 'x0' not in self.pvars:
            return
        try:
            pts = [(float(self.pvars['x0'].get()), float(self.pvars['y0'].get())),
                   (float(self.pvars['x1'].get()), float(self.pvars['y1'].get()))]
        except (tk.TclError, ValueError):
            return
        if pts[0] == pts[1]:
            return
        for x, y in pts:
            sx, sy = x * self.zoom + self.ox, y * self.zoom + self.oy
            c.create_rectangle(sx - 4, sy - 4, sx + 4, sy + 4, fill='#ffe28a', outline='black', tags='shapeh')

    # ---- scale and measurements -----------------------------------------------------------------------------------------
    def set_scale(self, upp, unit):
        self.scale = (float(upp), str(unit).strip() or "units")
        self.dirty = True
        self._update_state()
        self._refresh_measures()
        self._draw_measures()
        self._status("Scale set: 1 px = %.4g %s. Scale bars and measurements now use it (it is saved in a .ifl project)." % self.scale, ok=True)

    def clear_scale(self):
        if self.scale is not None:
            self.scale = None
            self.dirty = True
            self._update_state()
            self._refresh_measures()
            self._draw_measures()
            self._status("Scale cleared.", ok=True)

    def scale_by_hand(self):
        if not self._need_image():
            return
        upp = simpledialog.askfloat(APP_NAME, "Units per pixel (for example 0.5 if one pixel is half a micrometre):",
                                    parent=self.root, minvalue=1e-9, initialvalue=self.scale[0] if self.scale else None)
        if not upp:
            return
        unit = simpledialog.askstring(APP_NAME, "Unit name (um, mm, cm ...):", parent=self.root,
                                      initialvalue=self.scale[1] if self.scale else "um")
        if unit:
            self.set_scale(upp, unit)

    def scale_from_dpi(self):
        if not self._need_image():
            return
        if not self.dpi or not self.dpi[0]:
            self._status("This picture has no dpi recorded: calibrate with a measured length, or set the scale by hand.")
            return
        self.set_scale(25.4 / self.dpi[0], "mm")

    def calibrate(self):
        """Set the scale from a measured length: the selected row of the Measurements window, else the last length measured."""
        if not self._need_image():
            return
        rows = [i for i, m in enumerate(self.measures) if m['kind'] == 'length']
        if not rows:
            self._status("Measure a length of known size first (Measure tool, distance), then calibrate.")
            self.tree.selection_set("Measure")
            return
        pick = rows[-1]
        if self._mwin is not None and self._mwin.winfo_exists():
            sel = [int(i) for i in self.m_tree.selection()]
            sel = [i for i in sel if i in rows]
            if sel:
                pick = sel[0]
        m = self.measures[pick]
        real = simpledialog.askfloat(APP_NAME, "The measured length (%.1f px, measurement #%d) is really:" % (m['value'], pick + 1),
                                     parent=self.root, minvalue=1e-9)
        if not real:
            return
        unit = simpledialog.askstring(APP_NAME, "Unit name (um, mm, cm ...):", parent=self.root,
                                      initialvalue=self.scale[1] if self.scale else "um")
        if unit:
            self.set_scale(real / m['value'], unit)

    def _measure_cancel(self):
        self._mpts, self._mdrag = [], False
        self.canvas.delete('mdraft')

    def _measure_press(self, e):
        x, y = self._img_xy(e.x, e.y)
        what = self.pvars['what'].get()
        self.canvas.focus_set()
        now = time.time()
        quick = bool(self._mpts) and now - self._mlast_t < 0.45 and \
            math.hypot(x - self._mpts[-1][0], y - self._mpts[-1][1]) * self.zoom < 6
        self._mlast_t = now
        if what == 'distance':
            if not self._mpts:
                self._mpts, self._mdrag = [(x, y)], True
            else:
                self._mpts.append((x, y))
                self._measure_finish(what)
            return
        if what in ('path length', 'area') and self._mpts:
            if quick and len(self._mpts) >= (3 if what == 'area' else 2):
                self._measure_finish(what)
                return
            if what == 'area' and len(self._mpts) >= 3 and math.hypot(x - self._mpts[0][0], y - self._mpts[0][1]) * self.zoom < 6:
                self._measure_finish(what)
                return
        self._mpts.append((x, y))
        if what == 'angle' and len(self._mpts) == 3:
            self._measure_finish(what)
        else:
            self._draw_measure_draft(e)

    def _measure_release(self, e):
        self._mdrag = False
        if len(self._mpts) == 1:
            x, y = self._img_xy(e.x, e.y)
            if math.hypot(x - self._mpts[0][0], y - self._mpts[0][1]) * self.zoom >= 4:        # a drag: the release is the 2nd point
                self._mpts.append((x, y))
                self._measure_finish('distance')

    def _measure_enter(self):
        if self.tool is not None and self.tool.interactive == 'measure' and self._mpts:
            what = self.pvars['what'].get()
            if len(self._mpts) >= (3 if what == 'area' else 2) and what in ('path length', 'area'):
                self._measure_finish(what)

    def _measure_finish(self, what):
        pts = self._mpts
        self._measure_cancel()
        if what == 'angle':
            kind, value = 'angle', angle_between(pts[0], pts[1], pts[2])
        elif what == 'area':
            kind, value = 'area', polygon_area(pts)
        else:
            kind, value = 'length', polyline_length(pts)
        m = dict(kind=kind, what=what, pts=[(float(a), float(b)) for a, b in pts], value=float(value))
        self.measures.append(m)
        self._refresh_measures()
        self._draw_measures()
        self._status("#%d %s: %s" % (len(self.measures), what, format_measure(value, kind, self.scale)), ok=True)

    def _measure_text(self, m):
        return format_measure(m['value'], m['kind'], self.scale)

    def _draw_measure_draft(self, e):
        c = self.canvas
        c.delete('mdraft')
        if not self._mpts:
            return
        pts = [v for x, y in self._mpts for v in (x * self.zoom + self.ox, y * self.zoom + self.oy)] + [e.x, e.y]
        c.create_line(*pts, fill='#00e5ff', width=2, dash=(4, 3), tags='mdraft')
        what = self.pvars['what'].get() if 'what' in self.pvars else 'distance'
        cur = self._img_xy(e.x, e.y)
        allp = self._mpts + [cur]
        if what == 'distance' and len(allp) == 2:
            txt = format_measure(polyline_length(allp), 'length', self.scale)
        elif what == 'path length' and len(allp) >= 2:
            txt = format_measure(polyline_length(allp), 'length', self.scale)
        elif what == 'area' and len(allp) >= 3:
            txt = format_measure(polygon_area(allp), 'area', self.scale)
            c.create_line(e.x, e.y, pts[0], pts[1], fill='#00e5ff', dash=(2, 4), tags='mdraft')
        elif what == 'angle' and len(allp) == 3:
            txt = format_measure(angle_between(allp[0], allp[1], allp[2]), 'angle')
        else:
            txt = ""
        if txt:
            c.create_text(e.x + 12, e.y - 12, text=txt, fill='#00e5ff', anchor='sw', font=('Arial', 9, 'bold'), tags='mdraft')

    def _draw_measures(self):
        """The finished measurements, drawn over the picture (not part of it): lines, end points and a label each."""
        c = self.canvas
        c.delete('measure')
        if self.hist is None:
            return
        z = self.zoom
        for i, m in enumerate(self.measures):
            pts = [(x * z + self.ox, y * z + self.oy) for x, y in m['pts']]
            flat = [v for p in pts for v in p]
            if m['kind'] == 'area':
                c.create_polygon(*flat, outline='#ffd400', fill='', width=2, tags='measure')
                lx, ly = np.mean([p[0] for p in pts]), np.mean([p[1] for p in pts])
            else:
                c.create_line(*flat, fill='#ffd400', width=2, tags='measure')
                lx, ly = (pts[1] if m['kind'] == 'angle' else ((pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2))
            for px, py in pts:
                c.create_oval(px - 3, py - 3, px + 3, py + 3, fill='#ffd400', outline='black', tags='measure')
            label = c.create_text(lx + 6, ly - 6, text="%d: %s" % (i + 1, self._measure_text(m).split(" = ")[-1]), fill='black',
                                  anchor='sw', font=('Arial', 9, 'bold'), tags='measure')
            bb = c.bbox(label)
            if bb:
                bg = c.create_rectangle(bb[0] - 2, bb[1] - 1, bb[2] + 2, bb[3] + 1, fill='#fff3a0', outline='#999999', tags='measure')
                c.tag_lower(bg, label)

    def show_measurements(self):
        """Measure > Measurements: the list of what has been measured, to copy, to calibrate the scale from, or to clear."""
        if not self._need_image():
            return
        if self._mwin is not None and self._mwin.winfo_exists():
            self._mwin.lift()
            self._refresh_measures()
            return
        w = tk.Toplevel(self.root)
        w.title("Measurements - " + APP_NAME)
        self._mwin = w
        self.m_tree = ttk.Treeview(w, columns=('what', 'value'), show='headings', height=9, selectmode='browse')
        self.m_tree.heading('what', text="Measurement")
        self.m_tree.heading('value', text="Result")
        self.m_tree.column('what', width=130)
        self.m_tree.column('value', width=270)
        self.m_tree.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        row = tk.Frame(w)
        row.pack(fill=tk.X, padx=6, pady=(0, 6))
        for text, cmd in (("Copy", self.copy_measurements), ("Calibrate from selected...", self.calibrate),
                          ("Delete", self.delete_measure), ("Clear all", self.clear_measures)):
            tk.Button(row, text=text, command=cmd).pack(side=tk.LEFT, padx=2)
        self.l_mscale = tk.Label(w, text="", anchor='w', fg='#555555', font=('Arial', 8))
        self.l_mscale.pack(fill=tk.X, padx=8, pady=(0, 6))
        w.protocol('WM_DELETE_WINDOW', self._mwin_close)
        self._refresh_measures()

    def _mwin_close(self):
        if self._mwin is not None:
            self._mwin.destroy()
        self._mwin = None

    def _refresh_measures(self):
        if self._mwin is None or not self._mwin.winfo_exists():
            return
        self.m_tree.delete(*self.m_tree.get_children())
        for i, m in enumerate(self.measures):
            self.m_tree.insert('', tk.END, iid=str(i), values=("%d  %s" % (i + 1, m['what']), self._measure_text(m)))
        self.l_mscale.config(text=("Scale: 1 px = %.4g %s" % self.scale) if self.scale else
                             "No scale yet: measure a known length, then 'Calibrate from selected...'.")

    def measurement_table(self):
        """Tab-separated text of the measurements: number, kind, value in pixels (or degrees), value in units, unit."""
        lines = ["#\tkind\tvalue (px, px2 or deg)\tvalue (units)\tunit"]
        for i, m in enumerate(self.measures):
            cal, unit = "", ""
            if self.scale and m['kind'] != 'angle':
                k = 2 if m['kind'] == 'area' else 1
                cal, unit = "%.6g" % (m['value'] * self.scale[0] ** k), self.scale[1] + ("2" if k == 2 else "")
            lines.append("%d\t%s\t%.4f\t%s\t%s" % (i + 1, m['what'], m['value'], cal, unit))
        return "\n".join(lines)

    def copy_measurements(self):
        if not self.measures:
            self._status("Nothing measured yet.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.measurement_table())
        self._status("The table of measurements is on the clipboard (paste it into a spreadsheet).", ok=True)

    def delete_measure(self):
        sel = self.m_tree.selection() if self._mwin is not None and self._mwin.winfo_exists() else ()
        if sel:
            del self.measures[int(sel[0])]
            self._refresh_measures()
            self._draw_measures()

    def clear_measures(self):
        self.measures = []
        self._refresh_measures()
        self._draw_measures()

    # ---- keyboard shortcuts and mouse -----------------------------------------------------------------------------------
    def show_shortcuts(self):
        """Help > Keyboard shortcuts and mouse: every key and click combination of the program, and how it compares with other editors."""
        win = getattr(self, '_help_win', None)
        if win is not None and win.winfo_exists():
            win.lift()
            return
        w = tk.Toplevel(self.root)
        w.title("Keyboard shortcuts and mouse - " + APP_NAME)
        w.geometry("1100x640")
        self._help_win = w
        tree = ttk.Treeview(w, columns=('what', 'cmp'), show='tree headings', selectmode='none')
        tree.heading('#0', text="Keys")
        tree.heading('what', text="What it does")
        tree.heading('cmp', text="Like other programs?")
        tree.column('#0', width=270)
        tree.column('what', width=560)
        tree.column('cmp', width=270)
        sb = ttk.Scrollbar(w, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(fill=tk.BOTH, expand=True)
        parents = {}
        for section, keys, what, cmp_ in SHORTCUTS:
            if section not in parents:
                parents[section] = tree.insert('', tk.END, text=section, open=True)
            tree.insert(parents[section], tk.END, text=keys, values=(what, cmp_))

    # ---- histogram window ------------------------------------------------------------------------------
    HIST_W, HIST_H = 512, 200

    def show_histogram(self):
        """View > Histogram: the distribution of the values of the picture as it is shown now (preview included), per colour
        and as lightness, optionally only inside the working area; while Levels is open its black / white points are drawn."""
        if not self._need_image():
            return
        if self._hist_win is not None and self._hist_win.winfo_exists():
            self._hist_win.lift()
            self._hist_key = None
            self._hist_refresh()
            return
        w = tk.Toplevel(self.root)
        w.title("Histogram - " + APP_NAME)
        w.resizable(False, False)
        self._hist_win = w
        self.v_hist_log, self.v_hist_area = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        self.c_hist = tk.Canvas(w, width=self.HIST_W + 20, height=self.HIST_H + 26, bg='#fafafa', highlightthickness=0)
        self.c_hist.pack(padx=6, pady=(6, 0))
        row = tk.Frame(w)
        row.pack(fill=tk.X, padx=6)
        tk.Checkbutton(row, text="Logarithmic", variable=self.v_hist_log, command=self._hist_force).pack(side=tk.LEFT)
        tk.Checkbutton(row, text="Working area only", variable=self.v_hist_area, command=self._hist_force).pack(side=tk.LEFT)
        self.l_hist = tk.Label(w, text="", anchor='w', font=('Consolas', 8), justify='left')
        self.l_hist.pack(fill=tk.X, padx=8, pady=(0, 6))
        w.protocol('WM_DELETE_WINDOW', self._hist_close)
        self._hist_key = None
        self._hist_refresh()

    def _hist_close(self):
        if self._hist_win is not None:
            self._hist_win.destroy()
        self._hist_win = None

    def _hist_force(self):
        self._hist_key = None
        self._hist_refresh()

    def _hist_schedule(self):
        if self._hist_win is None or self._hist_pending or self.hist is None:
            return
        self._hist_pending = True
        self.root.after(90, self._hist_refresh)

    def _hist_refresh(self):
        self._hist_pending = False
        if self._hist_win is None or not self._hist_win.winfo_exists() or self.hist is None:
            return
        img = self._shown()
        marks = ()
        if self.tool is not None and self.tool.name == 'Levels' and self.pvars:
            try:
                marks = (int(float(self.pvars['in_black'].get())), int(float(self.pvars['in_white'].get())))
            except (tk.TclError, ValueError, KeyError):
                marks = ()
        key = (id(img), self.sel and tuple(self.sel), id(getattr(self.sel, 'mask', None)), self.v_hist_log.get(),
               self.v_hist_area.get(), marks)
        if key == self._hist_key:
            return
        self._hist_key = key
        mask = None
        if self.v_hist_area.get() and self.sel is not None:
            mask = region_mask(self.sel, img.shape) > 0.5 if region_mask(self.sel, img.shape) is not None else None
            if mask is None:
                mask = np.zeros(img.shape[:2], bool)
                x0, y0, x1, y1 = self.sel
                mask[y0:y1, x0:x1] = True
        counts = histogram_counts(img, mask)
        gray = cv2.cvtColor(np.ascontiguousarray(img), cv2.COLOR_RGB2GRAY)
        lum = np.bincount(gray[mask] if mask is not None else gray.ravel(), minlength=256).astype(np.float64)
        c, W, H = self.c_hist, self.HIST_W, self.HIST_H
        c.delete('all')
        data = counts.astype(np.float64)
        if self.v_hist_log.get():
            data, lum = np.log1p(data), np.log1p(lum)
        top = max(float(np.percentile(np.concatenate([data.ravel(), lum]), 99.7)), 1.0)    # a spike at 0 or 255 must not flatten the rest
        def ys(v):
            return 10 + H - min(1.0, v / top) * (H - 6)
        pts = [10, 10 + H] + [v for i in range(256) for v in (10 + i * W / 255.0, ys(lum[i]))] + [10 + W, 10 + H]
        c.create_polygon(*pts, fill='#d8d8d8', outline='')
        for k, col in enumerate(('#e03030', '#20a020', '#3050e0')):
            line = [v for i in range(256) for v in (10 + i * W / 255.0, ys(data[k, i]))]
            c.create_line(*line, fill=col)
        c.create_rectangle(10, 10, 10 + W, 10 + H, outline='#888888')
        for i in range(0, 256, 64):
            c.create_text(10 + i * W / 255.0, 10 + H + 10, text=str(i), font=('Arial', 7), fill='#555555')
        c.create_text(10 + W, 10 + H + 10, text="255", font=('Arial', 7), fill='#555555', anchor='e')
        for v, name in zip(marks, ("black", "white")):
            c.create_line(10 + v * W / 255.0, 10, 10 + v * W / 255.0, 10 + H, fill='#000000', dash=(3, 3))
        px = img[mask] if mask is not None else img.reshape(-1, 3)
        if len(px):
            self.l_hist.config(text="pixels %d   mean R %.0f  G %.0f  B %.0f   min %d  max %d" % (
                len(px), px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean(), px.min(), px.max()))
        else:
            self.l_hist.config(text="nothing selected")

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
                if self.scale:
                    text += "   1 px = %.4g %s" % self.scale
                if self.sel:
                    x0, y0, x1, y1 = self.sel
                    text += "   selection %d x %d" % (x1 - x0, y1 - y0)
        self.info.config(text=text)

    def _update_state(self):
        h = self.hist
        self.b_undo.config(state=tk.NORMAL if h and h.can_undo() else tk.DISABLED)
        self.b_redo.config(state=tk.NORMAL if h and h.can_redo() else tk.DISABLED)
        self.b_crop.config(state=tk.NORMAL if h and self.sel else tk.DISABLED)
        self._sync_area_box()
        self.m_edit.entryconfig(0, label="Undo" + (" " + h.undo_label() if h and h.can_undo() else ""),
                                state=tk.NORMAL if h and h.can_undo() else tk.DISABLED)
        self.m_edit.entryconfig(1, label="Redo" + (" " + h.redo_label() if h and h.can_redo() else ""),
                                state=tk.NORMAL if h and h.can_redo() else tk.DISABLED)
        name = os.path.basename(self.save_path or self.path) if self.path else ""
        self.root.title("%s%s - %s" % (name, "*" if self.dirty else "", APP_NAME) if name else APP_NAME)
        self.b_apply.config(text="Update layer" if self._edit_index() is not None else
                            "Add as layer" if self.tool is not None and self.tool.layer else "Apply")
        self._refresh_history()
        self._set_info()


def cli(argv):
    """Command line without a window. Returns an exit code, or None when the arguments are not a batch / list request.

        python ImageForge.pyw --list-tools
        python ImageForge.pyw --batch INPUT_FOLDER OUTPUT_FOLDER (--recipe FILE | --apply "Tool:box=value,box=value" ...)
                              [--format png|jpg|tif] [--suffix _x] [--quality 95] [--recursive] [--overwrite]
    """
    import argparse
    ap = argparse.ArgumentParser(prog="ImageForge.pyw", add_help=True)
    ap.add_argument('--list-tools', action='store_true')
    ap.add_argument('--batch', nargs=2, metavar=('INPUT', 'OUTPUT'))
    ap.add_argument('--recipe')
    ap.add_argument('--apply', action='append', default=[])
    ap.add_argument('--format', choices=('same', 'png', 'jpg', 'tif'), default='same')
    ap.add_argument('--suffix', default="")
    ap.add_argument('--quality', type=int, default=95)
    ap.add_argument('--recursive', action='store_true')
    ap.add_argument('--overwrite', action='store_true')
    ap.add_argument('image', nargs='?')
    ns = ap.parse_args(argv)
    if ns.list_tools:
        for t in TOOLS.values():
            if t.interactive or t.layer:
                continue
            print("%s  [%s]" % (t.name, t.group))
            for p in t.params:
                extra = "choices: " + ", ".join(p.choices) if p.kind == 'choice' else "%s..%s" % (p.lo, p.hi) if p.lo is not None else p.kind
                print("    %s = %r   (%s)" % (p.key, p.default, extra))
        return 0
    if not ns.batch:
        return None
    try:
        steps = load_recipe(ns.recipe) if ns.recipe else []
        steps += [parse_step_text(t) for t in ns.apply]
        validate_steps(steps)
    except ValueError as ex:
        print("error: %s" % ex)
        return 2
    if not steps:
        print("error: give a recipe (--recipe FILE) or at least one --apply")
        return 2
    src, out = ns.batch
    if not os.path.isdir(src):
        print("error: %s is not a folder" % src)
        return 2
    out_abs = os.path.abspath(out)
    files = [q for q in list_images(src, ns.recursive) if not os.path.abspath(q).startswith(out_abs + os.sep)]
    results = run_batch(steps, files, out, ns.format, ns.suffix, ns.overwrite, ns.quality, src if ns.recursive else None,
                        progress=lambda i, n, f: print("[%d/%d] %s" % (i + 1, n, f)))
    bad = [r for r in results if r[1] is None]
    for r in bad:
        print("FAILED %s: %s" % (r[0], r[2]))
    print("%d saved, %d failed" % (len(results) - len(bad), len(bad)))
    return 1 if bad else 0


def main():
    if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
        load_plugins()
        code = cli(sys.argv[1:])
        if code is not None:
            sys.exit(code)
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = ImageForge(root)
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        root.after(100, lambda: app.open_path(sys.argv[1]))
    root.mainloop()


if __name__ == '__main__':
    main()

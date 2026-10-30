# CLAUDE.md

Guidance for Claude Code when working in this repository. Everything is in English.

## What it is

`ImageForge.pyw`: a general-purpose image editor (Tkinter + Pillow + numpy + OpenCV) whose operations are
registered plug-in functions. New features are added over time as the author needs them, so the main design
goal is that adding a tool costs one function. The window/canvas conventions come from the "Clean image" tab
of SpectrumDigitizer (selection box, undo stack, pan/zoom, status line).

Run: `pythonw ImageForge.pyw [image]`. Tests: `python -m unittest discover -s tests -v`.

## Structure (single file, as in the other lab apps)

- **Core, no Tk usage at import** (tests load the file with `SourceFileLoader`): `Param`, `Tool`, `tool()` decorator,
  `TOOLS` registry, `apply_tool()`, built-in tools, `crop()`, `History`, `load_image()`, `save_image()`, `load_plugins()`.
- **`ImageForge` class**: canvas on the left, panel on the right (undo/redo/crop buttons, tool tree grouped by
  `group`, auto-built parameter widgets, Preview / Reset / Apply).
- **`plugins/*.py`**: executed at start-up by `load_plugins()` with `tool, Param, np, cv2, Image` pre-injected. Files
  starting with `_` are skipped; a plug-in that raises is reported and its half-registered tools are removed.

## Key decisions

- **Image = (H, W, 3) uint8 RGB, immutable by convention.** Tools return a new array (or the same object for "no change").
  `History` just keeps the arrays, so undo is free; the byte limit drops the oldest states (always keeps 2).
  Alpha is flattened onto white on load; 16-bit is scaled to 8-bit. Do not add alpha/16-bit without changing
  every tool and the tests (`test_every_tool_returns_rgb_uint8...`).
- **Selection semantics** (`apply_tool`): a non-geometry tool is computed on the *whole* image and only the selected
  rectangle is pasted from the result, so filters with a footprint (blur, sharpen) have no seam at the border.
  `geometry=True` tools (rotate, flip, resize) ignore the selection. A tool that needs to act *on the region's own
  content* (e.g. flip inside the rectangle) does not fit this model; give it its own handling when needed.
- **Preview**: parameter changes are debounced (60 ms) and run the tool on the full image on the Tk thread; `self.preview`
  replaces the displayed array. After Apply the preview is cleared and only recomputed on the next parameter change
  (otherwise the already-applied effect would show twice). `preview=False` for tools where it makes no sense (Resize).
- **Canvas**: image point -> screen is `x*zoom + ox`; only the visible part is cropped and resized per redraw
  (NEAREST above 100 %, BILINEAR below). Left drag = selection (a click clears it), right/middle drag = pan.
  The wheel factor is clamped: `event_generate` delivers an inflated delta in scripted tests.
- **Save never overwrites the source**: `save_path` stays None until a Save As.
- **Slider/number boxes** are built from `Param` (`_build_params`); a number box in the middle of typing is read as
  "no change" (`_read_params` returns None), not as an error.

## Interactive pieces

- **Pick modes** (`self.mode` = 'color' | 'angle', set by `start_pick()`, Esc ends it, selecting another tool cancels it): a
  `Param(pick=...)` puts a button next to its widget. 'color' appends the median of the 3x3 pixels clicked as `#rrggbb` to a
  `'colors'` text param and stays active; 'angle' takes two clicks and writes `straighten_angle()` into the float param
  (level, or vertical if the line is closer to vertical; the sign was checked by rotating a tilted synthetic line).
- **Eraser brush** (`Tool.interactive == 'brush'`): the tool function is a placeholder; the app copies the image on press, calls
  `paint_segment()` per motion (works on the small box around the stroke, clipped to the selection) and pushes one history
  step on release. While painting `self.preview` is the working copy and `_update_preview` is suppressed.
- Clean tools (`erase_colors`, `despeckle`, `flatten_ink`) are ports of `apply_filter`, `despeckle`, `flatten_colors` of
  SpectrumDigitizer; despeckle uses `cv2.connectedComponentsWithStats` (8-connectivity) instead of scipy.

- **Selection handles**: pure helpers `handle_points`, `hit_handle` (screen px, corners before edge midpoints, then 'move' inside),
  `drag_rect` (works from the rectangle at press time, so a drag is a pure function of the distance moved; edges never cross
  or leave the image). `_grab` holds (handle, press point, start rect); `self.sel` is updated live while dragging but the preview
  is recomputed only on release (`_after_selection`). Cursor follows the hit (`_update_cursor`); picks and the brush take priority.
  Arrow keys are bound on the canvas (it takes focus on click) so they do not fight with the tool tree. Tests: `SelectionTests`.

- **Auto-crop** (`autocrop`, geometry tool, no preview): bounding box of the pixels further than the tolerance from the border
  colour ('auto' = median of the outermost pixel ring, `border_color`), plus a margin clamped to the image. One speck in the border
  widens the box (documented in the tool; Despeckle first). Tests: `AutoCropTests`.

- **Join dashed lines** (`join_dashes`, port of SpectrumDigitizer's): groups by `cv2.connectedComponentsWithStats`, two ends per group
  (SVD main axis), local end direction, candidate pairs found in numpy blocks of 256 rows (a text page has thousands of ends; the
  original's Python double loop was only ever run inside a curve's box), best score `d*(3-ca-cb)` first, one join per end and per
  pair of groups, cubic Hermite stroke drawn with PIL. The colour is that of the most contrasted third of the dash's pixels
  (original: the darkest, i.e. white background only). With a selection the result is computed on the whole image and only the
  selection pasted (so a dash crossing the selection edge can be half joined). Tests: `JoinDashesTests`.

## Adding a tool

Prefer a plug-in file when it is an experiment, `@tool` in `ImageForge.pyw` when it is a standard feature (and add a
test: every registered tool is already checked for dtype/shape and for not modifying its input). Group names create
new tree nodes automatically.

## Verified / not verified

Unit tests on synthetic arrays and files; a scripted session with real Tk mouse events (drag selection, move/resize by handles, arrow nudge, Apply, crop,
undo, redo, wheel, pan, click-to-deselect, colour pick, angle pick, brush stroke) and a screenshot from the real window. Not verified: large photos, HiDPI
scaling other than the screenshot script's, drag-and-drop (tkinterdnd2 path), the Save dialogs by hand.

## Ideas not done yet (from SpectrumDigitizer's cleaning tab)

Rotation of the selection (SpectrumDigitizer's working area could rotate), manual link of a broken line, magnifier next to the cursor, arrow keys to nudge by one pixel.

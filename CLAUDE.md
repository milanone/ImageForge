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
  (NEAREST above 100 %, BILINEAR below). Left drag = selection (a click clears it), right/middle drag = pan; the view bar (`center_view`, `fit_view`, `actual_size`,
  Hand toggle `v_hand`) sits above the canvas inside the left pane frame. Left-button pan (`_pan_left`: Hand on, or `_space`
  held) takes priority over every tool in `_on_press`; Space is ignored while the focus is in an entry / spinbox / combobox /
  tree, so typing a space in a text box never pans. Check: `examples/view_session.py`.
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

- **Move content** (not a tool: an app mode): `hit_handle` == 'move' and (`v_content` XOR Ctrl) starts `_mv` = (press point, selection,
  copy?, original image); every motion recomputes `move_region(original, ...)` as the preview (so nothing accumulates) and sets
  `self.sel` to the new rectangle; release commits one history step ('Move content' / 'Copy content'). The fill left behind is
  white, black or `border_color` of the whole image (`v_fill`). `drag_rect('move')` keeps the object inside the image, so nothing is
  ever cut off. The image is still flat pixels: there are no layers, so an object cannot be moved again later independently of
  the background it was pasted on (undo is the way back). Tests: `TextAndMoveTests`.
- **Add text** (`add_text`, `Tool.place=True`): the text is rendered by PIL into an 8-bit alpha mask (multiline, optional rotation
  about the box, opacity), then blended into the image as float and clipped at the borders; the selection clips it further
  (standard region rule). A `place` tool has int params `x`, `y`; a press/drag on the image sets them (`_place_at`), which drives
  the normal debounced preview. Fonts: `FONT_FILES` names -> file names looked up in `FONT_DIRS` (Windows Fonts folder first),
  only those found are offered, plus Pillow's built-in 'default'. The text box is one line: `\n` typed in it is a line break.
  Colour param = `Param(kind='colors', pick='color1')`: first colour of the text, Pick replaces it, '...' opens the colour dialog.
  Not done: editing text that is already applied (it is pixels after Apply), alignment, outline/shadow, tool-wide font bold flag.
- The Apply/Preview/Reset row is packed at the bottom of the panel *first*, so it stays visible however many parameters a tool has.

- **Layers** (document = background array + `Layer` tuple): `Layer` is a frozen dataclass (`eq=False`, identity semantics):
  kind 'image' (`rgba`, straight alpha, immutable) or 'text' (`params` = kwargs of `render_text`, rasterised on demand,
  `lru_cache`d, so the text stays editable), `x`, `y` (may be off-canvas), `visible`, `opacity`. `History` states are
  `(label, base, layers)`; `push(label, img, layers=None)` keeps the layers when None; a layer-only step shares the base array and
  `_bytes()` counts shared arrays once. `composite(base, layers)` returns `base` itself when nothing shows (cheap identity for
  the cache); `_shown()` composites `preview or base` with `lprev or layers`, cached by identity in `_comp`, so pan/zoom do not
  recompute. `self.active` = index of the selected layer or None (Background). `lprev` is the layers preview (slider drag, layer
  drag, text being edited); `_commit_layers` is the way to push a layer change.
  *Ordinary tools act on the selected item* (`_tool_target`: base / image layer / text layer): on an image layer
  `apply_tool_to_layer` runs the tool on the RGB channels and keeps alpha; `layer_region` converts the selection to layer
  coordinates (empty = no overlap = no change). A tool with a `fill` param set to `'transparent'` (Erase / keep colours,
  Eraser brush) makes what it would paint transparent: found by running it with white and with black and comparing (generic,
  no per-tool code); `apply_tool` turns `'transparent'` into white where there is no alpha (Background). The brush paints a
  4-channel working copy `(0,0,0,0)` / `(r,g,b,255)` through `paint_segment`. Text layers refuse pixel tools
  (`_TEXT_LAYER_MSG`). Geometry tools on the Background flatten first after a confirmation (except those below); Crop shifts the layers (`shift_layers`).
  Save writes `_flat()`. Not done: layer blend modes, masks, *Drag moves the content* and Cut/Copy to layer from a layer (they use the Background).
  Colours picked with Pick come from the composite, so on a layer under another or with opacity < 100 they may not match its own.
- **Geometry tools on one layer** (`Tool.layer_geometry`: 'exact' for Rotate 90/180 and Flip, 'resample' for Rotate any angle and
  Resize; `transform_layer`): exact tools run on the RGBA array as it is (pure index shuffling, so four quarter turns give back the
  same bytes); resampling tools run on premultiplied float colour and on the alpha plane (replicated to 3 channels) with `fill`
  forced to black, then un-premultiply, because invisible pixels often carry white and straight-RGB resampling gives a light halo
  (checked: edge pixels of a rotated grey panel keep exactly grey). The centre stays fixed (`x += (w - nw) // 2`). Bicubic
  enlargement can overshoot a bright value on opaque pixels (ringing): expected, not a halo. Auto-crop is not layer-capable (it
  flattens). A first attempt compared float round trips exactly and failed two tests: hence the 'exact' path. Tests:
  `TransformLayerTests`; real window: `examples/layer_geometry_session.py`.
- **Tool-made layers** (`Tool.layer == 'text'`, only Add text): `_apply_layer_tool` adds a layer, or updates the selected one when
  its kind matches (`_edit_index`); selecting such a layer while the tool is open loads its parameters into the boxes
  (`_load_layer_into_tool`); double-click in the Layers tab opens the matching tool. The button reads Add as layer / Update layer.
- **Layer panel traps**: `ttk.Treeview.selection_set` queues `<<TreeviewSelect>>`, which arrives after the `_lsync` guard is
  down: `_on_layer_select` therefore ignores a selection equal to `active` (otherwise it refreshed forever and froze the UI -
  found by a scripted session). Property boxes commit on Return / FocusOut / slider release, one history step each.
  `examples/layers_session.py` is that scripted session (real Tk events, prints what it checked; it grabs the screen).

- **Project file** (`.ifl`, `save_project` / `load_project`, `PROJECT_VERSION` = 1): zip (stored, PNG is already compressed) with
  `manifest.json` (format string, version, width, height, dpi, layers: kind, name, x, y, visible, opacity, `params` for text or
  `file` for image layers), `background.png`, `layer_N.png` (RGBA). Members are only ever read by name (never extracted: no
  path traversal). Written to `<path>.tmp` then `os.replace`, so a failed save keeps the old project. A newer version, a
  wrong format string, a missing member or a size mismatch raise `ValueError` with a plain message (shown in a dialog). Text
  parameters are coerced with `TEXT_KEYS`. History, selection and the active layer are not stored. `open_path` of a project sets
  `save_path` to it (Ctrl+S writes back); of an image leaves it None. `_write`: exporting a flat image while layers exist does not
  clear `dirty`; `save()` with layers and a non-project `save_path` asks again. Tests: `ProjectTests`; real-window check
  `examples/project_session.py` (dialogs mocked: save, close, reopen, edit the text, save back).

- **Inpaint instead of 'nearest colour'** (`inpaint_mask`, `Erase / keep colours` with `fill='inpaint'`, params `grow`, `radius`):
  `cv2.inpaint` (Telea) on the erased mask. A nearest-colour fill (copy of the closest kept pixel via
  `distanceTransformWithLabels`) was written, tested and then removed on Francesco's request because it made visible mess too
  easily (streaks, hard edges); do not bring it back. `grow` dilates the mask (ellipse) so the pale anti-aliased fringe of a line
  goes too, for every fill. On a layer the erased pixels are found by the white/black comparison trick and `inpaint_mask(valid=alpha>0)`
  also treats invisible pixels next to the hole as holes, so their (often white) colour is never a source. Wide holes get smeared:
  the tool doc says so. Not done: Navier-Stokes variant, a texture-synthesis fill. Tests: `InpaintTests`.
- **Erase rectangle / Move area** (group Areas, `interactive='rect-erase'` / `'rect-move'`): *Erase rectangle* drags a rectangle with
  the selection-drawing code (`_rect_from`, `_draw_selection(rect)`, not stored in `self.sel`) and applies on release through
  `erase_rect` (RGB) or `erase_rect_layer` (RGBA, rectangle converted with `layer_region`): flat white/black, `transparent` (white on
  the Background), `inpaint`. *Move area* is the ordinary selection, but `_moves_content` is always true for it, so the drag inside
  the rectangle moves the pixels (the `Copy` param or Shift copies); `_start_content_move` / `_mv_to` / `_mv_end` now handle both
  targets: `_mv` = (press point, rect in target coordinates, copy, array, layer or None); on a layer the rectangle is in layer
  coordinates, `move_region` works on the 4-channel array with fill `(0,0,0,0)` (the 'leaves' box now has `transparent`), the
  object stops at the layer's own border and `self.sel` is shown in canvas coordinates. The checkbox "Drag moves the content" got
  the same layer support. Tests: `RectangleTests`; real window: `examples/rectangle_session.py`.

- **Eraser brush feedback** (`_brush_scope`, `_brush_end`): the brush is limited by the selection and paints on the selected layer
  (or refuses a text layer), and used to do nothing, silently, when a selection was left over from another tool or a layer was
  still selected after Add text / Copy to layer. Now the status line names the limit when a stroke starts and says why a stroke
  changed nothing; a good stroke reports itself. Rule of thumb for 'a tool does nothing': check the selection (Esc) and the
  selected item in the Layers tab first.

- **Brush family** (`_is_brush` = interactive in brush / clone / clean; `_brush_start/_to/_end` dispatch on the tool): all strokes
  work on a copy of the target (`_stroke`, the Background or the active layer's RGBA) and keep the untouched original in
  `_stroke_orig`; coordinates are converted to the target's own (`- (L.x, L.y)`), the clip is the selection (`layer_region` on a
  layer). *Eraser*: `paint_segment`. *Clone stamp* (`stamp_clone`): stamps spaced `size * 0.2` along the segment, sampling the
  ORIGINAL at the stroke start (no feedback smearing when source and destination are close), soft edge = distance falloff over
  `(1 - hardness) * r`, opacity; the source is set with Ctrl+click (`e.state & 0x4`; Alt would be grabbed by the Windows menu),
  `_clone_off` = source - first press, kept across strokes when 'Aligned'; the cross follows the cursor by that offset. *Clean
  brush*: the stroke fills a mask (`paint_segment` on a 2-D array) and a red trace for feedback; on release `inpaint_mask` rebuilds
  it (on a layer with `valid=alpha>0`). Tests: `RetouchTests`.
- **Errors in pythonw**: `root.report_callback_exception = _on_tk_error` appends the traceback to `ImageForge_errors.log`, shows it in
  the status line and resets every half-done drag state (`_stroke`, `_mv`, `_grab`, ...), because without a console a handler error
  made a tool look dead. The log is gitignored.
- **Real-mouse check** (`examples/real_mouse_brush.py`): SetCursorPos / mouse_event / keybd_event through the OS, not
  `event_generate`. Needs the window in the FOREGROUND (it refuses otherwise): the first version ran with the window behind
  another one, the clicks went elsewhere, and it looked like three broken tools while they were fine. The three painting tools
  pass with it. A report 'eraser brush does nothing' that does not reproduce here is therefore a state problem (selection / layer /
  Hand / Space) or an error in the log, not the painting code.

- **Clone source and picked colour** (second round): the clone source is two ordinary int params, `src_x` / `src_y` (-1 = not set;
  `_clone_source()` reads them), so it can be typed, set by the 'Set source' button (`pick='point'` on `src_x`, `start_pick('point')`,
  one click writes both), or by Ctrl+click while painting; `_draw_clone_marker()` keeps a cyan cross on it (called from `_redraw`,
  `_params_changed`, tool change) and `_draw_brush_circle` adds the moving cross (pointer + kept offset). `_clone_off` is rebuilt
  when the source differs from `_clone_last`. 'Erase rectangle' has `fill='picked colour'` + `color` (`pick='color1'`): after a Pick
  the tool's 'Paint with' switches to 'picked colour' by itself (`_pick_click`: any tool whose `fill` offers that choice).
  `erase_rect(..., color)` / `erase_rect_layer(..., color)`; `_picked_rgb` = first colour of the text, white if none.
- **Tools tab layout**: `ttk.PanedWindow` (vertical): tool tree on top, the parameters (a `_scrollable` frame) below, divider at 400 px
  after start (`_set_tools_divider`, 500 ms later so the window has a size) and draggable. The tree's `height` is now only a
  minimum. Before, the parameters were pushed below the fold by the tall list and the clone source button was out of sight.

## Adding a tool

Prefer a plug-in file when it is an experiment, `@tool` in `ImageForge.pyw` when it is a standard feature (and add a
test: every registered tool is already checked for dtype/shape and for not modifying its input). Group names create
new tree nodes automatically.

## Verified / not verified

Unit tests on synthetic arrays and files; a scripted session with real Tk mouse events (drag selection, move/resize by handles, arrow nudge, Apply, crop,
undo, redo, wheel, pan, click-to-deselect, colour pick, angle pick, brush stroke, content move/copy, text placement, layers: add/edit/move/opacity/merge/flatten/undo/save, tools, brush and geometry on a layer) and a screenshot from the real window. Not verified: large photos, HiDPI
scaling other than the screenshot script's, drag-and-drop (tkinterdnd2 path), the Save dialogs by hand.

## Ideas not done yet

Nothing here is promised; Francesco adds tools as he needs them. Ordered roughly by how likely they are to be wanted.

**Layers**
- Layer blend modes (multiply, screen, ...), layer masks, layer groups, lock/link layers.
- *Drag moves the content* and Cut/Copy to layer working from a layer (they use the Background); Pick colours from the
  selected layer instead of the composite.
- Undo history panel (list of steps to jump to), recent files, remember window/panel settings.

**Selection**
- Rotation of the selection (SpectrumDigitizer's working area rotated); free-form (lasso/polygon) selection, magic wand
  (select by colour, with tolerance), invert selection, feather, selection from a layer's alpha, zoom to selection.
- Aspect-ratio-locked selection and numeric entry of its position and size.

**Tools**
- From SpectrumDigitizer: manual link of a broken line (`draw_link`), magnifier next to the cursor, strip ticks inside a frame
  (`strip_inner_ticks`), frame finder (`find_frame`), erase along a polyline (`erase_mask_along`).
- Levels / curves, hue-saturation, colour balance, auto-contrast, white balance, histogram view; noise reduction (median,
  bilateral, non-local means), morphological operations (open/close, dilate/erode), edge detection.
- Perspective correction (four clicked corners), automatic deskew (Hough / projection profile), lens distortion.
- Crop with a fixed aspect ratio or to a size in cm at a dpi; canvas size (add borders); rulers, grid, guides, snapping;
  measuring tools and a scale bar drawn from a calibration (useful for microscopy / lab figures).
- Text: alignment, outline, shadow, background box, bold/italic as flags, several fonts per text; shapes (line, arrow,
  rectangle, ellipse) as editable layers, like the text layer.
- Clone / heal brush, smudge, fill bucket with tolerance, gradient fill.

**Files and use**
- Paste an image from the clipboard (as a layer) and copy the picture to the clipboard; export dialog with JPEG quality, PNG
  compression, dpi; save as layered TIFF/ORA/PSD-compatible if ever needed (the `.ifl` project is the native format).
- Batch mode: apply a chain of tools to a folder of images (record the history steps as a recipe, replay on other files);
  command-line `--apply "Tool:param=value"` for scripts.
- 16-bit and alpha on the Background, ICC profiles, EXIF kept on save, animated GIF / multi-page TIFF.
- Plug-in niceties: a `plugins/` template generator, per-plug-in help text in the tool list, parameter presets.

**Quality / performance**
- The live preview recomputes the whole image on the Tk thread: compute on a downscaled copy while a slider moves, or in a
  worker thread with cancel, for photos of tens of megapixels. `composite()` copies the whole base on every change: cache
  per-layer partial composites or composite only the visible window.
- HiDPI scaling check, Linux/macOS check (fonts, cursors such as `size_nw_se`, `bind_all` mouse wheel), keyboard-shortcut help
  window, GUI tests in CI (the scripted sessions in `examples/` are the closest thing now).

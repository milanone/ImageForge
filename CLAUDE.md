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
- **Implicit Apply** (`_pending_preview`, `_commit_pending`): what a one-shot tool shows as its preview is applied BY ITSELF when the user moves on: selecting another tool (`select_tool`), Save / Save as, Export, Crop to selection; the status line says so and one Undo takes it back (Francesco's rule: Apply must be implicit, there is Undo). It does not matter whether a slider was moved: a tool's default preview counts too (an earlier version applied only after an edit; he wanted the click on 'Local contrast' to be enough). Consequences to remember: scrolling through the tool list applies every tool whose default does something (a tool whose default changes nothing adds no step); not applied are brush / interactive / layer tools and `external` ones (they have no preview). **Undo while only a preview is on screen drops the preview and nothing else** (`undo`). Reported as: 'when I do Remove marks and start the next tool the marks come back' (the Remove marks result was only a live preview and was discarded on the tool switch). Check: `examples/implicit_apply_session.py` (no dialogs: a dialog would hang it).
- **Before / after at the same scale** (`uniform_scale`, `_view_after_resize`, `_before_image`, `_compare_changed`, `_before_key` / `_before_key_up`, `v_compare`, `b_before`):
  asked for while using Upscale ('I want to see before / after keeping the zoom I applied; unticking Preview does nothing'). (1) `_commit` and `_step` (Undo / Redo) used to call `fit_view()` on
  every change of size, so the zoom was lost; now a step that scales the picture IN PROPORTION (`uniform_scale` within 2 %) keeps the picture's size on the screen: zoom /= factor, origin
  unchanged (more pixels in the same place), a fitted view stays fitted, any other change of size (crop, canvas) refits as before. (2) The 'Before' toggle in the view bar (and `\` held, like
  Lightroom) makes `_shown()` return `_before_image()`: the picture without the preview when one is on screen, else the state before the last step, put at the current size (nearest when
  enlarged, so the real pixels show; cached by the ids of what it was made from); a yellow BEFORE tag is drawn on the picture; any step, Undo / Redo, tool change or Esc turns it off.
  (3) The Preview checkbox only means something for a tool with a live preview, so it is greyed out for the others (`_update_preview_check`: Upscale, Resize, brushes, external tools); it used
  to look broken there. Tests: `ViewScaleTests`; real window: `examples/before_after_session.py` (Resize 200 % stands for Upscale: same path, instant).
- **Undo / Redo say what they act on** (`_undo_texts`, `_update_undo_label`, `l_undo`): two lines under the Undo / Redo buttons ('Undo: Despeckle' / 'Redo: -') and the Edit menu entries ('Undo Despeckle'), refreshed from `_update_state` and from `_redraw` (a preview appearing or going changes what Undo does: with only a preview on screen the line reads 'Undo: preview of <tool>' and Undo just drops it; the Undo button is enabled for that too). The label of a step is the label given to `History.push`, i.e. the tool name or 'Move content' etc. Check: `examples/implicit_apply_session.py` (part 6).
- **Preview**: parameter changes are debounced (60 ms) and run the tool on the full image on the Tk thread; `self.preview`
  replaces the displayed array. After Apply the preview is cleared and only recomputed on the next parameter change
  (otherwise the already-applied effect would show twice). `preview=False` for tools where it makes no sense (Resize).
- **Canvas**: image point -> screen is `x*zoom + ox`; only the visible part is cropped and resized per redraw
  (NEAREST above 100 %, BILINEAR below). Left drag = selection (a click clears it), right/middle drag = pan; the view bar (`center_view`, `fit_view`, `actual_size`,
  Hand toggle `v_hand`, now a button in the toolbox (the variable lives in the bar's setup)) sits above the canvas inside the left pane frame. Left-button pan (`_pan_left`: Hand on, or `_space`
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
- **Erase rectangle / Move area** (group Retouch, `interactive='rect-erase'` / `'rect-move'`): *Erase rectangle* drags a rectangle with
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

- **Picked colour everywhere it paints**: Erase rectangle (`color`), Eraser brush (`color`) and Erase / keep colours (`fill_color`, its
  `colors` param being the colours to find) all have `fill='picked colour'` + a `pick='color1'` text param; after a Pick `_pick_click`
  switches 'Paint with' by itself. `_picked_rgb` gives the first colour of the text (white if none). On a layer the brush paints it
  opaque, and Erase / keep colours keeps the alpha (generic path of `apply_tool_to_layer`). Tests: `PickedColourFillTests`.

- **Working area** (`start_area`, `clear_area`, `_sync_area_box`, `_apply_area_box`, mode `'area'`): the box at the top of the panel (Whole image, Invert, X/Y/W/H, tolerance, Grow / Shrink / Feather; the buttons that START the area, the wand and the lasso live in the toolbox only, `b_modes`: one place per function, the panel and the bar used to repeat them) is a
  front door to `self.sel`, i.e. the working area IS the selection (one rectangle, no second concept: Move area, Crop, handles and
  nudge keep using it). Why: before, the rectangle could only be drawn with a non-interactive tool; with a brush / clone / layer /
  rectangle tool selected the drag belonged to the tool and the area could not be set ("I cannot use it"). `mode == 'area'` is
  checked FIRST in `_on_press` (before Hand and the pick modes) so the next drag only draws the area and then the mode ends; the
  tool stays as it was. `_update_state` calls `_sync_area_box` (every area change goes through `_after_selection`), the boxes
  commit on Return / FocusOut / arrow click and are ignored while `_area_sync`. Every tool already took `self.sel` as its region;
  Erase rectangle now intersects its rectangle with it (`intersect_rect`) instead of ignoring it. Status messages say 'working
  area'. Check: `examples/working_area_session.py`.
- Panel space is tight: the checkbox row and hint were shortened and the default window is 1250 x 900 so that the tool list
  (divider at 400 px) and the parameters both show.

- **Progressive slider** (`Param(progressive=True)`): slider position t in 0..1 maps to `lo + (hi-lo)*t^2`, so low values have fine steps and
  high ones coarse; the number box and the slider follow each other. Used by the Diameter of Clone stamp, Clean brush and Eraser brush (default 10).

- **Remove marks** (`remove_marks`, `rebuild_under`, `_stubs`, `_link_stubs`; group Clean-up): for stars/stamps/dots of one colour over a
  drawing made of fills and dark outlines (Francesco's `synechocystis.png`, a cartoon scheme with 16 red stars; the file is not in the
  repo, it is a published figure). Mask = pixels within the tolerance of the picked colours, connected shapes up to `max_area`
  (a large object of a similar colour, such as a pink box, is left alone: the default tolerance is tight, 35), dilated by `grow`.
  `rebuild_under` works per hole in a small ROI: (1) the COLOUR layer is Telea-inpainted with the near-black ink (`max(rgb) < 100`,
  dilated 1) and the holes treated as unknown, so no black is smeared into a shaded fill; (2) `_stubs`: every place where ink crosses
  the rim of the hole (`near <= 3.5`) gives a stub (entry point, direction from a PCA of the ink within 0.8 x `reach` of the
  crossing, width = area / length, colour; where two lines meet right at the hole the near estimate is bent, so the ink further out
  (6 .. 1.3 x `reach` px, rays that point at the hole only) is also split into strokes and replaces it when it gives several or
  another direction: a divider joining a ring, a diagonal ending on an arc); blobs fail the elongation test (`evals[1] < 1.8 *
  evals[0]`; an additive term made 5 px lines fail); (3) links, in this order: `_link_stubs` smooth pairs (facing, cosines >= 0.55,
  similar width, score `dist * (3 - ca - cb)` made worse by the width difference so a thin letter stem does not steal a partner),
  each a cubic Bezier drawn 4x supersampled (`_draw_stroke`); then an unpaired stub about as thick as the lines drawn that runs into
  one of them is extended to it (T junction); then CORNER links (`corners=True`: the two rays meet at a point, drawn as a two-segment
  polyline: a cone's tip hidden under a star). A link is refused when its body would cover more than 15 % visible non-ink pixels
  (a hollow star: the lines at its two sides are not one line); (4) the colour under the strokes is filled SIDE BY SIDE: the fills are cut along the strokes and the ink (4-connected components of
  `hole & ~barrier` plus the free known pixels), and each side is inpainted from the known pixels of its own side only, and the
  barrier/seam pixels take the colour of the nearest side (`distanceTransformWithLabels`), otherwise a mark straddling a border
  tints one side with the other's colour (a grey patch under a star on a circle's outline).
  Tried and dropped: exemplar-based inpainting (Criminisi, patches matched on colour + ink layers; cost ~3 s for 16 stars, but
  it pasted black specks and wrong-colour blobs into flat fills; the Telea guess as a weak template did not cure it), and
  the stub detection ONLY in a band 4..12 px away from the hole (it lost arcs where lines are close together; now it is the fallback
  for bent estimates), and rejecting stubs whose ink shape is small (it also threw away real outlines because the ROI cuts them).
  Known limit: lines that run tangent to the hole instead of into it, or a mark on a junction of three or more lines with a letter
  next to it, can still leave a small gap or blotch: Clone stamp. Tests: `RemoveMarksTests`; real window: `examples/remove_marks_session.py` (synthetic figure).

- **Ctrl+click quick picks** (`_quick_pick`, `_quick_pick_hint`, called from `_on_press` right after the Hand check, only when no pick
  mode is active): a tool with a `Param(pick='point' | 'color' | 'color1')` gets, on Ctrl+click, what its button does without the mode
  staying on: the clone source; a colour appended to the `pick='color'` text; or the `pick='color1'` colour (with `fill` switched to
  'picked colour'). Ctrl+Shift picks the `color1` one on a tool that has both (Erase / keep colours). It reuses `_pick_click(quick=True)`
  with `mode` set only for the call. It takes priority over Ctrl-flips-'Drag moves the content' for those tools (inside a selection on
  Erase / keep colours, Eraser brush, ...). The tool description gets a line about it. Check: `examples/ctrl_click_session.py`.

- **Shape selections** (`MaskRegion`, `wand_mask`, `polygon_mask`, `combine_selection`, `invert_selection`, `grow_selection`,
  `feather_selection`, `mask_outline`; app: `start_wand`, `_wand_click`, `start_lasso`, `_lasso_*`, `invert_sel`, `grow_sel`,
  `feather_sel`, `_set_shape`, `_rect_sel`): `self.sel` is still ONE object that every old code path treats as a rectangle, because a
  `MaskRegion` is a tuple subclass (x0, y0, x1, y1) that also carries `.mask` (float32 canvas-sized 0..1, feathered edges in between).
  Assigning a plain tuple to `self.sel` therefore drops the mask by itself. Consumers of the mask: `apply_tool` through
  `masked_merge(orig, new, region)` (a rectangle pastes its box, a mask blends), `layer_region` (cuts the canvas mask to the layer's
  box and returns a `MaskRegion` in layer coordinates), `paint_segment` / `stamp_clone` (`region_mask(region, img.shape)`: the mask
  must have the shape of the array being painted, otherwise it is ignored: a stale mask after an undo of a crop does nothing wrong),
  `_rect_erase_apply` (merges with the mask after `erase_rect`), `selection_to_layer` (mask = layer alpha, cut = masked fill). Handles,
  nudging, the content move and the cursor shapes are gated by `_rect_sel()`: they exist only for plain rectangles. The wand works on
  `_flat()` (the composite) with the RGB distance of the 3x3 median of the clicked pixel, like the other colour tools
  (`contiguous` = 8-connected component of the click). Modes `'wand'` and `'lasso'` are checked in `_on_press` right after `'area'`, so
  they work with any tool selected, and stay on until Esc; a lasso in progress is cleared by the first Esc. A double click is NOT bound
  (Tk sends only `<Double-Button-1>` to the more specific binding, and the second click of a quick pair was lost for the wand):
  `_lasso_press` times the clicks itself. The outline is `mask_outline` (cv2.findContours + approxPolyDP, the 300 longest lines,
  cached per mask object in `_outline`), drawn white + black dashed. Invert on a rectangle gives the frame; subtract with no
  selection selects everything but the shape. Tests: `MaskSelectionTests`; real window: `examples/selection_session.py`.
  Not done: moving a shape's content (Move area is rectangular), saving the selection, selection from a layer's alpha, rotation.

- **Tone and colour tools** (group Adjustments; all pure numpy / cv2 functions of `img`, each returning `img` itself at its
  defaults so 'no change' is recognised): `levels` (`levels_lut`, a 256-value LUT per call, `apply_lut` for one channel),
  `curves` (`monotone_curve`: Fritsch-Carlson cubic through 5 points, it cannot overshoot, so a rising set of points never makes
  an inverted band; a hand-drawn curve widget was left out on purpose: five sliders are enough and fit the Param machinery),
  `auto_contrast` (percentiles over the WHOLE picture, not the working area: the tool function gets no region), `clahe` (L of
  LAB), `even_illumination` (the blur is made on a copy shrunk by radius/12, then enlarged: same look, far faster; 'divide'
  by the luminance of the background estimate so colours stay), `hue_saturation` (cv2 HSV, hue in degrees, lightness blends to
  white / black), `colour_balance` (weights by luminance: shadows (1-y)^2, highlights y^2, midtones 1-|2y-1|), `white_balance`
  (gains = target / reference per channel; the neutral colour has a `pick='color1'`, so Ctrl+click picks it). Histogram window
  (`show_histogram`, `_hist_schedule` / `_hist_refresh`, `histogram_counts`): a Toplevel that redraws 90 ms after a `_redraw` or a
  parameter change, only when its key (id of the shown array, selection, options, Levels marks) changed; the top of the scale is the
  99.7th percentile of the counts so a spike at 0 or 255 does not flatten the rest. Tests: `ToneColourTests`; real window:
  `examples/tone_session.py`. Not done: a curve editor widget, per-colour-range hue/saturation, 16-bit histograms.

- **Vector layers: text, shapes, scale bars** (`LAYER_KINDS`, `_text_layer` / `_shape_layer` / `_scalebar_layer` and their `*_boxes`,
  `render_shape`, `render_scalebar`): every layer kind that is not 'image' is a `Layer` with `params` and `rgba=None`, drawn on demand
  by `LAYER_KINDS[kind]['render'](**params)` (lru_cached renderers). A kind is one dict: `render`, `keys` (parameter names and types the
  project file keeps), `build(boxes, old layer, scale)` (the boxes of its tool -> a Layer, or None when there is nothing to draw, with
  `empty` as the message), `boxes(layer, scale)` (a layer -> the values to put back into the boxes), `tag` (list label) and `tool`.
  The app code is generic: `_layer_from_params`, `_load_layer_into_tool`, `_apply_layer_tool`, `_on_layer_double`, the project IO and
  `_tool_target` (still returns 'text' for any such layer: the name means 'not pixels') all go through it. Adding a layer kind = a
  renderer, a builder, a boxes function, a dict entry and a `@tool(..., layer='kind')`.
  *Shapes*: the layer is the tight box around the drawing (pad = half the width + the head + 2 px); its points are stored RELATIVE
  to that box, so moving the layer moves the shape, and the boxes show canvas coordinates (`x0..y1` = relative + layer position).
  Drawn 4 x larger with cv2 and shrunk (INTER_AREA); the stroke stops inside an arrow head; dashes are walked along the polyline
  (`_dash_runs`); stroke and fill alpha are composed with the usual 'over' formula. `Tool.place == 'segment'` (Shapes) makes a press set
  both ends, a drag the second, and, with the shape's layer selected, a press within 10 px of an end grabs that end (`_seg_start`,
  `_seg_end`); `_draw_shape_handles` marks the ends. *Scale bar*: px_len = length / units-per-pixel, stored in the layer with the
  resolved label (`text`) and the unit, so a project file draws the same bar without the scale; editing recomputes it from the
  current scale (or from the box 'units per pixel', which overrides it). No scale -> `empty` message, nothing added.
  *Scale and measurements*: `self.scale` = (units per pixel, unit) in `.ifl` manifest key `scale` (`save_project(..., scale)`,
  `project_scale(path)`; `load_project` keeps its 3-tuple); `set_scale` / `calibrate` (from the selected length of the Measurements
  window) / `scale_by_hand` / `scale_from_dpi` (25.4 / dpi mm). The Measure tool (`interactive='measure'`) keeps the points in
  `_mpts` (distance: a drag or two clicks, `_mdrag`; path / area: clicks, double-click via `_mlast_t`, Enter, or the first point;
  angle: three clicks), results in `self.measures` (kind 'length' / 'angle' / 'area', value in px / deg / px2), drawn by
  `_draw_measures` as canvas items (never in the pixels) and listed in the Measurements window (`measurement_table` = TSV).
  `polyline_length`, `polygon_area` (shoelace), `angle_between`, `format_measure` are pure. Tests: `ShapeLayerTests`; real window:
  `examples/shapes_session.py` (answers the two dialogs by replacing `simpledialog.askfloat / askstring`). Not done: measurements in
  the project, a profile line (intensity along a line), counting tools, a text label on shapes, polygon / freehand shapes,
  rotating handles, a ruler along the picture's edges.

- **Clean-up and correction tools** (all registered functions with pure helpers): `reduce_noise` (cv2 median / bilateral /
  fastNlMeansDenoisingColored), `morphology` (`MORPH_OPS`; works on the values, so on a dark-on-white scan *erode* thickens the ink),
  `find_edges`, `estimate_skew(img, max_angle, method)` (default 'projection': the rotation, counter-clockwise like the Rotate tool,
  that maximises the sum of squared row-to-row differences of the Otsu ink mask on a copy shrunk to 900 px, coarse 1/10 of the range
  then refined by thirds down to 0.02 deg; 'hough': length-weighted median angle of HoughLinesP lines, verticals counted as
  horizontals), `auto_straighten` (calls `rotate_free`; leaves a sentence in `TOOL_NOTES[name]` that `apply()` appends to the
  status: the one mechanism for a tool to tell the user a result), `order_corners` / `perspective_warp` (the 'quad' pick: four
  clicks in any order are sorted TL, TR, BR, BL and written into the eight boxes; `_draw_shape_handles` outlines them; all-zero boxes
  = no change; automatic size = the longer opposite sides), `canvas_size` (copyMakeBorder; a cut that would leave nothing returns
  the image unchanged because the generic extremes test must not raise), `aspect_crop_box`, `undistort_radial` (cv2.remap, r
  normalised by the half-diagonal, zoom to hide the border). `Tool.layer_shift(params, old shape) -> (dx, dy)` marks geometry tools
  whose result keeps the layers (Canvas size, Crop to aspect ratio): `apply()` commits `shift_layers(...)` instead of asking to flatten.
  Auto straighten, Perspective and Lens distortion DO flatten (they resample the picture). Tests: `CleanCorrectTests`; real window:
  `examples/clean_session.py`. Not done: manual deskew by dragging, perspective corner dragging on the picture, lens profiles,
  deconvolution, inpainting-based dust removal.

- **Workflow: history steps, recipes, batch, settings** (`History.steps` / `.lost` / `.recipe()`, `tool_step`, `layer_step`,
  `validate_steps`, `save_recipe` / `load_recipe` (`.ifr`, JSON, version 1), `run_recipe`, `parse_step_text`, `list_images`,
  `batch_output_path`, `run_batch`, `cli`, `settings_path` / `load_settings` / `save_settings` / `add_recent` / `recent_files`):
  next to every `History` state sits the recipe step that made it, or None. `_commit(label, img, layers, step)` and
  `_commit_layers(..., step)` carry it; `apply()` records the Background tools (`_recipe_step_for`: not when a shape selection is
  active, because it belongs to this picture; a plain rectangle is stored as the step's `region` and clipped to each picture's size at
  run time; geometry tools ignore it) and `_apply_layer_tool` records the ADDITION of a text / shape / scale-bar layer (an edit of an
  existing layer is not repeatable). Everything else (brush strokes, moves, tools run on one layer, merges, crops to the selection)
  leaves None and is reported as skipped. Because the list is parallel to `states` and trimmed with them, an Undo or a jump shortens
  the recipe by itself, a new step after an Undo replaces the old branch, and `lost` counts steps dropped by the memory limit (the
  recipe then starts late: said when saving). `run_recipe` is headless: tools through `apply_tool`, layer steps through the
  `LAYER_KINDS` builders with the scale stored in the step, a geometry tool first merges the layers (or shifts them when it has
  `layer_shift`), the layers are merged at the end. `run_batch` never stops on a bad file, writes `name + suffix + ext` in the output
  folder (sub-folders kept when `root_dir` is given), takes a free name (`_1`, `_2`) unless overwrite, and never writes over its
  own source. The dialog processes one file per `after(5)` call so the window stays alive; the output folder is excluded from the
  input list. The command line (`main()` -> `cli()`, only when the first argument starts with `--`) loads the plug-ins and returns
  0 / 1 (some files failed) / 2 (bad arguments). The History tab (`_refresh_history`, `jump_to`) rebuilds only when the labels, the
  index or the replayable marks change; `jump_to` sets `hist.index` and refreshes like `_step`. Settings are written only in
  `on_close` (never while editing: the scripted sessions call `root.destroy()` and so never touch the user's file; the workflow session
  sets `IMAGEFORGE_SETTINGS` anyway); a damaged or unwritable file is ignored. Window geometry is restored only when it parses and fits the
  screen. `paste_clipboard` uses `PIL.ImageGrab.grabclipboard` (Windows / macOS; a copied FILE list opens the first picture). The
  Export dialog applies the options to a copy of the flattened picture. A modal `messagebox` in a scripted session hangs it: sessions
  set `a.dirty = False` before `open_path`. Tests: `WorkflowTests`; real window: `examples/workflow_session.py`. Not done:
  the preview on a shrunken copy or in a thread, recording moves / brush strokes, editing a recipe, a recipe with a choice of file,
  copy of the picture to the clipboard.

- **Drawing tools** (`stamp_cover`, `paint_over`, `region_weight_box`, `flood_fill`, `gradient_t`, `gradient_fill`; tools Pencil /
  Paintbrush with `interactive='paint'`, Fill bucket `'fill'`, Gradient `'gradient'`): the colours are app state, not tool boxes:
  `self.fg` / `self.bg` (saved in the settings), the swatches in the bar above the picture, `set_colour`, `swap_colours` (X on the canvas),
  `reset_colours` (D); `_quick_pick` sets them on Ctrl+click (Shift: background) for these three kinds. The paint tools join the brush
  family (`_is_brush`): the stroke works on a copy of the target (Background or the layer's RGBA) like the eraser, but paints through a
  per-stroke COVERAGE map (`self._cover`, float 0..1): `stamp_cover` stamps discs (spaced 10 % of the size, 15 % and aliased for the
  pencil) taking the maximum, and the display is `paint_over(original box, colour, coverage * opacity * region weight)`, so the opacity
  belongs to the stroke and overlaps do not darken (what Photoshop calls flow 100). `paint_over` also handles RGBA with the 'over'
  operator (paint on a see-through pixel makes it opaque). The pencil is told apart from the brush by having a `shape` box. Shift on
  press starts the first segment at `self._paint_prev` (the end of the last stroke). `region_weight_box` is the one place that turns a
  working area (nothing / rectangle / `MaskRegion`) into weights for a box, so a feathered selection fades strokes, fills and
  gradients alike. `flood_fill` compares all channels (alpha too on a layer, so the transparent part is one area), widens the mask by
  `grow` with an ellipse, and returns the same object when nothing changes. The gradient previews through `self.preview` / `lprev`
  while dragging (`_grad`, `_grad_start/_to/_end`; Esc cancels; `_update_preview` is suppressed meanwhile). None of these steps is
  recipe-able (positions belong to one picture). Tests: `DrawingTests`; real window: `examples/draw_session.py` (needs
  `IMAGEFORGE_SETTINGS` only if you care about the saved colours: it does not close the program). Not done: blend modes, a smoothing
  option for strokes, pressure, brush shapes / textures, a pattern fill, a path / curve tool.

- **Dodge / burn** (`tone_paint`, tool 'Dodge / burn' in Retouch, `interactive='paint'`): it rides on the drawing-tools stroke code: `_brush_to`
  tells it from the Paintbrush by the `effect` param (the Pencil is told apart by `shape`) and calls `tone_paint(original box, weight, effect,
  tones, exposure)` instead of `paint_over`; weight = per-stroke coverage x region weight, so a feathered selection fades the effect and a stroke
  does not build up on itself (a new stroke does). A pixel moves a fraction of the way to white (dodge) or black (burn) =
  weight x exposure x the tonal weight of the ORIGINAL pixel (same shadows / midtones / highlights curves as Colour balance), so colours are
  kept, nothing clips and nothing feeds back. RGBA: alpha untouched and see-through pixels change less. `_quick_pick` skips this tool (Ctrl+click
  must not set a colour). Tests: `DodgeBurnTests`; real window: `examples/dodge_burn_session.py`. Not done: a 'sponge' (saturate / desaturate)
  variant, a toggle key (Alt in Photoshop) to flip dodge / burn.

- **Portrait retouch** (`find_faces`, `portrait_retouch_faces`, `_soft_ellipse`; tool in Adjustments; the faces are one text param `faces` = 'x y width; x y width' with
  `Param(pick='faces')`, read by `parse_faces` / written by `format_faces`; empty = the finder's faces): faces come from OpenCV's Haar cascade (`frontalface_alt2`, then `default` keeping only the biggest, because it
  gives false hits) on a copy shrunk to 800 px, remembered for the last picture (`_FACE_CACHE`, key = shape + a sampled sum) because the live preview
  asks at every slider move; a profile, a covered or a small face can be missed, hence the click. Per face, in a crop of +-1.2 face widths: a soft
  face ellipse x a skin map (YCrCb, Cr 150 +- 26, Cb 112 +- 26, blurred) = the weight; smoothing = `cv2.bilateralFilter` blended by 0.85 x amount x
  weight (so eyes, lips, hair, glasses stay), warmth = +-0.4 x amount on R / B, light = `tone_paint` dodge on the midtones. Then, on the whole
  picture, `tone_paint` burn with a weight = 1 - a wide soft pool (1.9 x 2.6 face widths, centred a little below the face, built on a small copy
  and enlarged). The skin colour range was checked on one medium-skin photo and one dark-skin low-key portrait (median Cr 135, Cb 122 in its face box, which the
  first limits +-20 around 153 / 108 covered only partly: hence the wider ones) and not on other skin tones or light conditions: if the smoothing misses
  the skin or touches something else, adjust the Cr / Cb limits. On that portrait 'Light on the face' 25 turned the already grey-ish cheeks greyer:
  a low-key face wants a small light amount. A note ('3 faces found' / 'no face found: click on it') goes through
  `TOOL_NOTES`. **Marking faces by hand**: the `pick='faces'` text param gets three buttons under it (`_build_params`): *Find faces*
  (`find_faces_into`: writes the finder's faces into the list so they can be corrected), *Mark faces* (`start_pick('faces')`: mode stays on, each click
  appends 'x y w' with `w` = the 'Width for a face written without one' box or 22 % of the short side; Esc ends) and *Clear*; Ctrl+click adds one without
  the button (`_quick_pick` / `_quick_pick_hint` know the kind). `_draw_faces_marker` (called from `_redraw` and `_params_changed`) outlines the faces
  in yellow, numbered (the list) or dashed (what the finder sees while the list is empty). A hand-written list belongs to one picture, so it should not
  go into a recipe used on others. Not recipe-aware beyond the generic path. Tests: `PortraitTests`; real window: `examples/portrait_session.py`. Not done: eye
  and teeth brightening, blemish removal (Clean brush does it by hand), hair and face-shape handling, a profile detector, a deep-learning detector.

- **Upscale (local, free)** (`ai_upscale`, `ai_command`, `find_ai_program`, `AI_METHODS`, `Tool.external`, `_start_external` / `_poll_external`): runs the user's own
  `realesrgan-ncnn-vulkan` / `waifu2x-ncnn-vulkan` executables (nothing is bundled or downloaded; both are MIT / BSD-style projects, check before shipping them).
  `find_ai_program` looks in the folder given, `IMAGEFORGE_AI_DIR`, `AI_DIR` (settings key `ai_dir`, set by Tools > AI programs folder...), each as the program's own
  folder or a parent holding it one level down, then the PATH. The picture goes through a temporary PNG; the process is polled so `AI_CANCEL` (Esc) can
  terminate it, and the temporary folder is removed with `ignore_cleanup_errors` after `proc.wait` (on Windows the process still holds a file open for a moment: without
  that Esc ended in WinError 32). Real-ESRGAN always runs at the model's own 4x (a 2x result is reduced with Lanczos, better than the x4 model forced to 2x); waifu2x gets
  `-s` and `-n` (noise 0-3) and an absolute `-m` model folder. `Tool.external=True` tools are skipped by the generic default/extremes tests (they would run the
  real program) and tested with a fake `_popen` (`AITests`); in the editor `apply()` hands them to a worker thread (the status line counts seconds, Apply is disabled, Esc
  stops, a result is dropped if the history moved on meanwhile) and asks before a result above 12 MP. Measured here (Intel Iris Xe, 1182x887 -> 4x): Real-ESRGAN x4plus 152 s,
  waifu2x photo 2x 56 s; Real-ESRGAN gives the most detail and slightly waxy skin, waifu2x is gentler. Real window with the real programs: `examples/upscale_ai_session.py`.
  These are upscalers and denoisers: NOT inpainting, they cannot remove objects (that would need a model such as LaMa or MI-GAN, not added).
  **Progress** (`AI_PROGRESS`, `parse_progress`, `format_eta`, `_tail`, `_show_pbar`, `_hide_pbar`, `self.pbar`): both programs print `12.50%` on stderr after each finished tile, which
  was thrown away at first. stderr goes to a temp file; the poll loop in `ai_upscale` reads its last 400 bytes every 0.1 s (the file is read while the program still writes it: works on
  Windows) and stores the last percentage in `AI_PROGRESS['pct']`; `_poll_external` (Tk thread, every 0.5 s) turns it into a filling `ttk.Progressbar` placed at the right end of the status
  line and the text 'Upscale (local, free): 11 %, about 2 min 05 s left' (ETA = elapsed x (100 - p) / p, shown from 3 %). Tools that report no progress (Remove object local / API) get a
  moving (indeterminate) bar and the seconds. Measured with the real Real-ESRGAN: 11 % after 16 s, estimate 2 min 05 s on a 600x450 picture. The percentage is per TILE, so it is not
  smooth for a small picture (a few tiles). Tests: `AITests.test_progress_is_read_from_the_program_and_formatted`; real window: `examples/upscale_ai_session.py`.

- **Remove object (local, free)** (`lama_remove`, `_lama_fill`, `_lama_session`, `region_bool`, `find_ai_model`, `Tool.takes_region`; group Clean-up, `external=True`): the working area
  IS the input (`takes_region` tools get `region=` from `apply_tool` and are not merged afterwards; `_recipe_step_for` skips them, a selection belongs to one
  picture). LaMa (Carve/LaMa-ONNX `lama_fp32.onnx`, 208 MB, fixed 512 x 512 input `image` (0..1) + `mask`, output 0..255; the model blanks the masked pixels itself:
  blanking them in advance changed nothing) runs through `onnxruntime` on the CPU. Each connected piece of the selection is dilated by `grow` % of ITS size
  (cv2 ellipse, done in a padded box, not on the whole picture), then a square crop of 1.3 x the piece (at least 256 px) is shrunk to 512, filled, enlarged
  (cubic) and pasted back through a soft mask, always taking the model's pixels inside the hole; `grain` adds noise at the level of a ring round the hole
  (the output is smoother than a photo). Why the widening: the fringe of a transparent object (a bottle's green tint, its reflection on the table) is outside
  any outline you draw and the model copied it back (green ghosts at a 25 px growth; gone at about 12 % = 140 px on a 1160 px bottle). What we measured on the
  desk photo (3072 x 4080, Intel laptop CPU): loading the session takes about 60-70 s whatever the graph optimisation level (and also from an already
  optimised copy or from bytes: not the disk, not Google Drive), kept in `_LAMA` for the rest of the program run (the status line says so); one piece about
  6-8 s. Result: thermos and bottle gone, table clean, the forearm behind the bottle plausible but its lower edge a little dark, a cup behind the thermos
  lost its print. Not done: a GPU provider, a lower-resolution tile mode for huge holes, blending several runs, a second pass option, an MI-GAN (30 MB) variant.
  The model and `onnxruntime` are NOT dependencies: the tool says what is missing (`ToolUnavailable`). Tests with a fake session: `RemoveObjectTests`; real
  window with the real model: `examples/remove_object_session.py <folder> [photo]`.
- **Remove object (API, paid)** (`generative_remove`, `gen_pieces`, `gen_edit`, `align_to`, `gen_paste`, `gen_estimate`, `gen_prices`, `or_call`, `or_credit`, `openrouter_key`,
  `OBJECT_GUIDE`, `_build_or_panel`, `show_object_guide`; group Clean-up, `external` + `takes_region`): PAID. The selection is split into connected pieces, one request each
  (the model gets WORDS, not a mask: `what` describes the object, `gen_prompt`); a square crop 1.5 x the piece (+ widening, at least 384, at most GEN_MAX_SIDE = 1024 when sent)
  goes to OpenRouter chat completions (`modalities: [image, text]`, `usage.include`) with the picture as a data URI; the answer comes back ~1 MP and slightly rescaled and
  shifted (measured: x 0.90 + a few px; the crop is not returned at its size), so `align_to` finds an affine transform (ORB + RANSAC, 290-530 inliers on the desk photo; fewer
  than 40 = nothing is pasted, the request is lost) and `gen_paste` warps it to the crop, matches its colours (gain/offset per channel in a ring round the hole) and
  blends it ONLY over the piece widened by `grow` % (feathered): the rest of the picture is the original, byte for byte. Findings that shaped it (desk photo, Gemini 2.5
  flash image, 0.0391 USD per request = 1290 image tokens at 30 USD per million, plus ~1400 input tokens): asking for both bottles at once made the model redraw the whole
  scene (the hair band became a ring, the cup moved); ONE OBJECT PER REQUEST kept everything else, so each is described separately and pasted separately; Gemini 3.1 flash
  image (0.068 USD) was less faithful (it also removed the cup and invented the chair base). Compared with Google Photos' eraser on the same photo, this kept the arm and the
  hair band as in the original where Google redrew the elbow and dropped the band (mean difference to the original over the area 9.5 vs 21.6 levels, a number that includes the
  bottles). Money and privacy rules built in: the confirmation dialog (`_start_external`) states what is sent (crops, sizes, model, the words), the estimate from OpenRouter's PUBLIC
  price list (`gen_prices`, cached, fallback constants), the credit, a face warning (`find_faces` on every crop) and asks yes / no BEFORE anything is sent; the real cost of each
  request is read from `usage.cost` and subtracted from `OR_STATE['credit']` (the server's `/credits` count lags by a few requests, so the program keeps its own);
  `generative_remove(max_cost=)` stops before a request that would pass the limit; no key = nothing sent. The key is read from OPENROUTER_API_KEY or, on Windows, the user
  profile (`winreg`, so one set after the program started is found) and is never shown, stored or logged; errors are built without it. Tests use a fake `_urlopen`
  (`GenerativeTests`, 9 tests, including a fake model that returns the picture rescaled and shifted); `examples/remove_generative_session.py` is free by default (fake service)
  and `--real PHOTO` spends about 0.08 USD (2 requests) and refuses a crop with a face. An EMPTY description is allowed (`GEN_DEFAULT_WHAT` = 'the main object at the centre of the picture', no 'it is at the centre' clause; the dialog says so): it relies on the crop being centred on the selection and is UNTESTED against the live service. THE REAL-SERVICE RUN OF THIS TOOL THROUGH THE WINDOW WAS NOT DONE: the harness blocked
  the paid command; the request format, the alignment and the paste were verified earlier with standalone scripts on the live service (4 requests, 0.185 USD), and the editor
  path only with the fake. Not done: sending a mask or a drawn outline (untested whether Gemini follows it), a free-text extra instruction, other providers, a per-run spending
  limit setting in the UI (the code supports `max_cost`).
- **AI folder memory**: settings key `ai_dirs` (a LIST; `saved_ai_dirs` also reads the single `ai_dir` older versions wrote; `add_ai_dir` puts a new folder first and keeps the others;
  `set_ai_dir`, `choose_ai_dir`, saved at once with `save_settings`, not only on close). `find_ai_program` and `find_ai_model` look (`_ai_spots`) in each of them, in the folders inside them
  whose name starts with the program's name, and in the same in their PARENT (sibling folders). Why both: the user keeps Real-ESRGAN, waifu2x and `lama-onnx` together in one folder, and
  the first version remembered ONE folder, so choosing `lama-onnx` for the model made the app forget the folder with the other two ('not set up' again); a list alone would not have rescued
  the settings already written with only `lama-onnx`, the sibling search does.

- **Base state: Select / move** (`leave_tool`, `_toolbox_click`, `NO_TOOL_DOC`, button `b_modes["Select / move"]` = the old 'Rectangle'): `self.tool is None` is a real state, the one the
  program starts in and the one the user asked for ('the base tool should be select and move'). Reported as: 'from the clone stamp I want to go to Select to move something and the
  tool seems to stay on clone' - the Rectangle / wand / lasso buttons used to start a MODE and leave the tool in use selected (by design, so an area could be drawn with any tool, but a
  drag with the clone stamp still cloned). Now the three Select buttons call `leave_tool()` first (commits a pending preview, clears the panel, the tool tree AND the ops list selection -
  otherwise picking the same tool again fires no `<<TreeviewSelect>>` and the button looks dead), and a SECOND click on the tool in use deselects it (Francesco's idea). The area stays, so
  the way to limit a tool to an area is: draw it in the base state, then pick the tool. In the base state `_moves_content` is true (the drag inside a rectangle moves the content, Shift
  copies, Ctrl moves the frame only); with a tool in use the old rules stand (checkbox xor Ctrl; Move area always). `start_area` and the 'area' mode still exist (sessions, keyboard) but have no button.
  The toolbox button is sunken when there is no tool and no mode (`_sync_toolbox`). Check: `examples/base_tool_session.py`.

- **Automatic fit** (`self.autofit`, `_set_autofit`, `_on_canvas_resize`): `fit_view` turns it on (so opening a picture, an Undo that changes the size
  and the Fit button all do), `_zoom_at` (wheel, Ctrl +/-, 100%) and `_on_pan` turn it off, Center leaves it alone; the canvas `<Configure>` event
  refits while it is on and just redraws otherwise, so it also follows the panel divider and the first real window size (the first fit at
  open time happens before the window has its size). The Fit button's relief shows the state. Check: `examples/autofit_session.py`.

- **Keyboard and mouse conventions** (`SHORTCUTS`, `show_shortcuts`, F1): the table is the single list of every combination, with a column saying
  which editor's convention it follows; when a key or click is added, add its row (a test checks the table is well formed and has no
  duplicates, not that it is complete). The choices: Shift adds and Ctrl subtracts in selections, Ctrl+click picks (colour, clone source) = GIMP's
  scheme, because Photoshop's Alt is grabbed by the Windows menu bar in Tk; X / D, Space-pan, Shift-click straight lines, Ctrl+0 / Ctrl+1
  = common to all; wheel = zoom (Krita / Figma), not scroll. Single keys go through root bindings guarded by `_typing(event)` (a letter typed in a box must not act): `[` / `]` (`step_size`: about 10 % of
  the Size box, clamped to its range), `H` (Hand), Delete / Backspace and Shift+Delete (`clear_selection` / `clear_region`: background colour or
  transparency on a layer / foreground colour; weights follow the working area, feathered masks included), Shift+wheel scrolls sideways
  (`_on_wheel`). Beware when testing: `widget.event_generate('<KeyPress-x>', state=0)` is NOT delivered on Windows, leave `state` out.
  Trap found the hard way: in Tk `<Control-1>` is Control + MOUSE BUTTON 1 (digits 1-5 are buttons), so the Ctrl+1 shortcut was bound to every Ctrl+click and reset the zoom while the key did nothing; the key is `<Control-Key-1>` (`KeyBindingTests` greps the source for this). Known gaps against the usual editors: no single-key tool shortcuts (B, E, ...), no Ctrl+N / Ctrl+W. Check: `examples/keys_session.py`.
  **Info line** (`_set_info`, `_size_text`): the grey line under the picture always starts with the size in pixels (`3072 x 4080 px (12.5 MP)`, MP from 1 MP up), also while the pointer is over the picture (`x=.. y=.. RGB=..` follows after a `|`): before, the pointer text replaced the whole line and the size was never visible while working, so the effect of Upscale / Resize / Crop could not be seen. Checks: `examples/ctrl_click_session.py`, `examples/upscale_ai_session.py`.

- **Tool list layout, toolbox, search** (`TOOL_LAYOUT`, `TOOLBOX`, `tool_icon`, `search_tools`, `_fill_tool_tree`, `_build_toolbox`, `_build_chips`,
  `_refill_ops`): the order of the groups and of the tools inside them is the table `TOOL_LAYOUT`, not the order of registration (a test checks the
  decorators agree with it and that no built-in tool is missing; a tool or group of a plug-in comes after, in registration order). A group
  holding any `interactive` or `layer` tool goes under the *Tools* menu, every other under *Image*. Groups: Draw, Retouch, Annotate, Layers
  (mouse tools); Size and orientation, Adjustments, Filters, Clean-up (one-shot operations). The UI has THREE faces of one model: `self.tree` is the
  model (a ttk.Treeview in a never-packed frame: `selection_set(name)` still fires `<<TreeviewSelect>>` there, which `_on_tree_select` turns into
  `select_tool`; menus, keys and every scripted session select tools through it), the toolbox (`TOOLBOX`: icon Radiobuttons sharing `v_toolsel`,
  plus the wand / lasso buttons (sunken while their mode is on, `_sync_toolbox`) and the Hand Checkbutton), and the panel's search box + chips
  + `self.ops` list (the chosen group, or `search_tools(query)`: every word in the name or group, names starting with the first word first).
  `select_tool` keeps the faces in step (toolbox selection, chip, highlighted row) with the `_ops_sync` guard against echo. Icons are drawn
  with PIL at 4x and shrunk (`tool_icon`; one branch per tool, an unknown name gets its initials; a test checks every toolbox tool has a distinct,
  non-empty icon); `_tip` is the hover hint. Adding a tool: its group, a line in `TOOL_LAYOUT`, and for a mouse tool a line in `TOOLBOX` and a
  drawing in `tool_icon` (the tests fail otherwise). The old tall tree was dropped because 48 tools could not be seen without scrolling
  and the parameters sat below the fold. Ctrl+K = `focus_find`. Key events generated with `event_generate` go to the FOCUS widget: focus the
  entry first. Checks: `examples/toolbox_session.py`, `examples/menus_session.py`.

## Adding a tool

Prefer a plug-in file when it is an experiment, `@tool` in `ImageForge.pyw` when it is a standard feature (and add a
test: every registered tool is already checked for dtype/shape and for not modifying its input). Group names create
new tree nodes automatically (and add it to `TOOL_LAYOUT` to choose its place).

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

**Selection**
- Rotation of the selection (SpectrumDigitizer's working area rotated); selection from a layer's alpha, zoom to selection,
  moving the content of a shape selection, saving / loading a selection, quick mask painting.
- Aspect-ratio-locked selection and numeric entry of its position and size.

**Tools**
- From SpectrumDigitizer: manual link of a broken line (`draw_link`), magnifier next to the cursor, strip ticks inside a frame
  (`strip_inner_ticks`), frame finder (`find_frame`), erase along a polyline (`erase_mask_along`).
- Crop with a fixed aspect ratio or to a size in cm at a dpi; canvas size (add borders); rulers, grid, guides, snapping;
  an intensity profile along a line, counting / marking tool (cells, colonies), measurements saved in the project.
- Text: alignment, outline, shadow, background box, bold/italic as flags, several fonts per text; polygon / freehand shapes,
  a text label on a shape, rounded rectangles, rotation handles.
- Heal brush, smudge, dodge / burn, pattern fill, brush smoothing and pressure, blend modes for strokes.

**Files and use**
- Copy the picture to the clipboard; save as layered TIFF/ORA/PSD-compatible if ever needed (the `.ifl` project is the native format).
- 16-bit and alpha on the Background, ICC profiles, EXIF kept on save, animated GIF / multi-page TIFF.
- Plug-in niceties: a `plugins/` template generator, per-plug-in help text in the tool list, parameter presets.

**Quality / performance**
- The live preview recomputes the whole image on the Tk thread: compute on a downscaled copy while a slider moves, or in a
  worker thread with cancel, for photos of tens of megapixels. `composite()` copies the whole base on every change: cache
  per-layer partial composites or composite only the visible window.
- HiDPI scaling check, Linux/macOS check (fonts, cursors such as `size_nw_se`, `bind_all` mouse wheel), keyboard-shortcut help
  window, GUI tests in CI (the scripted sessions in `examples/` are the closest thing now).

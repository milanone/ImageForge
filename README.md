# ImageForge

A general-purpose image editor in one Tkinter window, built around small **plug-in tools**: adding a new
operation means adding one function. The window layout (fixed canvas with pan/zoom, side panel, undo history)
descends from the image-cleaning tab of [SpectrumDigitizer](https://github.com/milanone/SpectrumDigitizer).

![ImageForge](screenshot.png)

## What it does now

- **Open / Save**: PNG, JPEG, TIFF, BMP, WebP, GIF and `.ifl` projects (drag a file onto the window, or
  `pythonw ImageForge.pyw image.png`). *Save* never overwrites the original: the first save asks for a new name
  (`<name>_edited.png`, or `<name>_edited.ifl` when there are layers).
- **Undo / Redo** of every step (Ctrl+Z / Ctrl+Y), limited to about 1 GB of history.
- **Move an object**: select it with a rectangle, tick **Drag moves the content** (or hold Ctrl) and drag inside the
  selection: the pixels move and the old place is filled with white, black or the border colour; Shift copies instead.
  One drag is one undo step. The object stops at the image border.
- **Layers** (tab *Layers*): pictures stacked over the Background, each with name, visibility, opacity (live while you
  drag the slider) and position (X, Y). Make one from the selection (*Edit > Copy / Cut selection to layer*, Ctrl+J /
  Ctrl+Shift+J: a cut leaves a hole filled like *leaves*), from a file with its transparency (*Open as layer...*) or with
  *Add text*. Raise, lower, duplicate, delete, *Merge down* (onto the layer below or the Background), *Flatten all*.
  The **Move layer** tool drags the selected layer on the image (arrow keys nudge it). Every change is an undo step.
- **Add text** (group Draw): type the text (`\n` = new line), pick font, size, colour (type, **Pick** from the image or
  the colour dialog), rotation and opacity; click or drag on the image to place it, then *Add as layer*. The text stays
  editable: select its layer, change the boxes, press *Update layer* (select *Background* to add a second text).
- **Selection**: drag a rectangle; tools then act only inside it (a click outside it clears it). Drag inside it to move it,
  drag a handle (corners, edge midpoints) to resize it, arrow keys nudge it by 1 px (Shift: 10) once the image has been
  clicked. *Crop to selection*.
- **View**: the bar above the picture has **Center** (middle of the window, same zoom), **Fit** (fill the window and
  centre) and **100%**, plus a **Hand** button: while it is pressed, or while you hold **Space**, dragging with the left
  button moves the picture instead of selecting (right/middle drag always moves it). Wheel = zoom at the cursor.
  The status line shows the pixel under the cursor. The tool list is tall (21 rows) and scrolls.
- **Tools**, with live preview:
  - Tone: brightness / contrast, grayscale (with strength), invert, threshold (plug-in example)
  - Filters: sharpen (unsharp mask), Gaussian blur
  - Geometry: auto-crop borders (uniform white / black / frame colour, tolerance, margin), rotate 90/180, rotate by any angle (with **Measure**: click two points of a line to level it), flip,
    resize (percent or pixels, choice of interpolation)
  - Clean (ported from SpectrumDigitizer): erase / keep colours (**Pick** colours on the image, with tolerance; paint with white, black, transparent or the
    **nearest colour**: each erased pixel takes the colour of the closest pixel that was kept, so a line over a gradient or a
    photo disappears into it),
    despeckle, flatten ink to one colour, join dashed lines (smooth strokes that follow the dashes), eraser brush (drag on the image; one stroke = one undo step)

Geometry tools change the whole image and ignore the selection; all other tools, the eraser brush included, are limited to it.
The selection takes the place of SpectrumDigitizer's working area (which could also be rotated: not ported).

**Tools act on what is selected in the Layers tab.** With the Background selected (or no layers) they change the
Background; with an *image layer* selected they change that layer's own pixels (its transparency is kept; the live
preview and the selection work as usual, the selection being cut to the layer). The eraser brush and *Erase / keep
colours* have a **transparent** option for the paint: on a layer it erases to see through, on the Background it paints
white. A *text layer* is not made of pixels: pixel tools refuse it (edit it with Add text, or merge it down first).
*Drag moves the content* and *Cut/Copy to layer* work from the Background.

**Rotate, flip and resize a single layer**: select an image layer and use *Rotate 90 / 180*, *Rotate (any angle)* (with
live preview and the **Measure** button), *Flip* or *Resize* (percent or pixels): the layer is transformed around its own
centre, its transparency is kept, and the new corners of a rotation are transparent (no light fringe at the edges). With the
Background selected these tools change the whole picture and, if there are layers, ask to merge them first (so does
*Auto-crop borders*); *Crop to selection* keeps the layers.

**Projects**: *Save* with layers writes an `.ifl` project (a zip: `manifest.json`, `background.png`, one PNG per image
layer; text layers keep their text, font and colour), which reopens with all layers and editable text. Saving as PNG/JPEG
etc. writes the flattened picture and does not count as saved while layers exist (closing still asks). Ctrl+S goes back
to the project once there is one; an image file is never overwritten silently.

## Adding a tool

Drop a `.py` file into `plugins/` (see [plugins/example_threshold.py](plugins/example_threshold.py)); it is loaded at
start-up and shows up in the tool list with sliders, boxes and a live preview generated from its parameters:

```python
@tool("Sepia", "Tone", [Param('strength', "Strength (%)", 'int', 100, 0, 100, slider=True)])
def sepia(img, strength):
    ...        # img: (H, W, 3) uint8, never modified in place
    return new_img
```

A broken plug-in is reported on the console and skipped. The same decorator works inside `ImageForge.pyw` for tools
that should ship with the program.

## Limits (what is and is not verified)

- Images are held as **8-bit RGB**: transparency is flattened onto white, 16-bit / grey / palette / CMYK are converted,
  and ICC profiles are not kept. EXIF orientation is applied on load; other metadata is dropped on save, except dpi.
- Tested on synthetic images and a scripted session with real Tk mouse events (selection drag, colour pick, angle pick,
  brush strokes, apply, crop, undo/redo, zoom, pan); the clean-up tools have not been tried on real scans yet; the unit tests
  cover the tools, history, file round-trips and plug-ins. Not tried on very large photos: the live preview recomputes
  the whole image, so heavy filters can lag on tens of megapixels (turn Preview off).
- JPEG is saved at quality 95 without chroma subsampling; there is no quality dialog yet.

## Requirements and tests

`pip install -r requirements.txt` (numpy, Pillow, OpenCV; tkinterdnd2 optional). Run the tests with
`python -m unittest discover -s tests -v`. `python examples/make_screenshot.py` regenerates the screenshot.

## Licence

MIT, see [LICENSE](LICENSE).

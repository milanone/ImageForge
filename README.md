# ImageForge

A general-purpose image editor in one Tkinter window, built around small **plug-in tools**: adding a new
operation means adding one function. The window layout (fixed canvas with pan/zoom, side panel, undo history)
descends from the image-cleaning tab of [SpectrumDigitizer](https://github.com/milanone/SpectrumDigitizer).

![ImageForge](screenshot.png)

## What it does now

- **Open / Save**: PNG, JPEG, TIFF, BMP, WebP, GIF (drag a file onto the window, or `pythonw ImageForge.pyw image.png`).
  *Save* never overwrites the original: the first save asks for a new name (`<name>_edited.png`).
- **Undo / Redo** of every step (Ctrl+Z / Ctrl+Y), limited to about 1 GB of history.
- **Move an object**: select it with a rectangle, tick **Drag moves the content** (or hold Ctrl) and drag inside the
  selection: the pixels move and the old place is filled with white, black or the border colour; Shift copies instead.
  One drag is one undo step. The object stops at the image border.
- **Add text** (group Draw): type the text (`\n` = new line), pick font, size, colour (type, **Pick** from the image or
  the colour dialog), rotation and opacity; click or drag on the image to place it, then Apply.
- **Selection**: drag a rectangle; tools then act only inside it (a click outside it clears it). Drag inside it to move it,
  drag a handle (corners, edge midpoints) to resize it, arrow keys nudge it by 1 px (Shift: 10) once the image has been
  clicked. *Crop to selection*.
- **View**: wheel = zoom at the cursor, right/middle drag = pan, Ctrl+0 fit, Ctrl+1 100 %. The status line shows the
  pixel under the cursor.
- **Tools**, with live preview:
  - Tone: brightness / contrast, grayscale (with strength), invert, threshold (plug-in example)
  - Filters: sharpen (unsharp mask), Gaussian blur
  - Geometry: auto-crop borders (uniform white / black / frame colour, tolerance, margin), rotate 90/180, rotate by any angle (with **Measure**: click two points of a line to level it), flip,
    resize (percent or pixels, choice of interpolation)
  - Clean (ported from SpectrumDigitizer): erase / keep colours (**Pick** colours on the image, with tolerance),
    despeckle, flatten ink to one colour, join dashed lines (smooth strokes that follow the dashes), eraser brush (drag on the image; one stroke = one undo step)

Geometry tools change the whole image and ignore the selection; all other tools, the eraser brush included, are limited to it.
The selection takes the place of SpectrumDigitizer's working area (which could also be rotated: not ported).

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

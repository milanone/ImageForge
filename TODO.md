# TODO

Ideas and open items for ImageForge. Nothing here is promised; the order is a suggestion. Details of what exists already are in `CLAUDE.md`.

## New features (ideas, roughly by impact)

1. ~~Subject selection / background removal~~ **done** (Select subject, Remove background with white / colour / transparent / blurred background). A variant that takes a click or a box to say which object (SAM-style) is not done.
2. **Portrait background blur**: a flat blur of the background exists (Remove background, fill 'blurred picture'); a depth-aware blur (stronger far from the subject) is not done.
3. **Eye and teeth retouch** for Portrait retouch (brighter, sharper eyes, whiter teeth, automatic masks).
4. **Smudge** (the Heal brush is done: classic and LaMa). A heal with a manual source (Alt-click like Photoshop) would be a variant.
5. **Sky selection and sky change** (contrast / saturation, or replacement), from the subject-mask machinery.
6. **Panorama and HDR**: stitch several photos (OpenCV `Stitcher`) and exposure fusion; test with the Castellaneta panorama.
7. **Remove crowds / passers-by** from several shots of the same scene (median background).
8. **Colourise black-and-white photos and restore old ones** (scratches, grain) with local models, like the upscalers.
9. **Lab tools**: intensity profile along a line, counting tool (cells, colonies), measurements saved in the project file.
10. **Comfort**: parameter presets, a recipe applied to a folder with a preview, copy the picture to the clipboard, single-key tool shortcuts.

## Open items from earlier work

- **Not verified:** a real run of "Remove object (API, paid)" through the window (also with an empty description). It costs about 0.08 USD; command: `python examples/remove_generative_session.py --real PHOTO`. Ask before running (it spends money).
- Try a newer compact upscaler (`realesr-general-x4v3`, SPAN) if speed matters: not in the folder, no ncnn builds found.
- Esc could leave the current tool (back to Select / move).
- Decide which other tools should be `auto_preview` (now: Local contrast, Auto contrast, Even out illumination, Grayscale, tools without parameters). Candidates: Auto straighten, Despeckle.
- `screenshot.png` in the README predates many features: regenerate it from the real window.
- The "Enlarge canvas / Enlarge %" tools were left untouched when Upscale was renamed.
- Toolbar icons for XYplorer (files `ImageForge*.ico` / `.png`): pick one design and delete the others.

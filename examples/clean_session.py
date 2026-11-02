"""Scripted check of the clean-up and correction tools in the real window (real Tk mouse events): Perspective correction with four
clicks, Auto straighten (and the angle shown after Apply), Canvas size / Crop to aspect ratio with a layer on top, the filters.

    python examples/clean_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
# a photographed page: a rectangle of text-like rules seen at an angle on a grey table, and the whole picture slightly tilted
page = np.full((240, 360, 3), 246, np.uint8)
for y in range(30, 220, 20):
    cv2.line(page, (30, y), (330, y), (25, 25, 25), 3)
    cv2.putText(page, "ImageForge", (40, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
photo = np.full((360, 520, 3), 110, np.uint8)
src = np.float32([[0, 0], [359, 0], [359, 239], [0, 239]])
dst = np.float32([[90, 40], [450, 70], [430, 300], [60, 270]])
warped = cv2.warpPerspective(page, cv2.getPerspectiveTransform(src, dst), (520, 360), borderValue=(110, 110, 110))
mask = cv2.warpPerspective(np.full((240, 360), 255, np.uint8), cv2.getPerspectiveTransform(src, dst), (520, 360))
photo[mask > 0] = warped[mask > 0]
f = os.path.join(tempfile.gettempdir(), 'persp.png'); Image.fromarray(photo).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(pt, state=0): ev('<ButtonPress-1>', *scr(*pt), state=state); ev('<ButtonRelease-1>', *scr(*pt), state=state)
def find_button(text):
    stack = [a.params_frame]
    while stack:
        w = stack.pop()
        if w.winfo_class() == 'Button' and text in w.cget('text'): return w
        stack.extend(w.winfo_children())

# 1. Perspective correction: four clicks (in a scrambled order), outline, preview, Apply
a.tree.selection_set("Perspective correction"); root.update()
print("nothing set: no change", a.preview is None)
find_button("four corners").invoke(); print("mode:", a.mode)
for pt in ((430, 300), (90, 40), (60, 270), (450, 70)): click(pt)
print("corners (ordered by the tool):", [(a.pvars[k + '_x'].get(), a.pvars[k + '_y'].get()) for k in ('tl', 'tr', 'br', 'bl')], "| mode over:", a.mode is None,
      "| outline drawn:", len(c.find_withtag('shapeh')) > 0)
root.update(); root.update(); a.root.after(150); root.update()
print("preview shown:", a.preview is not None and a.preview.shape[:2])
a.b_apply.invoke(); root.update(); out = a.hist.current
print("page after correction:", out.shape[:2], "(about 360 x 240)")
rows = (out[..., 0] < 100).sum(axis=1); lines = np.nonzero(rows > out.shape[1] * 0.5)[0]
print("rules are level: row spread of each rule <= 3 px:", all(np.ptp(np.nonzero(out[y - 3:y + 4, :, 0] < 100)[0]) <= 7 for y in lines[:1]) if len(lines) else "no rules found")

# 2. Auto straighten a tilted copy and read the note
tilted = cv2.warpAffine(out, cv2.getRotationMatrix2D((out.shape[1] / 2, out.shape[0] / 2), 3.0, 1.0), (out.shape[1], out.shape[0]), borderValue=(110, 110, 110))
a.hist.push("tilted", tilted); a._redraw()
a.tree.selection_set("Auto straighten"); root.update(); a.pvars['max_angle'].set(10); root.update(); a.b_apply.invoke(); root.update()
print("Auto straighten:", a.status.cget('text')[:80])
print("estimated tilt left:", m.estimate_skew(a.hist.current, 10.0))

# 3. Canvas size and aspect crop keep a layer where it was
rgba = np.zeros((20, 20, 4), np.uint8); rgba[..., 0] = 255; rgba[..., 3] = 255
img_layer = m.Layer('image', "dot", 40, 50, rgba=rgba)
a._commit_layers("Add layer", [img_layer]); root.update()
before = a._flat()
a.tree.selection_set("Canvas size"); root.update(); a.pvars['left'].set(30); a.pvars['top'].set(10); a.pvars['right'].set(5); a.pvars['bottom'].set(5); root.update(); a.b_apply.invoke(); root.update()
lay = a.hist.layers[0]
print("Canvas size: layer kept (no merge prompt):", len(a.hist.layers) == 1, "| moved to", (lay.x, lay.y), "| picture", a.hist.current.shape[:2],
      "| the dot is still on the same pixel of the old picture:", bool((a._flat()[50 + 10 + 5, 40 + 30 + 5] == (255, 0, 0)).all()))
a.tree.selection_set("Crop to aspect ratio"); root.update(); a.pvars['ratio'].set('1 : 1'); root.update(); a.b_apply.invoke(); root.update()
lay = a.hist.layers[0]; h, w = a.hist.current.shape[:2]
print("Crop to aspect ratio: square", h == w, (w, h), "| layer at", (lay.x, lay.y), "| layers", len(a.hist.layers))
a.undo(); a.undo(); a.undo()

# 4. filters on the real window
for name, params in (("Reduce noise", dict(method='median', radius=2)), ("Morphology", dict(op='open', radius=1)), ("Find edges", dict(method='Canny')),
                     ("Lens distortion", dict(k1=-0.15))):
    a.tree.selection_set(name); root.update()
    for k, v in params.items(): a.pvars[k].set(v)
    root.update(); before = a.hist.current; a.b_apply.invoke(); root.update()
    print("%-16s image changed: %s | %s" % (name, a.hist.current is not before and not np.array_equal(a.hist.current, before), a.status.cget('text')[:50]))
    a.undo()
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'clean_ui.png'))
a.dirty = False; root.destroy()

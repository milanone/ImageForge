"""Scripted check of the shape selections (magic wand, lasso, invert, grow / shrink, feather) in the real window, with real Tk
mouse events: every tool, the brush, Erase rectangle and Copy to layer must respect the shape.

    python examples/selection_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((300, 440, 3), 235, np.uint8)
cv2.rectangle(img, (30, 30), (150, 130), (200, 40, 40), -1); cv2.rectangle(img, (230, 30), (350, 130), (205, 45, 45), -1)
cv2.circle(img, (120, 220), 50, (40, 40, 200), -1); cv2.rectangle(img, (250, 180), (400, 260), (30, 150, 60), -1)
f = os.path.join(tempfile.gettempdir(), 'sel.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(pt, state=0): ev('<ButtonPress-1>', *scr(*pt), state=state); ev('<ButtonRelease-1>', *scr(*pt), state=state)
def drag(p, q, n=6, state=0):
    ev('<ButtonPress-1>', *scr(*p), state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100 | state)
    ev('<ButtonRelease-1>', *scr(*q), state=state)
def button(text):
    stack = [root]
    while stack:
        w = stack.pop()
        if w.winfo_class() == 'Button' and w.cget('text') == text: return w
        stack.extend(w.winfo_children())
orig = a.hist.current
SHIFT, CTRL = 0x1, 0x4

# 1. magic wand: contiguous, then add the other (similar) square, then remove with Ctrl
a.v_wand_tol.set(30); a.b_modes["Magic wand"].invoke(); print("mode:", a.mode)
click((90, 80)); print("wand on the red square:", tuple(a.sel), "| shape:", m.region_mask(a.sel, orig.shape) is not None, "| mode stays:", a.mode, "| label:", a.l_area.cget('text'))
click((290, 80), SHIFT); print("Shift+click adds the other square:", tuple(a.sel), "| selected px:", int((a.sel.mask > 0.5).sum()), "(two squares = %d)" % (2 * 121 * 101))
click((290, 80), CTRL); print("Ctrl+click removes it again:", tuple(a.sel))
print("outline drawn:", len(c.find_withtag('sel')) > 0, "| no handles (only lines):", all(c.type(i) == 'line' for i in c.find_withtag('sel')))
a.escape(); print("Esc ends the wand:", a.mode is None, "| selection kept:", a.sel is not None)

# 2. an ordinary tool acts only on the shape
a.tree.selection_set("Invert"); root.update(); a.b_apply.invoke(); cur = a.hist.current
print("Invert only on the red square:", bool((cur[40:120, 40:140] == 255 - np.array([200, 40, 40])).all()), "| other square kept:", np.array_equal(cur[30:130, 230:350], orig[30:130, 230:350]), "| background kept:", np.array_equal(cur[150:, :], orig[150:, :]))
a.undo()

# 3. invert the selection: everything but the square; the brush is limited by it too
button("Invert").invoke(); print("Invert selection:", tuple(a.sel), "| square excluded:", float(a.sel.mask[80, 90]) == 0.0)
a.tree.selection_set("Eraser brush"); root.update(); a.pvars['size'].set(30); a.pvars['fill'].set('black'); root.update()
drag((10, 80), (200, 80)); cur = a.hist.current
print("brush crosses the red square without touching it:", np.array_equal(cur[60:100, 40:140], orig[60:100, 40:140]), "| paints outside:", bool((cur[80, 10:25] == 0).all()))
a.undo()

# 4. lasso: a triangle over the blue circle, closed with Enter; grow, shrink, feather
a.deselect(); a.b_modes["Lasso"].invoke()
click((30, 160)); click((60, 160)); click((60, 190)); click((60, 190)); print("double click closes a lasso:", a.sel is not None and a._lasso == [])
a.deselect(); a.mode = 'lasso'
click((60, 170)); click((190, 170)); click((120, 270)); print("lasso points so far:", len(a._lasso))
c.event_generate('<Return>'); root.update(); print("lasso closed:", tuple(a.sel), "| shape:", m.region_mask(a.sel, orig.shape) is not None, "| points cleared:", a._lasso == [])
n0 = int((a.sel.mask > 0.5).sum()); a.v_selpx.set(4); button("Grow").invoke(); n1 = int((a.sel.mask > 0.5).sum()); button("Shrink").invoke(); button("Shrink").invoke(); n2 = int((a.sel.mask > 0.5).sum())
print("grow / shrink px: %d -> %d -> %d" % (n0, n1, n2))
button("Feather").invoke(); fm = a.sel.mask; print("feathered edge has partial values:", bool(((fm > 0.05) & (fm < 0.95)).any()))
a.tree.selection_set("Invert"); root.update(); a.b_apply.invoke(); cur = a.hist.current
print("feathered Invert: some pixels half-way:", bool(((cur != orig).any(axis=2) & (np.abs(cur.astype(int) - (255 - orig.astype(int))).sum(axis=2) > 30)).any()))
a.undo()

# 5. Copy to layer takes the shape as alpha; Erase rectangle is clipped by the shape
a.sel = m.combine_selection(None, m.polygon_mask(orig.shape[:2], [(60, 170), (190, 170), (120, 270)]), 'replace', orig.shape[:2]); a._after_selection()
a.selection_to_layer(False); L = a.hist.layers[-1]
print("layer alpha follows the triangle: transparent corner", int(L.rgba[-1, 0, 3]), "| opaque centre", int(L.rgba[L.rgba.shape[0] // 3, L.rgba.shape[1] // 2, 3]))
a.undo(); a.sel = m.combine_selection(None, m.polygon_mask(orig.shape[:2], [(60, 170), (190, 170), (120, 270)]), 'replace', orig.shape[:2]); a._after_selection()
a.tree.selection_set("Erase rectangle"); root.update(); a.pvars['fill'].set('white'); root.update()
drag((20, 150), (220, 290)); cur = a.hist.current
print("Erase rectangle inside the triangle only:", bool((cur[200, 120] == 255).all()), "| corner of the rectangle outside the triangle kept:", np.array_equal(cur[280:290, 20:40], orig[280:290, 20:40]) and np.array_equal(cur[150:160, 25:50], orig[150:160, 25:50]))
a.undo()

# 6. a rectangle still works as before, and replaces the shape
a.b_modes["Select / move"].invoke(); drag((20, 20), (200, 140))
print("rectangle after a shape:", a.sel, "| plain tuple:", m.region_mask(a.sel, orig.shape) is None, "| handles drawn:", any(c.type(i) == 'rectangle' for i in c.find_withtag('sel')))
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'selection_ui.png'))
a.dirty = False; root.destroy()

"""Scripted check of 'Erase rectangle' and 'Move area' (real window, real mouse events).

    python examples/rectangle_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.zeros((400, 600, 3), np.uint8); img[:] = np.linspace(40, 220, 600).astype(np.uint8)[None, :, None]
cv2.circle(img, (150, 150), 55, (200, 50, 40), -1); cv2.rectangle(img, (350, 200), (500, 320), (40, 110, 190), -1)
f = os.path.join(tempfile.gettempdir(), 'rc.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=5, state=0):
    ev('<ButtonPress-1>', *p, state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n, state=0x100 | state)
    ev('<ButtonRelease-1>', *q, state=state)
orig = a.hist.current

# 1. Erase rectangle on the Background: drag, it erases on release, one step
a.tree.selection_set("Erase rectangle"); root.update(); n = len(a.hist.states)
drag(scr(350, 200), scr(500, 320))
cur = a.hist.current
print("Erase rectangle: steps added", len(a.hist.states) - n, "| square gone:", (cur[205:315, 355:495] == 255).all(), "| outside kept:",
      np.array_equal(cur[:195], orig[:195]), "| selection untouched:", a.sel is None, "|", a.hist.states[-1][0])
a.undo()
# 2. with inpaint
a.pvars['fill'].set('inpaint'); a.pvars['radius'].set(5); root.update(); drag(scr(350, 200), scr(420, 260))
cur = a.hist.current; err = np.abs(cur[205:255, 355:415].astype(int) - np.linspace(40, 220, 600).astype(np.uint8)[355:415][None, :, None].astype(int)).mean()
print("inpaint rectangle: smooth ramp back, mean error", round(float(err), 1), "(a flat white would be ~110)")
a.undo()
# 3. Move area on the Background: draw the rectangle, then drag inside it
a.tree.selection_set("Move area"); root.update(); a.v_fill.set('white')
drag(scr(85, 85), scr(215, 215)); print("rectangle drawn:", a.sel)
r0 = a.sel; n = len(a.hist.states)
drag(scr(150, 150), scr(330, 100))
cur = a.hist.current; x0, y0, x1, y1 = a.sel
print("Move area: sel", r0, "->", a.sel, "| content equal:", np.array_equal(cur[y0:y1, x0:x1], orig[r0[1]:r0[3], r0[0]:r0[2]]),
      "| old place white:", (cur[100:120, 100:120] == 255).all(), "| one step:", len(a.hist.states) - n, "|", a.hist.states[-1][0])
a.undo()
# 4. copy: the checkbox of the tool
a.pvars['copy'].set(True); root.update(); a.sel = r0; a._after_selection(); drag(scr(150, 150), scr(330, 100))
cur = a.hist.current
print("Move area with 'copy': original still there:", np.array_equal(cur[r0[1]:r0[3], r0[0]:r0[2]], orig[r0[1]:r0[3], r0[0]:r0[2]]) or "partly overlapped", "|", a.hist.states[-1][0])
a.undo(); a.pvars['copy'].set(False); root.update()
# 5. on an image layer: copy the circle into a layer, erase a rectangle to transparent, move an area inside the layer
a.tree.selection_set("Invert"); root.update(); drag(scr(85, 85), scr(215, 215)); a.selection_to_layer(False); root.update()
a.tree.selection_set("Move layer"); root.update(); drag(scr(150, 150), scr(450, 120)); a.sel = None
L0 = a.hist.layers[0]; print("layer", L0.rgba.shape[:2], "at", (L0.x, L0.y))
a.tree.selection_set("Erase rectangle"); root.update(); a.pvars['fill'].set('transparent'); root.update()
drag(scr(L0.x + 10, L0.y + 10), scr(L0.x + 60, L0.y + 60))
L1 = a.hist.layers[0]
print("Erase rectangle on the layer: transparent pixels", int((L1.rgba[..., 3] == 0).sum() - (L0.rgba[..., 3] == 0).sum()), "| base untouched:", a.hist.current is orig, "|", a.hist.states[-1][0])
a.tree.selection_set("Move area"); root.update(); a.v_fill.set('transparent')
drag(scr(L1.x + 70, L1.y + 70), scr(L1.x + 120, L1.y + 120)); print("rectangle in the layer:", a.sel)
r1 = a.sel; before = a.hist.layers[0].rgba.copy()
drag(scr(*[(r1[0] + r1[2]) // 2, (r1[1] + r1[3]) // 2]), scr(*[(r1[0] + r1[2]) // 2 + 30, (r1[1] + r1[3]) // 2 + 20]))
L2 = a.hist.layers[0]
print("Move area inside the layer: layer changed", not np.array_equal(L2.rgba, before), "| base untouched:", a.hist.current is orig, "| layer box same size:", L2.rgba.shape == before.shape, "|", a.hist.states[-1][0])
# 6. a text layer refuses
a.tree.selection_set("Add text"); root.update(); a.ltree.selection_set('BG'); root.update(); a.b_apply.invoke()
a.tree.selection_set("Erase rectangle"); root.update(); n = len(a.hist.states); drag(scr(50, 300), scr(120, 350))
print("text layer + Erase rectangle: steps added", len(a.hist.states) - n, "|", a.status.cget('text')[:45])
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'rect_ui.png'))
a.dirty = False; root.destroy()

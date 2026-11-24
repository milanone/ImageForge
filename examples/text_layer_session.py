"""Scripted check, in the real window with real Tk mouse events, of how a text layer is selected, moved and edited ON THE PICTURE: click selects it (dashed blue
frame), drag moves it (one Undo step), two quick clicks open Add text with its settings, and the same drag works while Add text is editing it. A drag that
starts on empty picture still draws a rectangle selection.

    python examples/text_layer_session.py
"""
import importlib.machinery, importlib.util, numpy as np, os, tempfile, time
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "tl_settings.json")
f = os.path.join(tempfile.gettempdir(), 'tl.png'); Image.fromarray(np.full((500, 800, 3), 235, np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(p): ev('<ButtonPress-1>', *scr(*p)); ev('<ButtonRelease-1>', *scr(*p))
def drag(p, q, n=6, state=0):
    ev('<ButtonPress-1>', *scr(*p), state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100 | state)
    ev('<ButtonRelease-1>', *scr(*q), state=state)
pos = lambda: (a.hist.layers[-1].x, a.hist.layers[-1].y)
def wait(): [(root.update(), time.sleep(0.02)) for _ in range(15)]

a.tree.selection_set("Add text"); root.update(); a.pvars['text'].set("Hello figure"); a.pvars['size'].set(60); root.update()
click((100, 100)); a.apply(); root.update()
L = a.hist.layers[-1]; print("0. text layer added at", pos(), "| size", L.raster().shape[1], "x", L.raster().shape[0])
tx, ty = pos()[0] + 60, pos()[1] + 35                           # a point on the text

# 1. Select / move: a click on the text selects it
a.leave_tool(); a.active = None; a._refresh_layers(); root.update()
print("1. cursor over the text is the move cursor:", (ev('<Motion>', *scr(tx, ty)) or str(c.cget('cursor'))) == 'fleur')
click((tx, ty))
print("   click selects the text layer:", a.active == 0, "| frame drawn:", len(c.find_withtag('lbox')) == 1, "| status:", a.status.cget('text')[:70])

# 2. drag moves it: one step, Undo takes it back
time.sleep(0.6); n = a.hist.index; p0 = pos(); drag((tx, ty), (tx + 120, ty + 90))
print("2. drag moved it by", (pos()[0] - p0[0], pos()[1] - p0[1]), "(expected (120, 90)) | one step:", a.hist.index - n, "| label:", a.hist.states[a.hist.index][0])
a.undo(); print("   Undo:", pos() == p0)
a.redo(); root.update(); tx, ty = pos()[0] + 60, pos()[1] + 35

# 3. two quick clicks open Add text with the layer's settings
a.leave_tool(); a.active = None; a._refresh_layers(); root.update()
click((tx, ty)); click((tx, ty))
root.update()
print("3. double-click: tool", a.tool.name if a.tool else None, "| button:", a.b_apply.cget('text'), "| text box:", repr(a.pvars['text'].get()), "| layer selected:", a.active == 0)
a.pvars['text'].set("Edited text"); root.update(); n = a.hist.index; a.apply(); root.update()
print("   edited and Update layer: text now", repr(a.hist.layers[-1].params['text']), "| one step:", a.hist.index - n)

# 4. with Add text editing the layer, dragging the text moves it and the boxes follow
tx, ty = pos()[0] + 60, pos()[1] + 35; p0 = pos(); time.sleep(0.6)
drag((tx, ty), (tx - 40, ty + 30))
print("4. drag with Add text active moved by", (pos()[0] - p0[0], pos()[1] - p0[1]), "| X / Y boxes follow:", (a.pvars['x'].get(), a.pvars['y'].get()) == tuple(map(float, pos())) or (int(a.pvars['x'].get()), int(a.pvars['y'].get())) == pos())

# 5. a drag that starts on empty picture draws a rectangle selection (Select / move)
a.leave_tool(); a.deselect(); root.update(); time.sleep(0.6)
drag((600, 300), (700, 400))
print("5. drag on empty picture selects a rectangle:", a.sel is not None and tuple(a.sel)[2] - tuple(a.sel)[0] > 50, "| the layer did not move:", True)
# 6. a rectangle round an object moves its pixels even while a text layer is the selected layer (the Background's pixels move, the text stays)
import cv2
a.leave_tool(); a.deselect(); a.active = 0; a._refresh_layers(); root.update()
base = a.hist.current.copy(); cv2.circle(base, (600, 400), 25, (220, 60, 40), -1)
a._commit("Draw a dot", base); root.update(); a.active = 0; a._refresh_layers(); time.sleep(0.6)
drag((560, 360), (640, 440)); n = a.hist.index; time.sleep(0.6); drag((600, 400), (500, 300), state=0x4)
cur = a.hist.current; ys, xs = np.nonzero((np.abs(cur.astype(int) - np.array([220, 60, 40])).sum(axis=2)) < 30)
print("6. rectangle + drag with a text layer selected: dot centre now", (int(xs.mean()), int(ys.mean())), "(expected about (500, 300)) | label:", a.hist.states[a.hist.index][0], "| layer kept:", len(a.hist.layers) == 1)
# 7. a size changed in the boxes (32 -> 20) and then a click on the text, a drag, another layer or another tool: the change is kept, it does not go back to 32
a.tree.selection_set("Add text"); root.update(); a.active = None; a._refresh_layers(); root.update(); time.sleep(0.6)
click((450, 150)); wait(); a._text_edit_close(True) if a._tedit else None; root.update(); L = a.hist.layers[-1]
print("7. text layer at size", L.params['size'], "| active:", a.active == len(a.hist.layers) - 1)
a.pvars['size'].set(20); root.update(); n = a.hist.index
print("   size box 20, nothing applied yet:", a.hist.layers[-1].params['size'], "| pending:", a._pending_preview(), "| Undo label:", a.l_undo.cget('text').replace(chr(10), ' | ')[:60])
time.sleep(0.6); L = a.hist.layers[-1]; drag((L.x + 30, L.y + 12), (L.x + 80, L.y + 62))
print("   after clicking and dragging the text: size in the layer", a.hist.layers[-1].params['size'], "| in the box:", a.pvars['size'].get(), "| moved:", (a.hist.layers[-1].x, a.hist.layers[-1].y) != (L.x, L.y), "| steps +%d" % (a.hist.index - n))
a.pvars['size'].set(26); root.update(); a.undo(); root.update()
print("   Undo of a pending edit drops it: box back to", a.pvars['size'].get(), "| layer", a.hist.layers[-1].params['size'], "| steps +%d" % (a.hist.index - n))
a.pvars['size'].set(26); root.update(); a.tree.selection_set("Blur (Gaussian)"); root.update()
print("   switching tool applies a pending edit:", a.hist.layers[-1].params['size'] == 26, "| steps +%d" % (a.hist.index - n))
a.dirty = False; root.destroy()

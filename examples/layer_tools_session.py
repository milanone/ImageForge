"""Scripted check that ordinary tools and the eraser brush work on the selected image layer (real window, real events).

    python examples/layer_tools_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((400, 600, 3), 120, np.uint8); cv2.rectangle(img, (350, 200), (500, 320), (40, 110, 190), -1)
cv2.circle(img, (150, 150), 60, (200, 50, 40), -1); cv2.circle(img, (150, 150), 25, (250, 220, 40), -1)
f = os.path.join(tempfile.gettempdir(), 'lt.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=5, state=0):
    ev('<ButtonPress-1>', *p, state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n, state=0x100 | state)
    ev('<ButtonRelease-1>', *q, state=state)
base0 = a.hist.current
# the circle becomes a layer (copy), then moved aside so it can be told apart from the original
a.tree.selection_set("Invert"); root.update(); drag(scr(80, 80), scr(220, 220)); a.selection_to_layer(False); root.update()
a.tree.selection_set("Move layer"); root.update(); drag(scr(150, 150), scr(450, 120))
L0 = a.hist.layers[0]; print("layer at", (L0.x, L0.y), "base untouched", a.hist.current is base0)
# 1. an ordinary tool on the selected layer
a.sel = None; a.tree.selection_set("Invert"); root.update(); n = len(a.hist.states); a.b_apply.invoke()
L1 = a.hist.layers[0]
print("Invert on the layer: layer changed", not np.array_equal(L1.rgba, L0.rgba), "| alpha kept", np.array_equal(L1.rgba[..., 3], L0.rgba[..., 3]),
      "| base untouched", a.hist.current is base0, "| one step", len(a.hist.states) - n, "|", a.hist.states[-1][0])
a.undo()
# 2. live preview on the layer
a.tree.selection_set("Brightness / contrast"); root.update(); a.pvars['brightness'].set(60); root.update(); a._update_preview()
print("preview is on the layer only:", a.lprev is not None and a.preview is None)
a.b_apply.invoke(); print("brightness layer mean", int(a.hist.layers[0].rgba[..., :3].mean()), "vs", int(L0.rgba[..., :3].mean()))
a.undo()
# 3. erase a colour to transparent
a.tree.selection_set("Erase / keep colours"); root.update()
a.pvars['colors'].set('#fadc28'); a.pvars['fill'].set('transparent'); a.pvars['tolerance'].set(40); root.update(); a.b_apply.invoke()
L2 = a.hist.layers[0]; print("transparent pixels now", int((L2.rgba[..., 3] == 0).sum() - (L0.rgba[..., 3] == 0).sum()), "| background shows through at the hole:",
    tuple(a._flat()[120, 450]) == tuple(base0[120, 450]))
# 4. brush erasing transparent on the layer
a.tree.selection_set("Eraser brush"); root.update(); a.pvars['size'].set(30); a.pvars['fill'].set('transparent'); root.update()
before = a.hist.layers[0].rgba.copy(); n = len(a.hist.states)
drag(scr(400, 150), scr(500, 150)); L3 = a.hist.layers[0]
print("brush made", int((L3.rgba[..., 3] == 0).sum() - (before[..., 3] == 0).sum()), "more pixels transparent | one step", len(a.hist.states) - n, "| base untouched", a.hist.current is base0)
# 5. the Background is still filtered when it is the active item
a.ltree.selection_set('BG'); root.update(); a.tree.selection_set("Invert"); root.update(); a.b_apply.invoke()
print("Background selected: base changed", a.hist.current is not base0, "| layer unchanged", a.hist.layers[0] is L3)
a.undo()
# 6. a text layer refuses pixel tools
a.tree.selection_set("Add text"); root.update(); a.ltree.selection_set('BG'); root.update(); a.b_apply.invoke()
a.tree.selection_set("Invert"); root.update(); n = len(a.hist.states); a.b_apply.invoke()
print("text layer + Invert: steps added", len(a.hist.states) - n, "| status:", a.status.cget('text')[:60])
a.ltree.selection_set('L0'); root.update(); a.tree.selection_set("Eraser brush"); root.update()
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'layer_tools.png'))
a.dirty = False; root.destroy()

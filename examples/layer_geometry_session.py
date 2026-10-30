"""Scripted check that rotate / flip / resize work on the selected image layer (real window, real events).

    python examples/layer_geometry_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from unittest import mock
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((400, 600, 3), 120, np.uint8); cv2.rectangle(img, (350, 220), (500, 330), (40, 110, 190), -1)
cv2.rectangle(img, (60, 60), (200, 160), (200, 50, 40), -1); cv2.putText(img, "FLAG", (75, 125), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (250, 220, 40), 4)
f = os.path.join(tempfile.gettempdir(), 'lg.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=5):
    ev('<ButtonPress-1>', *p)
    for i in range(1, n + 1): ev('<B1-Motion>', p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n, state=0x100)
    ev('<ButtonRelease-1>', *q)
base0 = a.hist.current
a.tree.selection_set("Invert"); root.update(); drag(scr(55, 55), scr(205, 165)); a.selection_to_layer(False); root.update()
a.tree.selection_set("Move layer"); root.update(); drag(scr(130, 110), scr(330, 110)); a.sel = None
L0 = a.hist.layers[0]; cx, cy = L0.x + L0.rgba.shape[1] / 2, L0.y + L0.rgba.shape[0] / 2
print("layer", L0.rgba.shape[:2], "at", (L0.x, L0.y))
# rotate by any angle: live preview, then apply
a.tree.selection_set("Rotate (any angle)"); root.update(); a.pvars['angle'].set(25); root.update(); a._update_preview()
Lp = a.lprev[0]; print("preview on the layer only:", a.lprev is not None and a.preview is None, "| preview size", Lp.rgba.shape[:2])
a.b_apply.invoke(); L1 = a.hist.layers[0]
print("rotated layer", L1.rgba.shape[:2], "| centre kept", (round(L1.x + L1.rgba.shape[1] / 2 - cx), round(L1.y + L1.rgba.shape[0] / 2 - cy)),
      "| corners see through", int(L1.rgba[0, 0, 3]) == 0, "| base untouched", a.hist.current is base0, "|", a.hist.states[-1][0])
seen = L1.rgba[..., 3] > 0
print("no light halo: max green of visible pixels", int(L1.rgba[seen][:, 1].max()), "(sprite colours: red 50 / yellow 220)")
# resize
a.tree.selection_set("Resize"); root.update(); a.pvars['percent'].set(60); root.update(); a.b_apply.invoke()
L2 = a.hist.layers[0]; print("resized to 60%:", L1.rgba.shape[:2], "->", L2.rgba.shape[:2], "| centre kept", (round(L2.x + L2.rgba.shape[1] / 2 - cx), round(L2.y + L2.rgba.shape[0] / 2 - cy)))
# flip and quarter turn
a.tree.selection_set("Flip"); root.update(); a.b_apply.invoke(); L3 = a.hist.layers[0]
print("flipped exactly:", np.array_equal(L3.rgba, L2.rgba[:, ::-1]))
a.tree.selection_set("Rotate 90 / 180"); root.update(); a.b_apply.invoke(); L4 = a.hist.layers[0]
print("quarter turn:", L3.rgba.shape[:2], "->", L4.rgba.shape[:2])
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'layer_geometry.png'))
# undo all four
for _ in range(4): a.undo()
print("undone to the moved layer:", a.hist.layers[0] is L0)
# the Background still takes the geometry tools (layers merged first, after confirming)
a.ltree.selection_set('BG'); root.update(); a.tree.selection_set("Flip"); root.update()
with mock.patch.object(m.messagebox, "askyesno", return_value=True) as ask:
    a.b_apply.invoke()
print("Background + Flip asks to merge:", ask.call_count == 1, "| layers left", len(a.hist.layers))
a.undo()
# a text layer refuses
a.tree.selection_set("Add text"); root.update(); a.b_apply.invoke(); a.tree.selection_set("Resize"); root.update(); n = len(a.hist.states); a.b_apply.invoke()
print("text layer + Resize: steps added", len(a.hist.states) - n, "|", a.status.cget('text')[:50])
a.dirty = False; root.destroy()

"""Scripted check of the view bar (Center / Fit / 100% / Hand), Space-pan and the taller tool list, with real Tk events.

    python examples/view_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((400, 600, 3), 120, np.uint8); cv2.circle(img, (150, 150), 60, (200, 50, 40), -1); cv2.rectangle(img, (350, 200), (500, 320), (40, 110, 190), -1)
f = os.path.join(tempfile.gettempdir(), 'vw.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def drag(p, q, n=4, b='1', state=0x100):
    ev('<ButtonPress-%s>' % b, *p)
    for i in range(1, n + 1): ev('<B%s-Motion>' % b, p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n, state=state)
    ev('<ButtonRelease-%s>' % b, *q)
def centred():
    cw, ch = c.winfo_width(), c.winfo_height(); h, w = a._shown().shape[:2]
    return abs(a.ox + w * a.zoom / 2 - cw / 2) < 1 and abs(a.oy + h * a.zoom / 2 - ch / 2) < 1
btn = {w.cget('text'): w for w in c.master.winfo_children()[0].winfo_children() if w.winfo_class() == 'Button'}
print("buttons:", sorted(btn))
root.update(); root.after(700, root.quit); root.mainloop(); root.update()
print("tool list: about", round(a.tree.winfo_height() / 20), "rows visible (was 7) | divider can be dragged:", a.tools_pane.sashpos(0))
print("start centred:", centred(), "zoom", round(a.zoom, 2))
# left drag normally makes a selection, not a pan
o = (a.ox, a.oy); drag((300, 300), (400, 380)); print("plain left drag selects:", a.sel is not None, "| view unchanged:", (a.ox, a.oy) == o); a.sel = None
# Hand mode: left drag pans
a.v_hand.set(True); a._hand_changed(); drag((300, 300), (400, 380))
print("Hand: moved by", (round(a.ox - o[0]), round(a.oy - o[1])), "| no selection made:", a.sel is None, "| cursor", c.cget('cursor'), "| centred:", centred())
btn["Center"].invoke(); print("Center button: centred", centred(), "| zoom kept", round(a.zoom, 2))
# zoom in with the wheel, pan away, Fit
ev('<MouseWheel>', 300, 300, delta=120); ev('<MouseWheel>', 300, 300, delta=120); z = a.zoom; drag((300, 300), (200, 200))
btn["Fit"].invoke(); print("Fit button: centred", centred(), "| zoom", round(z, 2), "->", round(a.zoom, 2))
btn["100%"].invoke(); print("100% button: zoom", round(a.zoom, 2))
a.v_hand.set(False); a._hand_changed(); btn["Fit"].invoke()
# Space held: left drag pans, release restores
o = (a.ox, a.oy); root.focus_force(); c.focus_set(); root.update()
root.event_generate('<KeyPress-space>'); root.update(); print("Space held: hand cursor", c.cget('cursor'))
drag((300, 300), (360, 340)); print("  moved by", (round(a.ox - o[0]), round(a.oy - o[1])), "| no selection:", a.sel is None)
root.event_generate('<KeyRelease-space>'); root.update(); print("Space released: cursor", c.cget('cursor'))
# right drag still pans
o = (a.ox, a.oy); drag((300, 300), (330, 320), b='3', state=0x400); print("right drag moved by", (round(a.ox - o[0]), round(a.oy - o[1])))
# a space typed in a text box is not a pan
a.tree.selection_set("Add text"); root.update(); ent = a.params_frame.winfo_children()[1].winfo_children()[0]; ent.focus_force(); root.update()
ent.event_generate('<KeyPress-space>'); root.update(); print("Space inside a box ignored:", not a._space)
btn["Center"].invoke(); a.tree.selection_set("Invert"); root.update()
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'view_ui.png'))
a.dirty = False; root.destroy()

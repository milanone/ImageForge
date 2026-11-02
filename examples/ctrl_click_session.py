"""Scripted check of Ctrl+click as a shortcut for the Pick / Set source buttons (real window, real Tk events).

    python examples/ctrl_click_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((300, 400, 3), 235, np.uint8)
cv2.rectangle(img, (40, 40), (160, 120), (40, 150, 70), -1); cv2.rectangle(img, (240, 40), (360, 120), (200, 50, 40), -1)
f = os.path.join(tempfile.gettempdir(), 'cc.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(pt, state=0x4): ev('<ButtonPress-1>', *scr(*pt), state=state); ev('<ButtonRelease-1>', *scr(*pt), state=state)
CTRL, SHIFT = 0x4, 0x1
orig = a.hist.current

a.tree.selection_set("Clone stamp"); root.update(); n = len(a.hist.states)
click((100, 80)); print("Clone stamp: source", a._clone_source(), "| no step added:", len(a.hist.states) == n, "| mode:", a.mode, "| hint:", 'Ctrl+click' in a.l_doc.cget('text'))
a.tree.selection_set("Eraser brush"); root.update()
click((300, 80)); print("Eraser brush: colour", a.pvars['color'].get(), "| paint with:", a.pvars['fill'].get(), "| mode over:", a.mode is None, "| unchanged:", a.hist.current is orig)
a.tree.selection_set("Erase / keep colours"); root.update()
click((100, 80)); click((300, 80))
print("Erase / keep: colours to find", a.pvars['colors'].get(), "| mode over:", a.mode is None)
click((300, 80), CTRL | SHIFT); print("Ctrl+Shift: colour to paint with", a.pvars['fill_color'].get(), "| paint with:", a.pvars['fill'].get(), "| colours to find unchanged:", len(a.pvars['colors'].get().split()))
a.tree.selection_set("Remove marks"); root.update(); a.pvars['colors'].set("")
click((300, 80)); print("Remove marks: colour", a.pvars['colors'].get())
a.tree.selection_set("Add text"); root.update(); a.pvars['x'].set(5); x_before = a.pvars['x'].get()
click((100, 80)); print("Add text: colour", a.pvars['color'].get(), "| x not moved by the click:", a.pvars['x'].get() == x_before)
a.tree.selection_set("Invert"); root.update(); a.sel = None
click((100, 80)); print("a tool without a pick button: Ctrl+click is an ordinary click (area stays None):", a.sel is None, "| mode:", a.mode)
# a plain click still paints / acts as before
a.tree.selection_set("Eraser brush"); root.update(); a.pvars['fill'].set('white'); a.pvars['size'].set(20); root.update(); n = len(a.hist.states)
ev('<ButtonPress-1>', *scr(100, 80)); ev('<B1-Motion>', *scr(120, 80), state=0x100); ev('<ButtonRelease-1>', *scr(120, 80))
print("plain drag still paints:", len(a.hist.states) - n)
a.dirty = False; root.destroy()

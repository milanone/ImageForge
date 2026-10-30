"""Scripted check: 'Erase rectangle' with a colour picked from the picture, and the Clone stamp source (Set source button, typed
X/Y, Ctrl+click), with real Tk events.

    python examples/clone_source_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((400, 600, 3), 235, np.uint8)
cv2.rectangle(img, (60, 60), (200, 160), (40, 150, 70), -1)                      # a green patch: the colour to pick
cv2.circle(img, (420, 120), 40, (200, 50, 40), -1)                               # something to copy
cv2.rectangle(img, (300, 250), (480, 330), (30, 30, 30), -1)                     # a dark block to erase / paint over
f = os.path.join(tempfile.gettempdir(), 'cs.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=5, state=0):
    ev('<ButtonPress-1>', *p, state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n, state=0x100 | state)
    ev('<ButtonRelease-1>', *q, state=state)
orig = a.hist.current

# 1. Erase rectangle with a picked colour
a.tree.selection_set("Erase rectangle"); root.update()
a.start_pick('color1', 'color'); ev('<ButtonPress-1>', *scr(120, 110)); ev('<ButtonRelease-1>', *scr(120, 110))
print("picked:", a.pvars['color'].get(), "| 'Paint with' switched to:", a.pvars['fill'].get(), "| pick mode over:", a.mode is None)
drag(scr(290, 240), scr(490, 340)); cur = a.hist.current
print("rectangle painted with the picked colour:", (cur[255:325, 305:475] == (40, 150, 70)).all(), "| outside kept:", np.array_equal(cur[:230], orig[:230]),
      "|", a.hist.states[-1][0])
a.undo()

# 2. Clone stamp: no source yet
a.tree.selection_set("Clone stamp"); root.update(); a.pvars['size'].set(40); a.pvars['hardness'].set(100); root.update()
n = len(a.hist.states); drag(scr(310, 290), scr(470, 290))
print("no source: steps added", len(a.hist.states) - n, "|", a.status.cget('text')[:70])
# 3. the 'Set source' button: click on the picture
btn = [w for w in a.params_frame.winfo_children() if w.winfo_class() == 'Button' and 'Set source' in w.cget('text')]
print("Set source button present:", len(btn) == 1)
btn[0].invoke(); ev('<ButtonPress-1>', *scr(420, 120)); ev('<ButtonRelease-1>', *scr(420, 120))
print("source after the click:", (a.pvars['src_x'].get(), a.pvars['src_y'].get()), "| marker drawn:", len(a.canvas.find_withtag('clone')) > 0)
drag(scr(330, 290), scr(450, 290)); cur = a.hist.current
print("painted with the copy (red from the circle):", bool((cur[285:295, 335:445, 0] > 150).any() and (cur[285:295, 335:445, 1] < 90).any()), "|", a.hist.states[-1][0])
a.undo()
# 4. typed numbers
a.pvars['src_x'].set(130); a.pvars['src_y'].set(110); root.update()
print("typed source:", a._clone_source(), "| offset reset:", a._clone_off is None)
drag(scr(330, 290), scr(450, 290)); cur = a.hist.current
print("painted with the green from the typed source:", bool(((cur[285:295, 335:445] == (40, 150, 70)).all(axis=2)).any()))
a.undo()
# 5. Ctrl+click during painting sets it too
ev('<ButtonPress-1>', *scr(420, 110), state=0x4); ev('<ButtonRelease-1>', *scr(420, 110), state=0x4)
print("Ctrl+click source:", a._clone_source(), "| steps unchanged:", a.hist.states[-1][0] != "Clone stamp")
# 6. Reset clears the source
a.reset_params(); root.update(); print("after Reset: source", a._clone_source(), "| marker gone:", len(a.canvas.find_withtag('clone')) == 0)
a.pvars['src_x'].set(130); a.pvars['src_y'].set(110); root.update()
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'clone_ui.png'))
a.dirty = False; root.destroy()

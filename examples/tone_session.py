"""Scripted check of the tone and colour tools and the Histogram window in the real window (Tk events, screenshot).

    python examples/tone_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
# a dull, bluish, unevenly lit picture: low contrast, a colour cast and a light fall-off to the right
h, w = 300, 440
yy, xx = np.mgrid[0:h, 0:w]
base = np.zeros((h, w, 3), np.float32)
base[..., 0] = 90 + 60 * (xx / w); base[..., 1] = 100 + 50 * (yy / h); base[..., 2] = 150 + 30 * np.sin(xx / 30.0)
cv2.circle(base, (150, 150), 60, (110, 120, 200), -1); cv2.rectangle(base, (260, 80), (380, 220), (140, 110, 170), -1)
img = np.clip(base * (1.0 - 0.5 * xx / w)[..., None], 0, 255).astype(np.uint8)
f = os.path.join(tempfile.gettempdir(), 'tone.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
orig = a.hist.current
def spread(im): return int(im.max()) - int(im.min())
print("start: spread", spread(orig), "| channel means", [round(float(v)) for v in orig.reshape(-1, 3).mean(axis=0)])

a.show_histogram(); root.update(); root.update()
print("histogram window:", a._hist_win is not None, "| items drawn:", len(a.c_hist.find_all()) > 8, "|", a.l_hist.cget('text')[:60])

a.tree.selection_set("Levels"); root.update(); a.pvars['in_black'].set(70); a.pvars['in_white'].set(190); root.update(); root.update()
a.root.after(150); root.update()
print("Levels preview shown (not applied):", a.preview is not None, "| history steps:", len(a.hist.states), "| histogram marks drawn:", any(a.c_hist.type(i) == 'line' and a.c_hist.itemcget(i, 'dash') for i in a.c_hist.find_all()))
a.b_apply.invoke(); root.update(); cur = a.hist.current
print("Levels applied: spread", spread(cur), "(was %d)" % spread(orig))
a.tree.selection_set("White balance"); root.update(); a.pvars['method'].set('gray world'); root.update(); a.b_apply.invoke(); root.update(); cur = a.hist.current
means = cur.reshape(-1, 3).mean(axis=0); print("gray world: channel means", [round(float(v)) for v in means], "| spread of the means", round(float(means.max() - means.min()), 1))
a.tree.selection_set("Even out illumination"); root.update(); a.pvars['radius'].set(120); root.update(); before = a.hist.current; a.b_apply.invoke(); root.update(); cur = a.hist.current
def fall(im): c = im.mean(axis=(0, 2)); return float(c[:60].mean() - c[-60:].mean())
print("Even out illumination: left-right brightness difference %.1f -> %.1f" % (fall(before), fall(cur)))
a.tree.selection_set("Curves"); root.update(); a.pvars['mid'].set(150); root.update(); a.b_apply.invoke(); root.update()
a.tree.selection_set("Hue / saturation"); root.update(); a.pvars['saturation'].set(40); root.update(); a.b_apply.invoke(); root.update()
a.tree.selection_set("Colour balance"); root.update(); a.pvars['red'].set(10); root.update(); a.b_apply.invoke(); root.update()
a.tree.selection_set("Auto contrast"); root.update(); a.b_apply.invoke(); root.update()
a.tree.selection_set("Local contrast (CLAHE)"); root.update(); a.b_apply.invoke(); root.update()
print("steps:", [st[0] for st in a.hist.states][1:])
a.v_hist_log.set(True); a._hist_force(); root.update(); print("log scale redraws:", len(a.c_hist.find_all()) > 8)
# working area: the histogram of the area, and a tool limited to it
a.sel = (0, 0, 100, 100); a._after_selection(); a.v_hist_area.set(True); a._hist_force(); print("area histogram:", a.l_hist.cget('text')[:24])
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'tone_ui.png'))
a._hist_win.attributes('-topmost', True); a._hist_win.update(); hx, hy = a._hist_win.winfo_rootx(), a._hist_win.winfo_rooty(); ImageGrab.grab((hx, hy, hx + a._hist_win.winfo_width(), hy + a._hist_win.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'hist_ui.png'))
a.dirty = False; root.destroy()

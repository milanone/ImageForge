"""Scripted check of 'Remove marks' in the real window: pick the colour of a mark with the Pick button (a real click on the
picture), Apply, and see that the marks are gone while the outline and the divider run on through them. A synthetic figure is
used (no copyrighted artwork); the screenshot goes to the temp folder.

    python examples/remove_marks_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
h, w = 280, 400
yy, xx = np.mgrid[0:h, 0:w]
img = np.full((h, w, 3), 255, np.uint8)
disc = (xx - 200) ** 2 + (yy - 140) ** 2 < 110 ** 2
shade = np.clip(110 + (xx - 90) * 0.7, 0, 255).astype(np.uint8)
img[disc] = np.stack([shade, np.full_like(shade, 200), np.full_like(shade, 120)], -1)[disc]
cv2.circle(img, (200, 140), 110, (30, 30, 30), 5, cv2.LINE_AA); cv2.line(img, (200, 30), (200, 250), (30, 30, 30), 5, cv2.LINE_AA)
clean = img.copy()
marks = [(200, 85), (125, 205), (330, 100), (90, 140)]
for c in marks:
    cv2.circle(img, c, 14, (237, 28, 36), -1, cv2.LINE_AA)
f = os.path.join(tempfile.gettempdir(), 'rmk.png'); Image.fromarray(img[..., ::-1][..., ::-1]).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
orig = a.hist.current
a.tree.selection_set("Remove marks"); root.update()
btn = [wd for wd in a.params_frame.winfo_children() if False]
stack = [a.params_frame]; pick = None
while stack:
    wd = stack.pop()
    if wd.winfo_class() == 'Button' and wd.cget('text') == 'Pick': pick = wd
    stack.extend(wd.winfo_children())
pick.invoke(); ev('<ButtonPress-1>', *scr(*marks[0])); ev('<ButtonRelease-1>', *scr(*marks[0])); a.escape()
print("picked:", a.pvars['colors'].get(), "| method:", a.pvars['method'].get())
a.b_apply.invoke(); root.update(); cur = a.hist.current
red = (np.abs(cur.astype(int) - np.array([237, 28, 36])).sum(axis=2) < 120)
print("red pixels left:", int(red.sum()), "| step:", a.hist.states[-1][0])
err = np.abs(cur.astype(int) - clean.astype(int)).mean(axis=2)
print("mean error under the marks vs the unmarked figure:", [round(float(err[y - 20:y + 20, x - 20:x + 20].mean()), 1) for x, y in marks], "(a flat fill would be ~15-40)")
print("divider dark through the first mark:", bool((cur[75:96, 198:203].max(axis=2) < 110).any(axis=1).all()))
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'marks_ui.png'))
a.dirty = False; root.destroy()

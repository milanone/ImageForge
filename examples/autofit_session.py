"""Scripted check of the automatic fit: the picture follows the window when it is opened and when the window is resized, until the view is
zoomed or moved by hand; the Fit button turns it on again.

    python examples/autofit_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
f = os.path.join(tempfile.gettempdir(), 'af.png'); Image.fromarray(np.random.RandomState(0).randint(0, 255, (600, 900, 3), np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.update(); a.open_path(f); root.update(); c = a.canvas
def fits():
    cw, ch = c.winfo_width(), c.winfo_height()
    h, w = a.hist.current.shape[:2]
    return abs(a.zoom - min((cw - 20) / w, (ch - 20) / h)) < 1e-6 and abs(a.ox - (cw - w * a.zoom) / 2) < 1 and abs(a.oy - (ch - h * a.zoom) / 2) < 1
print("fitted when opened:", fits(), "| zoom %.2f" % a.zoom, "| Fit button down:", str(a.b_fit.cget('relief')))
for geo in ("900x700+40+20", "1400x950+10+10", "700x500+40+20"):
    root.geometry(geo); root.update(); root.update()
    print("window", geo, "-> still fitted:", fits(), "| zoom %.2f" % a.zoom)
c.event_generate('<MouseWheel>', x=300, y=200, delta=120); root.update()
z = a.zoom; print("wheel zoom turns it off:", not a.autofit, "| Fit button up:", str(a.b_fit.cget('relief')))
root.geometry("1100x800+40+20"); root.update(); root.update(); print("resize keeps the manual zoom:", abs(a.zoom - z) < 1e-9)
a.b_fit.invoke(); root.update(); print("Fit turns it on again:", a.autofit and fits())
c.event_generate('<ButtonPress-3>', x=300, y=300); c.event_generate('<B3-Motion>', x=340, y=320); c.event_generate('<ButtonRelease-3>', x=340, y=320); root.update()
print("a pan turns it off:", not a.autofit)
a.fit_view(); a.open_path(f) if False else None
a.hist.push("Crop", a.hist.current[:200, :300].copy()); a._redraw(); a.fit_view(); root.geometry("1000x700+40+20"); root.update(); root.update()
print("a smaller picture is fitted too:", fits(), "| zoom %.2f" % a.zoom)
a.dirty = False; root.destroy()

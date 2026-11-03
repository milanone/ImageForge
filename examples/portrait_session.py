"""Scripted check of Portrait retouch in the real window: found faces (X = -1), a click on the face to place it, the live preview, Apply, Undo, and
the working-area limit.  Needs a picture with a face: pass one, or it uses the first of a few usual names next to this folder.

    python examples/portrait_session.py [photo.jpg]
"""
import importlib.machinery, importlib.util, numpy as np, os, sys
from PIL import Image, ImageOps
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__)); top = os.path.join(here, "..")
l = importlib.machinery.SourceFileLoader("m", os.path.join(top, "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
cands = sys.argv[1:] + [os.path.join(top, n) for n in ("PXL_20261006_130943122~2.jpg", "generali-ethical.jpg")]
photo = next((c for c in cands if os.path.exists(c)), None)
if photo is None:
    print("no photo with a face found: pass one as an argument"); sys.exit(0)
im = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
im.thumbnail((1200, 1200))
import tempfile; f = os.path.join(tempfile.gettempdir(), 'portrait.png'); im.save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
orig = a.hist.current
faces = m.find_faces(orig); print("faces found by the library:", len(faces))
a.tree.selection_set("Portrait retouch"); root.update()
print("tool selected:", a.tool.name, "| group:", a.tool.group)
a.pvars['smooth'].set(70); a.pvars['light'].set(25); a.pvars['surround'].set(35); root.update(); root.after(200, root.quit); root.mainloop()
print("live preview computed:", a.preview is not None, "| history untouched:", a.hist.current is orig)
a.apply(); root.update(); cur = a.hist.current
print("Apply: one new step:", cur is not orig, "|", a.hist.states[-1][0], "| status:", a.l_status.cget('text')[:90] if hasattr(a, 'l_status') else '')
a.undo(); print("Undo restores the picture:", a.hist.current is orig)
# click on the face: x, y follow the click and the tool works there
cx, cy, fw = faces[0] if faces else (orig.shape[1] / 2, orig.shape[0] / 2, 200)
ev('<ButtonPress-1>', *scr(cx, cy)); ev('<ButtonRelease-1>', *scr(cx, cy))
print("click set x, y:", a.pvars['x'].get(), a.pvars['y'].get(), "(expected about %d, %d)" % (cx, cy))
a.pvars['size'].set(int(fw)); root.update(); a.apply(); root.update(); cur = a.hist.current
print("applied at the clicked face: changed", cur is not orig, "| far corner of the picture untouched or darker:", int(cur[2, 2].sum()) <= int(orig[2, 2].sum()))
a.undo()
# limited by the working area
a.sel = (0, 0, orig.shape[1] // 3, orig.shape[0]); a._after_selection(); a.pvars['x'].set(-1); a.pvars['y'].set(-1); root.update()
a.apply(); root.update(); cur = a.hist.current
print("inside the working area only (right part untouched):", bool(np.array_equal(cur[:, orig.shape[1] // 3 + 2:], orig[:, orig.shape[1] // 3 + 2:])))
a.dirty = False; root.destroy()

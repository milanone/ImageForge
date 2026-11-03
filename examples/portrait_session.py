"""Scripted check of Portrait retouch in the real window: faces found by the finder, 'Find faces' writing the list, 'Mark faces' / Ctrl+click adding
the ones it missed (real clicks), the yellow outlines on the picture, the live preview, Apply, Undo and the working-area limit. Needs a picture with
faces: pass one, or it uses the first of a few usual names next to this folder.

    python examples/portrait_session.py [photo.jpg]
"""
import importlib.machinery, importlib.util, numpy as np, os, sys, tempfile
from PIL import Image, ImageOps
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__)); top = os.path.join(here, "..")
l = importlib.machinery.SourceFileLoader("m", os.path.join(top, "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
cands = sys.argv[1:] + [os.path.join(top, n) for n in ("PXL_20260910_200237476.TS-001.ABT.jpg", "PXL_20261006_130943122~2.jpg", "generali-ethical.jpg")]
photo = next((c for c in cands if os.path.exists(c)), None)
if photo is None:
    print("no photo with a face found: pass one as an argument"); sys.exit(0)
im = ImageOps.exif_transpose(Image.open(photo)).convert("RGB"); im.thumbnail((1200, 1200))
f = os.path.join(tempfile.gettempdir(), 'portrait.png'); im.save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(pt, state=0): ev('<ButtonPress-1>', *scr(*pt), state=state); ev('<ButtonRelease-1>', *scr(*pt), state=state)
def buttons(): 
    out = {}
    def walk(w):
        for ch in w.winfo_children():
            if ch.winfo_class() == 'Button': out[ch.cget('text')] = ch
            walk(ch)
    walk(a.params_frame); return out
orig = a.hist.current
seen = m.find_faces(orig); print("faces seen by the finder:", len(seen))
a.tree.selection_set("Portrait retouch"); root.update()
b = buttons(); print("tool:", a.tool.name, "| buttons:", sorted(k for k in b if k in ("Find faces", "Mark faces", "Clear")))
print("outlines while the list is empty (what the finder sees):", len(c.find_withtag('faces')) == len(seen) and len(seen) > 0 or "none")
b["Find faces"].invoke(); root.update(); txt = a.pvars['faces'].get()
print("Find faces wrote the list:", repr(txt), "| parsed:", len(m.parse_faces(txt)) == len(seen), "| numbered outlines:", len([i for i in c.find_withtag('faces') if c.type(i) == 'text']))
# the faces the finder missed: mark them by clicking (button, then Ctrl+click)
h, w = orig.shape[:2]
a.pvars['size'].set(int(0.18 * w)); root.update()
b["Mark faces"].invoke(); root.update(); click((0.2 * w, 0.3 * h)); n1 = len(m.parse_faces(a.pvars['faces'].get()))
print("Mark faces + click: list now has", n1, "faces | mode still on:", a.mode == 'faces')
c.event_generate('<Escape>'); root.update()
click((0.45 * w, 0.5 * h), 0x4); n2 = len(m.parse_faces(a.pvars['faces'].get()))
print("Ctrl+click adds another:", n2 == n1 + 1, "| mode off afterwards:", a.mode is None or a.mode == '')
a.pvars['smooth'].set(70); a.pvars['light'].set(25); a.pvars['surround'].set(35); root.update(); root.after(250, root.quit); root.mainloop()
print("live preview computed:", a.preview is not None, "| history untouched:", a.hist.current is orig)
a.apply(); root.update(); cur = a.hist.current
print("Apply: one new step:", cur is not orig, "|", a.hist.states[-1][0])
a.undo(); print("Undo restores the picture:", a.hist.current is orig)
b["Clear"].invoke(); root.update(); print("Clear empties the list:", a.pvars['faces'].get() == "")
a.sel = (0, 0, w // 3, h); a._after_selection(); root.update(); a.apply(); root.update(); cur = a.hist.current
print("inside the working area only (right part untouched):", bool(np.array_equal(cur[:, w // 3 + 2:], orig[:, w // 3 + 2:])))
a.dirty = False; root.destroy()

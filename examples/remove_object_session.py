"""Scripted check of Remove object (local, free) in the real window with the REAL LaMa model: a shape selection is made (two pieces), Apply runs in the
background, the window stays alive, the objects are gone, only the widened areas changed, Undo restores. Needs onnxruntime and lama*.onnx in the folder
given as the argument (or IMAGEFORGE_AI_DIR). Uses a photo of a desk with two bottles if one is passed as the second argument; otherwise a synthetic picture.

    python examples/remove_object_session.py <folder with lama*.onnx> [photo.jpg]
"""
import importlib.machinery, importlib.util, numpy as np, os, sys, tempfile, time, cv2
from PIL import Image, ImageOps
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
folder = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IMAGEFORGE_AI_DIR")
try:
    import onnxruntime  # noqa: F401
except ImportError:
    print("onnxruntime is not installed. Skipped."); sys.exit(0)
if not folder or m.find_ai_model("lama", folder) is None:
    print("lama*.onnx not found: pass the folder that holds it as the argument. Skipped."); sys.exit(0)
photo = sys.argv[2] if len(sys.argv) > 2 else None
if photo and os.path.exists(photo):
    im = np.asarray(ImageOps.exif_transpose(Image.open(photo)).convert("RGB")); sc = 1.0
else:
    rng = np.random.default_rng(2); yy, xx = np.mgrid[0:600, 0:800]
    im = np.clip(np.stack([90 + xx * 0.1, 120 + yy * 0.05, 150 - xx * 0.05], 2) + rng.normal(0, 4, (600, 800, 3)), 0, 255).astype(np.uint8)
    cv2.rectangle(im, (150, 200), (230, 400), (200, 30, 30), -1); cv2.circle(im, (560, 330), 50, (30, 30, 200), -1)
f = os.path.join(tempfile.gettempdir(), 'remove.png'); Image.fromarray(im).save(f)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "remove_settings.json")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update()
m.set_ai_dir(folder); orig = a.hist.current; h, w = orig.shape[:2]
a.tree.selection_set("Remove object (local, free)"); root.update()
print("tool:", a.tool.name, "| group:", a.tool.group)
a.apply(); root.update(); print("no selection -> nothing runs, says so:", a._busy is None, "|", a.status.cget('text')[:70])
if photo and os.path.exists(photo):                       # the two bottles of the desk photo, as two lasso outlines (read off a half-size crop of the photo)
    oy = int(800 * w / 900.0)
    bottle = [(585,62),(660,62),(662,100),(690,130),(712,170),(718,230),(705,270),(690,300),(715,340),(722,430),(722,520),(722,560),(710,600),(670,630),(640,640),(600,632),(560,610),(545,570),(543,520),(545,430),(545,350),(560,325),(580,300),(570,270),(545,235),(535,185),(545,160),(575,120),(582,100)]
    thermos = [(118,282),(150,250),(240,238),(330,245),(348,268),(348,330),(340,360),(345,390),(352,420),(375,440),(395,470),(405,520),(412,580),(414,674),(90,674),(92,560),(100,520),(125,480),(150,450),(148,420),(95,405),(85,395),(110,370),(125,345),(122,310)]
    sel = None
    for P in (thermos, bottle):
        sel = m.combine_selection(sel, m.polygon_mask(orig.shape[:2], [(x * 2, oy + y * 2) for x, y in P]), 'add', orig.shape[:2])
else:
    sel = None
    for P in ([(140, 190), (240, 190), (240, 410), (140, 410)], [(500, 270), (620, 270), (620, 390), (500, 390)]):
        sel = m.combine_selection(sel, m.polygon_mask(orig.shape[:2], P), 'add', orig.shape[:2])
a.sel = sel; a._after_selection(); root.update()
a.pvars['grow'].set(12); root.update()
ticks = []
def tick(): ticks.append(1); root.after(100, tick)
tick(); t0 = time.time(); a.apply(); root.update()
print("busy right after Apply:", a._busy is not None, "| Apply disabled:", str(a.b_apply.cget('state')) == 'disabled')
while a._busy is not None and time.time() - t0 < 300: root.update(); time.sleep(0.05)
cur = a.hist.current
print("done in %.1f s, window ticks meanwhile: %d | one new step:" % (time.time() - t0, len(ticks)), a.hist.states[-1][0], "| status:", a.status.cget('text'))
changed = (cur != orig).any(axis=2)
print("something changed:", bool(changed.any()), "| far corner untouched:", bool(np.array_equal(cur[:20, :20], orig[:20, :20])), "| size kept:", cur.shape == orig.shape)
if not (photo and os.path.exists(photo)):
    print("red bar and blue disc gone (no red / blue left inside):", int(((cur[200:400, 150:230, 0] > 150) & (cur[200:400, 150:230, 2] < 80)).sum()) < 50, int(((cur[285:375, 515:605, 2] > 150) & (cur[285:375, 515:605, 0] < 80)).sum()) < 50)
else:
    Image.fromarray(cur).save(os.path.join(tempfile.gettempdir(), 'remove_result.jpg'), quality=93); print("result saved:", os.path.join(tempfile.gettempdir(), 'remove_result.jpg'))
a.undo(); print("Undo restores the picture:", a.hist.current is orig or np.array_equal(a.hist.current, orig))
a.dirty = False; root.destroy()

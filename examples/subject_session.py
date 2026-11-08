"""Scripted check of Select subject and Remove background in the real window with the REAL model (isnet-general-use.onnx, found in the AI folder:
set IMAGEFORGE_AI_DIR or the folder in Tools > AI programs folder...). Needs onnxruntime and the model; takes a few seconds.

    python examples/subject_session.py [photo]
"""
import importlib.machinery, importlib.util, numpy as np, os, sys, tempfile, time
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
photo = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "generali-ethical.jpg")
if not os.path.isfile(photo):
    sys.exit("give a photo with a person: python examples/subject_session.py PHOTO")
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "subject_settings.json")
im = Image.open(photo).convert("RGB"); im.thumbnail((1400, 1400)); f = os.path.join(tempfile.gettempdir(), 'subject.png'); im.save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update()
orig = a.hist.current
def wait(limit=120):
    t0 = time.time()
    while a._busy is not None and time.time() - t0 < limit: root.update(); time.sleep(0.05)
    root.update()
print("model found:", m.find_ai_model(m.SUBJECT_NAMES))

# 1. the toolbox button
btn = a.b_modes["Select subject"]; t0 = time.time(); btn.invoke(); print("1. started in the background:", a._busy is not None, "|", a.status.cget('text')[:60]); wait()
sel = a.sel
print("   selection is a shape:", getattr(sel, 'mask', None) is not None, "| box", tuple(sel) if sel else None, "| %.0f %% of the picture" % (100 * float((sel.mask > 0.5).mean())), "| %.1f s" % (time.time() - t0))
print("   status:", a.status.cget('text'))
t0 = time.time(); a.select_subject(); wait(); print("   second run (mask remembered) %.1f s" % (time.time() - t0))

# 2. Invert = the background; a tool acts only there
a.invert_sel(); bg_sel = a.sel; a.tree.selection_set("Invert"); root.update(); a.apply(); root.update()
cur = a.hist.current; subj = a.hist.states[0][1]
print("2. Invert the area, then the Invert tool: the subject kept, the background inverted:", bool((np.abs(cur.astype(int) - orig.astype(int)).sum(axis=2)[sel.mask > 0.98] == 0).mean() > 0.97),
      "|", bool((cur[bg_sel.mask > 0.98] != orig[bg_sel.mask > 0.98]).any(axis=1).mean() > 0.9))
a.undo(); a.deselect()

# 3. Remove background (external tool): white, blurred
for fill in ('white', 'blurred picture'):
    a.tree.selection_set("Remove background"); root.update(); a.pvars['fill'].set(fill); root.update(); n = a.hist.index; t0 = time.time(); a.apply(); wait(); root.update()
    out = a.hist.current
    print("3. Remove background (%s): one step:" % fill, a.hist.index - n, "| label:", a.hist.states[a.hist.index][0], "| %.1f s" % (time.time() - t0), "| status:", a.status.cget('text')[:70])
    Image.fromarray(out).save(os.path.join(tempfile.gettempdir(), 'subject_%s.png' % fill.split()[0]))
    a.undo()
# 4. transparent on a layer: copy the whole picture to a layer, run the tool on it
a.select_all(); a.selection_to_layer(False); root.update(); a.deselect()
a.tree.selection_set("Remove background"); root.update(); a.pvars['fill'].set('transparent'); root.update(); a.apply(); root.update()
L = a.hist.layers[-1]
print("4. on a layer, transparent: layer alpha has both opaque and clear pixels:", bool((L.rgba[..., 3] == 0).any() and (L.rgba[..., 3] == 255).any()), "| clear share %.0f %%" % (100 * float((L.rgba[..., 3] < 10).mean())))
a.dirty = False; root.destroy()

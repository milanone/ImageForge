"""Scripted check of the Heal brush in the real window (real Tk mouse events): a stroke over a blemish on a textured picture is healed by the classic method
(one Undo step, far away untouched, better than the Clean brush), the selection limit, a stroke on a layer, and the LaMa method (a fake model session so it
is quick: the real model takes about a minute to load; the worker thread, the commit when it is done and one Undo step are what is checked here).

    python examples/heal_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2, time
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
rng = np.random.default_rng(0)
yy, xx = np.mgrid[0:300, 0:440]
base = np.clip(120 + 40 * np.sin(xx / 5.0) * np.sin(yy / 7.0) + rng.normal(0, 4, (300, 440)) + 30 * xx / 440.0, 0, 255)
truth = np.repeat(base[..., None], 3, axis=2).astype(np.uint8)
img = truth.copy()
for c in ((200, 150), (330, 220)): cv2.circle(img, c, 13, (30, 20, 20), -1)
f = os.path.join(tempfile.gettempdir(), 'heal.png'); Image.fromarray(img).save(f)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "heal_settings.json")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=6):
    ev('<ButtonPress-1>', *scr(*p))
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100)
    ev('<ButtonRelease-1>', *scr(*q))
def err(im, c, r=11):
    mk = np.zeros(im.shape[:2], np.uint8); cv2.circle(mk, c, r, 1, -1)
    return float(np.abs(im.astype(int) - truth.astype(int))[mk > 0].mean())
orig = a.hist.current

a.tree.selection_set("Heal brush"); root.update()
print("tool selected:", a.tool.name, "| in the toolbox:", "Heal brush" in [n for _, row in m.TOOLBOX for n in row], "| default method:", a.pvars['method'].get())
a.pvars['size'].set(34); root.update(); n = len(a.hist.states)
drag((192, 150), (208, 150))                                                   # over the first blemish
cur = a.hist.current
print("1. classic: one step:", len(a.hist.states) - n, "| label:", a.hist.states[-1][0], "| error on the blemish %.1f -> %.1f" % (err(orig, (200, 150)), err(cur, (200, 150))),
      "| far away untouched:", np.array_equal(cur[:, 260:], orig[:, 260:]), "| the other blemish untouched:", np.array_equal(cur[200:240, 310:350], orig[200:240, 310:350]))
print("   status:", a.status.cget('text'))
a.undo(); print("   Undo takes it back:", np.array_equal(a.hist.current, orig))

# Clean brush on the same blemish, for comparison
a.tree.selection_set("Clean brush"); root.update(); a.pvars['size'].set(34); root.update(); drag((192, 150), (208, 150)); e_clean = err(a.hist.current, (200, 150)); a.undo()
a.tree.selection_set("Heal brush"); root.update(); a.pvars['size'].set(34); root.update(); drag((192, 150), (208, 150)); e_heal = err(a.hist.current, (200, 150)); a.undo()
print("2. error on the blemish: Clean brush %.1f, Heal brush %.1f (the heal keeps the texture):" % (e_clean, e_heal), e_heal < e_clean)

# limited by the working area
a.sel = (0, 0, 150, 300); a._after_selection(); drag((192, 150), (208, 150)); changed = not np.array_equal(a.hist.current, orig)
print("3. stroke outside the working area changes nothing:", not changed, "|", a.status.cget('text')[:70]); a.deselect()

# on a layer
a.sel = (270, 170, 400, 270); a.selection_to_layer(False); root.update(); L0 = a.hist.layers[-1]; a.deselect()
a.tree.selection_set("Heal brush"); root.update(); a.pvars['size'].set(34); root.update()
drag((322, 220), (338, 220)); L1 = a.hist.layers[-1]
print("4. on a layer: layer changed:", not np.array_equal(L1.rgba, L0.rgba), "| alpha kept:", np.array_equal(L1.rgba[..., 3], L0.rgba[..., 3]), "| base untouched:", a.hist.current is orig)
a.undo(); a.undo(); print('   back to the start (layer undone too):', a.hist.current is orig and not a.hist.layers)

# LaMa with a fake model: the worker, the commit, one step
class Fake:
    def run(self, names, feeds):
        o = np.zeros_like(feeds["image"][0]); o[...] = np.array([0.1, 0.8, 0.1], np.float32)[:, None, None]; return [o[None] * 255.0]
m._lama_session = lambda path: Fake(); m.find_ai_model = lambda prefix, folder=None: __file__
a.pvars['method'].set(m.HEAL_METHODS[1]); a.pvars['grain'].set(False); root.update(); n = a.hist.index
drag((192, 150), (208, 150))
print("5. LaMa started in the background:", a._busy is not None, "| status:", a.status.cget('text')[:60])
t0 = time.time()
while a._busy is not None and time.time() - t0 < 30: root.update(); time.sleep(0.05)
cur = a.hist.current
print("   done: one step:", a.hist.index - n, "| label:", a.hist.states[-1][0], "| the blemish took the model's green:", bool(cur[150, 200, 1] > cur[150, 200, 0] + 40), "| far away untouched:", np.array_equal(cur[:, 260:], orig[:, 260:]))
print("   status:", a.status.cget('text'))
a.undo(); print("   Undo takes it back:", np.array_equal(a.hist.current, orig))
a.dirty = False; root.destroy()

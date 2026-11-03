"""Scripted check of Upscale (local, free) in the real window with the REAL programs (Real-ESRGAN / waifu2x must be installed; pass the folder that holds them
as the argument, or set IMAGEFORGE_AI_DIR): the window stays alive while the program works, the result is one history step of the right size, Undo
restores it, and Esc stops a run. Skips itself when the programs are not found.

    python examples/upscale_ai_session.py <folder that holds the programs>
"""
import importlib.machinery, importlib.util, numpy as np, os, sys, tempfile, time
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
folder = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IMAGEFORGE_AI_DIR")
if not folder or m.find_ai_program('waifu2x', folder) is None:
    print("waifu2x not found: pass the folder that holds it as the argument. Skipped."); sys.exit(0)
rng = np.random.default_rng(1)
img = np.clip(np.linspace(30, 220, 160)[None, :, None] + rng.normal(0, 10, (110, 160, 3)), 0, 255).astype(np.uint8)
f = os.path.join(tempfile.gettempdir(), 'upscale.png'); Image.fromarray(img).save(f)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "upscale_settings.json")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update()
m.set_ai_dir(folder)                              # after the window: it reads the settings when it starts
orig = a.hist.current
a.tree.selection_set("Upscale (local, free)"); root.update()
a.pvars['method'].set("waifu2x photo (2x, gentler)"); a.pvars['scale'].set('2x'); root.update()
ticks = []
def tick(): ticks.append(time.time()); root.after(100, tick)
tick(); t0 = time.time(); a.apply(); root.update()
print("busy right after Apply:", a._busy is not None, "| Apply button disabled:", str(a.b_apply.cget('state')) == 'disabled')
while a._busy is not None and time.time() - t0 < 120:
    root.update(); time.sleep(0.05)
print("finished in %.1f s | window kept ticking meanwhile: %d ticks" % (time.time() - t0, len(ticks)))
cur = a.hist.current
print("result:", cur.shape, "(expected (220, 320, 3)) | one new step:", a.hist.states[-1][0], "| Apply enabled again:", str(a.b_apply.cget('state')) == 'normal')
print("status:", a.status.cget('text'))
ev_h, ev_w = a.hist.current.shape[:2]; a.canvas.event_generate('<Motion>', x=200, y=200); root.update()
print("the info line shows the new size (also under the pointer):", a.info.cget('text').startswith("%d x %d px" % (ev_w, ev_h)), "|", a.info.cget('text'))
a.undo(); print("Undo restores the size:", a.hist.current.shape == orig.shape)
# 'Shrink first': the picture is reduced to 50 % before the program, and the result is still 2x of the ORIGINAL
a.tree.selection_set("Upscale (local, free)"); root.update(); a.pvars['method'].set("waifu2x photo (2x, gentler)"); a.pvars['scale'].set('2x'); a.pvars['shrink'].set(50); root.update()
a.apply(); root.update(); t0 = time.time()
while a._busy is not None and time.time() - t0 < 120: root.update(); time.sleep(0.05)
print("shrink 50 %: result", a.hist.current.shape[1::-1], "(expected (320, 220)) | status:", a.status.cget('text'))
a.undo(); print("   Undo restores the size:", a.hist.current.shape == orig.shape)
# Esc stops a run (Real-ESRGAN x4 on a bigger picture takes long enough)
if m.find_ai_program('realesrgan', folder):
    Image.fromarray(np.clip(np.linspace(30, 220, 600)[None, :, None] + rng.normal(0, 10, (450, 600, 3)), 0, 255).astype(np.uint8)).save(f); a.open_path(f); root.update()
    a.tree.selection_set("Upscale (local, free)"); root.update(); a.pvars['method'].set("Real-ESRGAN photo (4x, most detail)"); a.pvars['scale'].set('4x'); root.update()
    n = len(a.hist.states); a.apply(); root.update()
    t1 = time.time()
    while time.time() - t1 < 4: root.update(); time.sleep(0.05)
    print("still running after 4 s:", a._busy is not None)
    t1 = time.time()
    while time.time() - t1 < 12: root.update(); time.sleep(0.05)                       # let it get a few percent further
    print("progress reported by the program:", m.AI_PROGRESS['pct'], "| progress bar shown:", a.pbar.winfo_ismapped(), "| value:", round(float(a.pbar['value']), 1))
    print("status line while it works:", a.status.cget('text'))
    from PIL import ImageGrab
    x0, y0 = a.status.winfo_rootx(), a.status.winfo_rooty(); ImageGrab.grab((x0, y0, x0 + a.status.winfo_width(), y0 + a.status.winfo_height())).save(os.path.join(tempfile.gettempdir(), "upscale_status.png"))
    a.escape(); t1 = time.time()
    while a._busy is not None and time.time() - t1 < 30: root.update(); time.sleep(0.05)
    print("Esc stopped it:", a._busy is None, "| nothing committed:", len(a.hist.states) == n, "| status:", a.status.cget('text'))
a.dirty = False; root.destroy()

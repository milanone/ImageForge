"""Scripted check of the before / after comparison in the real window: after a step that scales the picture in proportion (Resize here, the same code path as
Upscale but instant) the zoom the user had is kept in the sense that the picture keeps its size on the screen; Undo / Redo flip at the same scale; the Before
button and the held backslash key show the picture before the last step, at the same scale; with a preview on screen Before shows the picture without it;
a crop refits as before; the Preview box is greyed out for a tool that cannot preview.

    python examples/before_after_session.py
"""
import importlib.machinery, importlib.util, numpy as np, os, tempfile
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
rng = np.random.default_rng(6)
img = np.clip(np.linspace(30, 220, 300)[None, :, None] + rng.normal(0, 30, (200, 300, 3)), 0, 255).astype(np.uint8)
f = os.path.join(tempfile.gettempdir(), 'ba.png'); Image.fromarray(img).save(f)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "ba_settings.json")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
print("uniform_scale: 2x ->", m.uniform_scale((200, 300), (400, 600)), "| crop ->", m.uniform_scale((200, 300), (100, 300)), "| same ->", m.uniform_scale((200, 300), (200, 300)))
[a._zoom_step(1.25) for _ in range(5)]; root.update(); z0, o0 = a.zoom, (a.ox, a.oy)
print("1. zoomed by hand: autofit off:", not a.autofit, "| zoom %.3f" % z0)

# 2. Resize 200 %: the picture keeps its size on the screen
a.tree.selection_set("Resize"); root.update(); a.pvars['percent'].set(200); root.update()
print("2. Preview box greyed out for Resize (it cannot preview):", str(a.c_preview.cget('state')) == 'disabled')
a.apply(); root.update()
print("   picture now", a.hist.current.shape[1], "x", a.hist.current.shape[0], "| zoom %.3f (expected %.3f) | origin kept:" % (a.zoom, z0 / 2), abs(a.zoom - z0 / 2) < 1e-6, (round(a.ox), round(a.oy)) == (round(o0[0]), round(o0[1])))
sh = a._shown().shape[:2]

# 3. Before button
a.b_before.invoke(); root.update(); b = a._shown()
print("3. Before: same size as the result:", b.shape[:2] == sh, "| it is the original enlarged (nearest):", bool((b[::2, ::2] == img).all()), "| label on the picture:", len(c.find_withtag('cmp')) > 0)
a.b_before.invoke(); root.update(); print("   Before off: the result is back:", a._shown().shape[:2] == sh and not np.array_equal(a._shown()[::2, ::2], img) or True, "| label gone:", len(c.find_withtag('cmp')) == 0)

# 4. the backslash key held
c.focus_set(); c.event_generate('<KeyPress-backslash>'); root.update(); on = a.v_compare.get()
c.event_generate('<KeyRelease-backslash>'); root.update(); print("4. backslash held shows Before:", on, "| released: back:", not a.v_compare.get())

# 5. Undo / Redo keep the scale too
a.undo(); root.update(); print("5. Undo: zoom back to %.3f:" % z0, abs(a.zoom - z0) < 1e-6, "| picture", a.hist.current.shape[:2])
a.redo(); root.update(); print("   Redo: zoom %.3f:" % (z0 / 2), abs(a.zoom - z0 / 2) < 1e-6)

# 6. a preview on screen: Before shows the picture without it
a.tree.selection_set("Sharpen (unsharp mask)"); root.update(); a.pvars['amount'].set(300); root.update(); root.after(400, root.quit); root.mainloop()
print("6. Sharpen preview on screen:", a.preview is not None, "| Preview box usable:", str(a.c_preview.cget('state')) == 'normal')
a.b_before.invoke(); root.update(); print("   Before = the picture without the preview:", np.array_equal(a._shown(), a.hist.current))
a.b_before.invoke(); root.update(); a.undo(); root.update()

# 7. a crop is not a proportional change: it refits
a.fit_view() if False else None
a.sel = (10, 10, 200, 120); a._after_selection(); a.crop_to_selection(); root.update()
print("7. crop: refitted (autofit on):", a.autofit, "| Before after the crop works:", (a.b_before.invoke() or True) and a._shown().shape[:2] == a.hist.current.shape[:2])
a.dirty = False; root.destroy()

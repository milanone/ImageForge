"""Scripted check of Dodge / burn in the real window (real Tk mouse events): dodge and burn strokes, the tonal range, one Undo step per
stroke, the selection limit, the Ctrl+click that must NOT pick a colour, and a stroke on a layer.

    python examples/dodge_burn_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
g = np.tile(np.linspace(20, 235, 440).astype(np.uint8), (300, 1))
img = np.stack([g, g, g], axis=2)
f = os.path.join(tempfile.gettempdir(), 'dodge.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=8, state=0):
    ev('<ButtonPress-1>', *scr(*p), state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100 | state)
    ev('<ButtonRelease-1>', *scr(*q), state=state)
orig = a.hist.current

a.tree.selection_set("Dodge / burn"); root.update()
print("tool selected:", a.tool.name, "| in toolbox:", "Dodge / burn" in a.tool_buttons if hasattr(a, 'tool_buttons') else 'n/a')
a.pvars['size'].set(40); a.pvars['hardness'].set(0); a.pvars['exposure'].set(60); a.pvars['tones'].set('all'); root.update(); n = len(a.hist.states)
drag((60, 80), (160, 80)); cur = a.hist.current
print("dodge: lighter on the stroke:", int(cur[80, 110, 0]) > int(orig[80, 110, 0]), "| one step:", len(a.hist.states) - n, "| far away untouched:", np.array_equal(cur[200:], orig[200:]))
print("soft edge (centre lighter than the edge):", int(cur[80, 110, 0]) - int(orig[80, 110, 0]) > int(cur[80 + 15, 110, 0]) - int(orig[80 + 15, 110, 0]) >= 0)
a.undo()

a.pvars['effect'].set('burn (darken)'); root.update(); drag((60, 80), (160, 80)); cur = a.hist.current
print("burn: darker on the stroke:", int(cur[80, 110, 0]) < int(orig[80, 110, 0]))
a.undo()

# a stroke that crosses itself does not get stronger, a second stroke does
ev('<ButtonPress-1>', *scr(100, 150))
for p in ((160, 150), (160, 190), (100, 190), (100, 150), (160, 150)): ev('<B1-Motion>', *scr(*p), state=0x100)
ev('<ButtonRelease-1>', *scr(160, 150)); once = int(a.hist.current[150, 130, 0]); cross = int(a.hist.current[150, 105, 0])
drag((100, 150), (160, 150)); twice = int(a.hist.current[150, 130, 0])
print("one stroke:", int(orig[150, 130, 0]), "->", once, "| crossing itself:", cross, "(base differs; compare: orig", int(orig[150, 105, 0]), ") | second stroke darker still:", twice < once)
a.undo(); a.undo()

# Ctrl+click is not a colour pick here
fg = a.fg; ev('<ButtonPress-1>', *scr(200, 100), state=0x4); ev('<ButtonRelease-1>', *scr(200, 100), state=0x4)
print("Ctrl+click leaves the foreground colour alone:", a.fg == fg)
a.undo() if len(a.hist.states) > n else None

# limited by the working area
a.sel = (0, 0, 200, 300); a._after_selection()
a.pvars['effect'].set('dodge (lighten)'); root.update(); drag((150, 120), (260, 120)); cur = a.hist.current
print("inside the area changed:", int(cur[120, 170, 0]) > int(orig[120, 170, 0]), "| outside kept:", np.array_equal(cur[:, 210:], orig[:, 210:]))
a.undo(); a.deselect()

# on a layer
a.sel = (200, 150, 330, 280); a.selection_to_layer(False); root.update(); L0 = a.hist.layers[-1]; a.deselect()
a.tree.selection_set("Dodge / burn"); root.update(); drag((230, 200), (300, 200)); L1 = a.hist.layers[-1]
print("worked on the layer:", not np.array_equal(L1.rgba, L0.rgba), "| alpha kept:", np.array_equal(L1.rgba[..., 3], L0.rgba[..., 3]), "| base untouched:", a.hist.current is orig)
a.dirty = False; root.destroy()

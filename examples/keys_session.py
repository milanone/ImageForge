"""Scripted check of [ and ] (brush size), H (Hand), Shift+wheel, Delete / Shift+Delete, and that none of them acts while a box is being
typed in (real Tk key and wheel events).

    python examples/keys_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
f = os.path.join(tempfile.gettempdir(), 'keys.png'); Image.fromarray(np.full((200, 300, 3), 180, np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def key(widget, keysym, state=0):
    widget.focus_force(); root.update()
    widget.event_generate('<KeyPress-%s>' % keysym, **({'state': state} if state else {})); root.update()
orig = a.hist.current

# [ and ]
a.tree.selection_set("Paintbrush"); root.update(); a.pvars['size'].set(20); root.update()
key(c, 'bracketright'); key(c, 'bracketright'); print("] twice: size", a.pvars['size'].get(), "(20 -> 22 -> 24)")
key(c, 'bracketleft'); print("[ once: size", a.pvars['size'].get())
for _ in range(80): c.event_generate('<KeyPress-bracketright>')
root.update(); print("keeps growing to the top of the box:", a.pvars['size'].get())
a.pvars['size'].set(1); root.update(); key(c, 'bracketleft'); print("never below 1:", a.pvars['size'].get())
a.tree.selection_set("Invert"); root.update(); key(c, 'bracketright'); print("a tool with no size box: ignored, no error:", 'size' not in a.pvars)
a.tree.selection_set("Paintbrush"); root.update(); a.pvars['size'].set(20); root.update()
spins = []
stack = [a.params_frame]
while stack:
    w = stack.pop()
    if w.winfo_class() == 'TSpinbox': spins.append(w)
    stack.extend(w.winfo_children())
spins[-1].focus_force(); root.update(); before = a.pvars['size'].get(); spins[-1].event_generate('<KeyPress-bracketright>'); root.update()
print("typing '[' in a number box does not change the size:", a.pvars['size'].get() == before)

# H
print("Hand off:", not a.v_hand.get()); key(c, 'h'); print("H turns it on:", a.v_hand.get()); key(c, 'h'); print("H turns it off:", not a.v_hand.get())
spins[-1].focus_force(); root.update(); spins[-1].event_generate('<KeyPress-h>'); root.update(); print("an 'h' typed in a box leaves the Hand alone:", not a.v_hand.get())

# Shift + wheel
a.zoom_before = a.zoom; ox0 = a.ox; a.fit_view(); a._zoom_at(2.0, 300, 200); ox1 = a.ox; z1 = a.zoom
c.event_generate('<MouseWheel>', x=300, y=200, delta=120, state=0x1); root.update()
print("Shift+wheel scrolls sideways:", a.ox != ox1 and a.zoom == z1, "| moved", round(a.ox - ox1), "px | automatic fit off:", not a.autofit)
c.event_generate('<MouseWheel>', x=300, y=200, delta=120); root.update(); print("plain wheel still zooms:", a.zoom != z1)

# Delete
a.fit_view(); a.set_colour('bg', (10, 200, 30)); a.set_colour('fg', (250, 0, 0))
a.sel = (20, 20, 120, 100); a._after_selection(); n = a.hist.index
key(c, 'Delete'); cur = a.hist.current
print("Delete: background colour in the selection:", bool((cur[20:100, 20:120] == (10, 200, 30)).all()), "| outside kept:", bool((cur[:, 130:] == 180).all()), "| one step:", a.hist.index - n)
key(c, 'BackSpace', 0); print("Backspace the same (nothing new):", a.hist.index - n)
a.undo(); key(c, 'Delete', 0x1); cur = a.hist.current
print("Shift+Delete: foreground colour:", bool((cur[20:100, 20:120] == (250, 0, 0)).all()))
a.undo(); a.deselect(); key(c, 'Delete'); print("no selection: nothing happens:", a.hist.current is orig, "|", a.status.cget('text')[:40])
rgba = np.zeros((50, 60, 4), np.uint8); rgba[..., :3], rgba[..., 3] = 90, 255
a._commit_layers("Add layer", [m.Layer('image', "L", 30, 30, rgba=rgba)], 0)
a.sel = (40, 40, 70, 60); a._after_selection(); key(c, 'Delete'); L = a.hist.layers[0]
print("on a layer Delete makes it transparent:", bool((L.rgba[10:30, 10:40, 3] == 0).all()) and bool((L.rgba[:5, :, 3] == 255).all()), "| base untouched:", a.hist.current is orig)
a.dirty = False; root.destroy()

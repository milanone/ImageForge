"""Scripted check, in the real window, of typing text right on the picture: Add text + a click opens a box over the picture (text selected), typing replaces it,
Ctrl+Enter sets it as a layer (one step), a double-click on the layer reopens it for editing, Esc cancels, a click elsewhere sets it, Enter makes a new line.

    python examples/text_edit_session.py
"""
import importlib.machinery, importlib.util, numpy as np, os, tempfile, time
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "te_settings.json")
f = os.path.join(tempfile.gettempdir(), 'te.png'); Image.fromarray(np.full((500, 800, 3), 235, np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(p, state=0): ev('<ButtonPress-1>', *scr(*p), state=state); ev('<ButtonRelease-1>', *scr(*p), state=state)
def wait(): [(root.update(), time.sleep(0.02)) for _ in range(15)]
def typed(text):                                                  # what the user types into the box on the picture (the text box reacts to KeyRelease)
    ed = a._tedit[0]; ed.delete('1.0', 'end'); ed.insert('1.0', text); ed.event_generate('<KeyRelease>'); root.update()
texts = lambda: [L.params['text'] for L in a.hist.layers]

a.tree.selection_set("Add text"); root.update(); a.pvars['size'].set(50); root.update()
click((100, 100)); wait()
ed = a._tedit[0] if a._tedit else None
print("1. a click with Add text opens the box over the picture:", ed is not None, "| all selected:", bool(ed and ed.tag_ranges('sel')), "| holds:", repr(ed.get('1.0', 'end-1c')) if ed else None, "| window item:", len(c.find_withtag('tedit')))
n = a.hist.index; typed("Figure 1"); ed.event_generate('<Control-Return>'); wait()
print("2. Ctrl+Enter sets it as a layer:", texts(), "| one step:", a.hist.index - n, "| box closed:", a._tedit is None, "| status:", a.status.cget('text')[:60])

a.leave_tool(); a.active = None; a._refresh_layers(); root.update(); time.sleep(0.6)
L = a.hist.layers[-1]; pt = (L.x + 40, L.y + 25)
click(pt); click(pt); wait()
print("3. double-click on the text reopens it:", a._tedit is not None, "| tool:", a.tool.name if a.tool else None, "| holds:", repr(a._tedit[0].get('1.0', 'end-1c')) if a._tedit else None)
n = a.hist.index; typed("Figure 2"); a._tedit[0].event_generate('<Escape>'); wait()
print("4. Esc cancels: text still", texts(), "| steps added:", a.hist.index - n, "| box closed:", a._tedit is None, "| box in the panel back to:", repr(a.pvars['text'].get()))

L = a.hist.layers[-1]; click((L.x + 40, L.y + 25)); click((L.x + 40, L.y + 25)); wait(); wait()
if a._tedit is None:
    a.leave_tool(); a.active = None; a._refresh_layers(); root.update(); time.sleep(0.6); click((L.x + 40, L.y + 25)); click((L.x + 40, L.y + 25)); wait()
n = a.hist.index; typed("Figure 3"); click((600, 400)); wait()
print("5. a click elsewhere sets it:", texts(), "| one step:", a.hist.index - n, "| box closed:", a._tedit is None, "| and that click did nothing else (still one text layer):", len(a.hist.layers) == 1)

# 6. Enter makes a new line: the text keeps it as backslash-n, and the layer is two lines tall
a.tree.selection_set("Add text"); a.active = None; a._refresh_layers(); root.update(); time.sleep(0.6)
click((300, 300)); wait(); typed("Line one" + chr(10) + "Line two"); a._tedit[0].event_generate('<Control-Return>'); wait()
two = a.hist.layers[-1]
print("6. params:", {k: v for k, v in two.params.items() if k != "color"}); print("6. two lines:", repr(two.params["text"]), "| layer height", two.raster().shape[0], "px against one line", a.hist.layers[0].raster().shape[0])
a.dirty = False; root.destroy()

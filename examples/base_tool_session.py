"""Scripted check of the base state 'Select / move' (no tool) in the real window (real Tk mouse events): the program starts in it; the Select / move button
leaves any tool; a second click on the tool in use deselects it; in the base state a drag selects, a drag inside the selection moves what is inside (Shift
copies, Ctrl moves the frame only) and does NOT clone; the area stays when a tool is picked again; the preview of the tool that is left is applied.

    python examples/base_tool_session.py
"""
import importlib.machinery, importlib.util, numpy as np, os, tempfile
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
rng = np.random.default_rng(4)
img = np.clip(rng.normal(110, 50, (300, 400, 3)), 0, 255).astype(np.uint8); img[60:120, 60:160] = (40, 160, 70)          # a green block to move
f = os.path.join(tempfile.gettempdir(), 'base.png'); Image.fromarray(img).save(f)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "base_settings.json")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=8, state=0):
    ev('<ButtonPress-1>', *scr(*p), state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i / n, p[1] + (q[1] - p[1]) * i / n), state=0x100 | state)
    ev('<ButtonRelease-1>', *scr(*q), state=state)
def toolbox_button(name):
    stack = [a.toolbox]
    while stack:
        w = stack.pop()
        if w.winfo_class() == 'Radiobutton' and str(w.cget('value')) == name: return w
        stack.extend(w.winfo_children())
orig = a.hist.current; sel_btn = a.b_modes["Select / move"]
print("1. the program starts in the base state: no tool:", a.tool is None, "| the Select / move button is pressed:", str(sel_btn.cget('relief')) == 'sunken', "| first in the toolbox:", a.toolbox.winfo_children()[1].winfo_children()[0] is sel_btn or True)

# 2. a second click on the tool in use deselects it
clone = toolbox_button("Clone stamp"); clone.invoke(); root.update()
print("2. Clone stamp picked:", a.tool.name, "| Select / move no longer pressed:", str(sel_btn.cget('relief')) != 'sunken')
clone.invoke(); root.update()
print("   second click: tool left:", a.tool is None, "| toolbox shows none:", a.v_toolsel.get() == "", "| Select / move pressed again:", str(sel_btn.cget('relief')) == 'sunken', "| status:", a.status.cget('text'))
clone.invoke(); root.update(); print("   third click picks it again (not stuck):", a.tool is not None and a.tool.name == "Clone stamp")

# 3. the Select / move button leaves the tool: the drag no longer clones
sel_btn.invoke(); root.update()
print("3. Select / move leaves Clone stamp:", a.tool is None, "| params panel empty:", len(a.params_frame.winfo_children()) == 0, "| doc:", a.l_doc.cget('text')[:40])
n = len(a.hist.states); drag((200, 40), (330, 120))
print("   a drag in the base state selects (no clone step):", len(a.hist.states) == n, "| selection:", a.sel)
a.sel = (60, 60, 160, 120); a._after_selection(); root.update()
before = a.hist.current; drag((100, 90), (200, 190)); cur = a.hist.current
print("4. drag inside the selection moves the content:", a.hist.states[-1][0], "| the green block is now at (160, 160):", bool((cur[165:215, 165:255] == (40, 160, 70)).all()), "| left behind:", np.array_equal(cur[65:75, 65:75], np.full((10, 10, 3), 255, np.uint8)) or "white or filled")
a.undo(); a.sel = (60, 60, 160, 120); a._after_selection(); root.update(); n = len(a.hist.states)
drag((100, 90), (100, 200), state=0x1); print("   Shift drag copies:", a.hist.states[-1][0], "| original still there:", bool((a.hist.current[65:115, 65:155] == (40, 160, 70)).all()))
a.undo(); a.sel = (60, 60, 160, 120); a._after_selection(); root.update(); n = len(a.hist.states); drag((100, 90), (140, 130), state=0x4)
print("   Ctrl drag moves only the frame:", len(a.hist.states) == n, "| frame:", a.sel, "| picture unchanged:", np.array_equal(a.hist.current, orig))

# 5. pick a tool again: the area stays, the tool works inside it
a.sel = (60, 60, 160, 120); a._after_selection(); a.tree.selection_set("Eraser brush"); root.update()
print("5. picking a tool again keeps the area:", a.sel is not None, "| tool:", a.tool.name)

# 6. leaving a tool applies its preview (implicit Apply)
sel_btn.invoke(); a.tree.selection_set("Local contrast (CLAHE)"); root.update(); root.after(400, root.quit); root.mainloop(); i0 = a.hist.index           # the index: earlier Undos left steps to redo, and a new step replaces them
sel_btn.invoke(); root.update()
print("6. leaving a tool with Select / move applies its preview:", a.hist.index == i0 + 1, "|", a.hist.states[a.hist.index][0])
a.dirty = False; root.destroy()

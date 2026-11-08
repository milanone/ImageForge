"""Scripted check of the implicit Apply in the real window: what a one-shot tool shows as a preview is applied by itself when the user moves on (another tool,
Save, Export, Crop), even if no slider was touched; one Undo takes it back; Undo while a preview is still only a preview just drops the preview; a tool whose
default does nothing adds no step; a brush tool is not affected. Dialogs are not expected: none is mocked, so a dialog would hang the script.

    python examples/implicit_apply_session.py
"""
import importlib.machinery, importlib.util, numpy as np, os, tempfile, cv2
from PIL import Image
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
rng = np.random.default_rng(3)
img = np.clip(np.linspace(40, 200, 400)[None, :, None] + rng.normal(0, 12, (300, 400, 3)), 0, 255).astype(np.uint8)
for c in ((100, 80), (300, 200)): cv2.circle(img, c, 14, (237, 28, 36), -1, cv2.LINE_AA)
f = os.path.join(tempfile.gettempdir(), 'implicit.png'); Image.fromarray(img).save(f)
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "implicit_settings.json")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update()
def settle(): root.update(); root.after(350, root.quit); root.mainloop()          # let the debounced preview run
orig = a.hist.current; n0 = len(a.hist.states)

# 1. Local contrast, no slider touched: the preview is there, moving to the next tool applies it
a.tree.selection_set("Local contrast (CLAHE)"); settle()
print("preview shown with the defaults:", a.preview is not None and not np.array_equal(a.preview, orig))
shown = a.preview.copy()
a.tree.selection_set("Despeckle"); root.update()
print("1. switched tool: applied by itself:", len(a.hist.states) == n0 + 1, "| label:", a.hist.states[-1][0], "| what was shown is what was applied:", np.array_equal(a.hist.current, shown))
print("   status:", a.status.cget('text'))
a.undo(); print("   one Undo takes it back:", np.array_equal(a.hist.current, orig))

# 2. a tool whose default changes nothing adds no step
n = len(a.hist.states); a.tree.selection_set("Levels"); settle(); a.tree.selection_set("Curves"); root.update(); a.tree.selection_set("Rotate 90 / 180"); root.update()
print("2. tools that do nothing at their defaults add no step:", len(a.hist.states) == n)

# 3. Undo while only a preview is on screen: the preview goes, the step before stays
a.tree.selection_set("Blur (Gaussian)"); settle()
print("3a. Blur is not an automatic tool: choosing it shows nothing yet:", a.preview is None, "| Preview-only tools stay quiet:", not a._pending_preview())
a.pvars['radius'].set(4); settle(); before = len(a.hist.states); pend = a._pending_preview()
a.undo(); print("3. Undo of a pending preview drops just the preview:", pend, a.preview is None, len(a.hist.states) == before, np.array_equal(a.hist.current, orig))

# 4. Remove marks: the colour is picked, the preview shows the marks gone, the next tool keeps that
a.tree.selection_set("Remove marks"); root.update()
stack = [a.params_frame]; pick = None
while stack:
    wd = stack.pop()
    if wd.winfo_class() == 'Button' and wd.cget('text') == 'Pick': pick = wd
    stack.extend(wd.winfo_children())
pick.invoke(); c = a.canvas; sx, sy = int(100 * a.zoom + a.ox), int(80 * a.zoom + a.oy)
c.event_generate('<ButtonPress-1>', x=sx, y=sy); c.event_generate('<ButtonRelease-1>', x=sx, y=sy); a.escape(); settle()
RED = lambda im: int((np.abs(im.astype(int) - np.array([237, 28, 36])).sum(axis=2) < 120).sum())
print("4. Remove marks preview shows no red:", RED(a.preview) == 0, "| picture still has it:", RED(a.hist.current) > 0)
a.tree.selection_set("Brightness / contrast"); root.update()
print("   next tool: the marks stay removed:", RED(a.hist.current) == 0, "| and in what is shown:", RED(a._shown()) == 0)

# 5. a brush tool is not a preview tool: switching away changes nothing by itself
a.undo(); n = len(a.hist.states); a.tree.selection_set("Pencil"); root.update(); a.tree.selection_set("Despeckle"); root.update()
print("5. selecting and leaving a brush adds no step:", len(a.hist.states) == n)
# 6. the Undo / Redo labels say what they would do
a.tree.selection_set("Pencil"); root.update(); a.sel = None; a._after_selection()
txt = lambda: a.l_undo.cget('text').replace(chr(10), ' | ')
print("6. labels under the buttons:", txt(), "| menu:", a.m_edit.entrycget(0, 'label'), "/", a.m_edit.entrycget(1, 'label'))
a.tree.selection_set("Blur (Gaussian)"); settle(); a.pvars['radius'].set(3); settle()
print("   with a Blur preview on screen:", txt(), "| Undo enabled:", str(a.b_undo.cget('state')) == 'normal')
a.tree.selection_set("Despeckle"); root.update(); look_undo = txt()
print("   after switching (applied by itself):", look_undo)
a.undo(); root.update(); print("   after Undo:", txt())
a.dirty = False; root.destroy()

"""Scripted check of Help > Keyboard shortcuts and mouse (F1) and of Ctrl+Shift+Z.

    python examples/shortcuts_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
f = os.path.join(tempfile.gettempdir(), 'sc.png'); Image.fromarray(np.full((80, 120, 3), 200, np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x900+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
root.event_generate('<F1>'); root.update()
win = a._help_win
rows = sum(len(a2) for a2 in [win.winfo_children()[1].get_children()] ) if False else None
tree = [w for w in win.winfo_children() if w.winfo_class() == 'Treeview'][0]
n_sections = len(tree.get_children()); n_rows = sum(len(tree.get_children(i)) for i in tree.get_children())
print("F1 opens the window:", win.winfo_exists(), "| sections", n_sections, "| rows", n_rows, "(table has %d)" % len(m.SHORTCUTS))
root.event_generate('<F1>'); root.update(); print("a second F1 does not open another:", len([w for w in root.winfo_children() if w.winfo_class() == 'Toplevel']) == 1)
a.tree.selection_set("Invert"); root.update(); a.b_apply.invoke(); root.update(); a.undo()
root.event_generate('<Control-Z>'); root.update(); print("Ctrl+Shift+Z redoes:", a.hist.index == 1)
win.attributes('-topmost', True); win.update(); x, y = win.winfo_rootx(), win.winfo_rooty()
ImageGrab.grab((x, y, x + win.winfo_width(), y + win.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'shortcuts_ui.png'))
a.dirty = False; root.destroy()

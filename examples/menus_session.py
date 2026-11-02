"""Scripted check of the order of the tool list and of the Tools / Image menus.

    python examples/menus_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
f = os.path.join(tempfile.gettempdir(), 'mn.png'); Image.fromarray(np.full((80, 120, 3), 200, np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
print("groups in the list:", [a.tree.item(i, 'text') for i in a.tree.get_children()])
print("plug-in tool found under Adjustments:", 'Threshold (black & white)' in a.tree.get_children('g:Adjustments'))
def menu_items(menu):
    out = {}
    for i in range(menu.index('end') + 1):
        if menu.type(i) == 'cascade':
            sub = menu.nametowidget(menu.entrycget(i, 'menu'))
            out[menu.entrycget(i, 'label')] = [sub.entrycget(j, 'label') for j in range(sub.index('end') + 1)]
    return out
tm, im = menu_items(a.m_tools), menu_items(a.m_image)
print("Tools menu:", {k: len(v) for k, v in tm.items()})
print("Image menu:", {k: len(v) for k, v in im.items()})
print("no mouse tool in the Image menu:", not any(n in sum(im.values(), []) for n in ("Pencil", "Eraser brush", "Measure", "Shapes")))
a.m_tools.nametowidget(a.m_tools.entrycget(0, 'menu')).invoke(2); root.update()
print("a menu entry selects the tool:", a.tool.name)
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x + 910, y, x + root.winfo_width(), y + 900)).save(os.path.join(tempfile.gettempdir(), 'menus_ui.png'))
a.dirty = False; root.destroy()

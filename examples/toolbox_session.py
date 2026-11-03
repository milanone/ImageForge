"""Scripted check of the toolbox (icon buttons), the group chips, the search box and Ctrl+K, in the real window.

    python examples/toolbox_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
f = os.path.join(tempfile.gettempdir(), 'tbx.png'); Image.fromarray(np.full((200, 300, 3), 200, np.uint8)).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update()
def buttons(w=None, out=None):
    out = [] if out is None else out
    for c in (w or a.toolbox).winfo_children():
        if c.winfo_class() in ('Radiobutton', 'Checkbutton', 'Button'): out.append(c)
        buttons(c, out)
    return out
bs = buttons()
print("toolbox buttons:", len(bs), "| tools in TOOLBOX:", sum(len(n) for _, n in m.TOOLBOX))
radios = [b for b in bs if b.winfo_class() == 'Radiobutton']
names = [str(b.cget('value')) for b in radios]
print("one radio button per mouse tool:", sorted(names) == sorted(n for g, ns in m.TOOLBOX for n in ns if n in m.TOOLS))
pb = [b for b in radios if b.cget('value') == "Pencil"][0]; pb.invoke(); root.update()
print("click Pencil:", a.tool.name, "| params shown:", 'size' in a.pvars, "| toolbox selection:", a.v_toolsel.get())
a.tree.selection_set("Levels"); root.update(); print("a one-shot op leaves the toolbox unselected:", a.v_toolsel.get() == "", "| chip:", a.v_group.get(), "| list shows it:", a.ops.exists("Levels") and a.ops.selection() == ("Levels",))
a.v_group.set("Filters"); a._refill_ops(); print("chip Filters lists:", list(a.ops.get_children()))
a.ops.selection_set("Reduce noise"); root.update(); print("click in the list selects the tool:", a.tool.name)
a.v_find.set("curv"); root.update(); print("search 'curv':", list(a.ops.get_children()), "| placeholder hidden:", not a.l_ph.winfo_ismapped())
a.e_find.focus_force(); root.update(); a.e_find.event_generate('<Return>'); root.update(); print("Enter picks the first:", a.tool.name)
a.v_find.set("pen"); root.update(); print("search 'pen' finds mouse tools too:", list(a.ops.get_children()))
a.v_find.set("rotate"); root.update(); print("search 'rotate':", list(a.ops.get_children()))
a.v_find.set(""); root.update(); print("cleared: back to the group list:", len(a.ops.get_children()) > 3, "| placeholder back:", a.l_ph.winfo_ismapped())
root.event_generate('<Control-k>'); root.update(); print("Ctrl+K focuses the search:", root.focus_get() is a.e_find or str(root.focus_get()).endswith("entry"))
rect = a.b_modes["Select / move"]; rect.invoke(); root.update(); print("Rectangle button: mode", a.mode, "| pressed:", str(rect.cget('relief')), "| first in the toolbox:", a.toolbox.winfo_children()[0].cget('text')); a.cancel_mode()
wand = a.b_modes["Magic wand"]; wand.invoke(); root.update(); print("wand button: mode", a.mode, "| pressed look:", str(wand.cget('relief')))
a.cancel_mode(); root.update(); print("after Esc the wand button is up:", str(wand.cget('relief')))
hand = [b for b in bs if b.winfo_class() == 'Checkbutton'][0]; hand.invoke(); root.update(); print("Hand button toggles the Hand:", a.v_hand.get()); hand.invoke()
print("every built-in tool is reachable (list or toolbox):", all((t.name in {n for _, ns in m.TOOLBOX for n in ns}) or t.group in a._chip_groups for t in m.TOOLS.values()))
print("icons are all different:", len({m.tool_icon(n).tobytes() for _, ns in m.TOOLBOX for n in ns}) == sum(len(ns) for _, ns in m.TOOLBOX))
a.tree.selection_set("Fill bucket"); root.update()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'toolbox_ui.png'))
a.dirty = False; root.destroy()

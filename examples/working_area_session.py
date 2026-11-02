"""Scripted check of the 'Define working area' bar (real window, real mouse events): set the area BEFORE choosing a tool, with
a painting tool already selected, edit it by number, clear it; every tool must respect it.

    python examples/working_area_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((400, 600, 3), 235, np.uint8)
cv2.rectangle(img, (40, 60), (240, 160), (200, 0, 0), -1); cv2.rectangle(img, (360, 60), (560, 160), (200, 0, 0), -1)
cv2.rectangle(img, (40, 240), (240, 340), (30, 30, 30), -1); cv2.rectangle(img, (360, 240), (560, 340), (30, 30, 30), -1)
f = os.path.join(tempfile.gettempdir(), 'wa.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=5):
    ev('<ButtonPress-1>', *p)
    for i in range(1, n + 1): ev('<B1-Motion>', p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n, state=0x100)
    ev('<ButtonRelease-1>', *q)
def find_button(text):
    stack = [root]
    while stack:
        w = stack.pop()
        if w.winfo_class() == 'Button' and w.cget('text') == text: return w
        stack.extend(w.winfo_children())
btn_define, btn_whole = a.b_modes["Rectangle"], find_button("Whole image (no area)")
print("bar present:", btn_define is not None and btn_whole is not None, "| label:", a.l_area.cget('text'))

# 1. the awkward case: a painting tool is ALREADY selected, then the area is defined
a.tree.selection_set("Eraser brush"); root.update(); a.pvars['size'].set(30); root.update()
btn_define.invoke(); print("mode after the button:", a.mode)
drag(scr(20, 30), scr(300, 200))
print("area set with the brush tool selected:", a.sel, "| mode over:", a.mode is None, "| tool unchanged:", a.tool.name, "| boxes:", [a.area_vars[k].get() for k in 'XYWH'],
      "| label:", a.l_area.cget('text'))
orig = a.hist.current
n = len(a.hist.states); drag(scr(10, 100), scr(590, 100))                      # a stroke across the whole width, area is x 20..300
cur = a.hist.current
print("brush stroke clipped to the area:", "changed", int((cur != orig).any(axis=2).sum()), "px | right red box kept:", np.array_equal(cur[60:160, 360:560], orig[60:160, 360:560]),
      "| left red box erased where the stroke passed:", bool((cur[90:110, 45:235] == 255).all()))
a.undo()
# 2. other tools respect it too: Erase rectangle, a colour erase, a filter
a.tree.selection_set("Erase rectangle"); root.update(); a.pvars['fill'].set('white'); root.update()
n = len(a.hist.states); drag(scr(10, 220), scr(590, 360)); print("Erase rectangle completely outside the area: steps added", len(a.hist.states) - n, "|", a.status.cget('text')[:60])
drag(scr(10, 150), scr(590, 260)); cur = a.hist.current                          # overlaps the area (y 29..200, x 19..300) only partly
print("Erase rectangle clipped to the area:", bool((cur[155:195, 25:295] == 255).all()), "| beyond the area kept:", np.array_equal(cur[205:260, :], orig[205:260, :]) and np.array_equal(cur[150:200, 300:], orig[150:200, 300:]))
a.undo()
a.tree.selection_set("Invert"); root.update(); a.b_apply.invoke(); cur = a.hist.current
print("Invert only inside:", bool((cur[60:160, 40:240] == 255 - 200 * 0 - np.array([200, 0, 0])).all() or (cur[100, 100] != orig[100, 100]).any()), "| outside kept:", np.array_equal(cur[:, 360:], orig[:, 360:]))
a.undo()
# 3. the numbers
a.area_vars['X'].set("350"); a.area_vars['Y'].set("50"); a.area_vars['W'].set("230"); a.area_vars['H'].set("130"); a._apply_area_box()
print("typed area:", a.sel, "| label:", a.l_area.cget('text'))
a.tree.selection_set("Erase / keep colours"); root.update()
a.start_pick('color', 'colors'); ev('<ButtonPress-1>', *scr(450, 100)); ev('<ButtonRelease-1>', *scr(450, 100)); a.escape()
a.pvars['tolerance'].set(40); root.update(); a.b_apply.invoke(); cur = a.hist.current
print("erase colours inside the typed area only:", bool((cur[65:155, 365:555] == 255).all()), "| red box on the left kept:", np.array_equal(cur[60:160, 40:240], orig[60:160, 40:240]))
a.undo()
# 4. Esc while defining, then clearing
btn_define.invoke(); a.escape(); print("Esc cancelled the definition:", a.mode is None, "| area kept:", a.sel is not None)
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'area_ui.png'))
btn_whole.invoke(); print("Whole image: area", a.sel, "| boxes empty:", all(a.area_vars[k].get() == "" for k in 'XYWH'), "| label:", a.l_area.cget('text'))
a.dirty = False; root.destroy()

"""Drive the painting tools with REAL operating-system mouse and keyboard events (SetCursorPos / mouse_event / keybd_event),
not Tk's event_generate: Eraser brush, Clone stamp (Ctrl+click source) and Clean brush.

    python examples/real_mouse_brush.py
Windows only. The window is put on top and the pointer is moved by this script for a few seconds.
"""
import ctypes, importlib.machinery, importlib.util, os, tempfile, time, traceback
import numpy as np
from PIL import Image
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
user32 = ctypes.windll.user32
LEFT_DOWN, LEFT_UP, VK_CONTROL, KEYUP = 0x0002, 0x0004, 0x11, 0x0002

clean = np.zeros((300, 500, 3), np.uint8); clean[:] = np.linspace(40, 220, 500).astype(np.uint8)[None, :, None]
clean[:, :, 2] = np.linspace(200, 60, 300).astype(np.uint8)[:, None]
img = clean.copy(); img[100:112, 150:330] = (200, 0, 0)                         # a red bar across the ramp
f = os.path.join(tempfile.gettempdir(), 'rm.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
errors = []
a = m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost', True); root.update()
app_hook = root.report_callback_exception                                            # the app's own (log + status line)
root.report_callback_exception = lambda *x: (errors.append(x), app_hook(*x))
a.open_path(f); root.update(); c = a.canvas
root.focus_force(); user32.SetForegroundWindow(root.winfo_id()); root.update(); time.sleep(0.5); root.update()
if root.focus_displayof() is None:                                                   # real clicks would land on another window
    print("The ImageForge window is not in the foreground: not sending real mouse events."); a.dirty = False; root.destroy(); raise SystemExit(1)


def pump(sec=0.15):
    end = time.time() + sec
    while time.time() < end:
        root.update(); time.sleep(0.01)


def screen_of(ix, iy):
    return (c.winfo_rootx() + int(ix * a.zoom + a.ox), c.winfo_rooty() + int(iy * a.zoom + a.oy))


def real_drag(p, q, steps=12, ctrl=False):
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, 0, 0); pump(0.05)
    user32.SetCursorPos(*p); pump(); user32.mouse_event(LEFT_DOWN, 0, 0, 0, 0); pump(0.05)
    for i in range(1, steps + 1):
        user32.SetCursorPos(p[0] + (q[0] - p[0]) * i // steps, p[1] + (q[1] - p[1]) * i // steps); pump(0.03)
    user32.mouse_event(LEFT_UP, 0, 0, 0, 0); pump(0.05)
    if ctrl:
        user32.keybd_event(VK_CONTROL, 0, KEYUP, 0); pump(0.05)


def report(label, before):
    cur = a.hist.current
    print("%-22s steps %d | changed px %5d | status: %s" % (label, len(a.hist.states) - 1, int((cur != before).any(axis=2).sum()), a.status.cget('text')[:70]))


# 1. Eraser brush
a.tree.selection_set("Eraser brush"); root.update(); before = a.hist.current.copy()
real_drag(screen_of(160, 106), screen_of(320, 106)); report("Eraser brush", before)
a.undo()
# 2. Clean brush over the red bar: red should fade into the ramp
a.tree.selection_set("Clean brush"); root.update(); a.pvars['size'].set(20); root.update(); before = a.hist.current.copy()
real_drag(screen_of(150, 106), screen_of(330, 106)); report("Clean brush", before)
cur = a.hist.current
print("   bar replaced by the ramp: mean error", round(float(np.abs(cur[100:112, 155:325].astype(int) - clean[100:112, 155:325].astype(int)).mean()), 1),
      "(the red bar was ~120 off)")
a.undo()
# 3. Clone stamp: Ctrl+click a clean spot, paint over the bar
a.tree.selection_set("Clone stamp"); root.update(); a.pvars['size'].set(24); a.pvars['hardness'].set(40); root.update(); before = a.hist.current.copy()
real_drag(screen_of(200, 60), screen_of(200, 60), steps=1, ctrl=True)
print("   source set:", a._clone_src)
real_drag(screen_of(160, 106), screen_of(320, 106)); report("Clone stamp", before)
cur = a.hist.current
print("   bar replaced by a copy of the area 46 px above: mean error vs the ramp", round(float(np.abs(cur[100:112, 160:320].astype(int) - clean[100:112, 160:320].astype(int)).mean()), 1))
print("Tk errors:", len(errors))
for e in errors:
    traceback.print_exception(*e)
a.dirty = False; root.destroy()

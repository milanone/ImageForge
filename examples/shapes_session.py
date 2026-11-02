"""Scripted check of Shapes, Scale bar and Measure in the real window (real Tk mouse events, dialogs answered by the script).

    python examples/shapes_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((320, 480, 3), 230, np.uint8)
cv2.rectangle(img, (60, 60), (260, 200), (90, 150, 210), -1)             # a 200 x 140 px "object" of known size: 100 x 70 um
f = os.path.join(tempfile.gettempdir(), 'shp.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(pt, state=0): ev('<ButtonPress-1>', *scr(*pt), state=state); ev('<ButtonRelease-1>', *scr(*pt), state=state)
def drag(p, q, n=6):
    ev('<ButtonPress-1>', *scr(*p))
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100)
    ev('<ButtonRelease-1>', *scr(*q))
orig = a.hist.current

# 1. Shapes: drag a line, it becomes a layer on Apply; edit one end by dragging it; restyle; move the layer
a.tree.selection_set("Shapes"); root.update()
drag((300, 40), (440, 120)); root.update()
print("drag sets the ends:", [a.pvars[k].get() for k in ('x0', 'y0', 'x1', 'y1')], "| handles drawn:", len(c.find_withtag('shapeh')))
a.b_apply.invoke(); root.update(); L = a.hist.layers[-1]
print("layer:", L.kind, L.name, "| in the list:", "[shape]" in a.ltree.item('L0', 'text'), "| base untouched:", a.hist.current is orig, "| button:", a.b_apply.cget('text'))
drag((440, 120), (440, 200)); print("dragging the end moves only that end:", [a.pvars[k].get() for k in ('x0', 'y0', 'x1', 'y1')])
a.pvars['shape'].set('double arrow'); a.pvars['stroke'].set('#0044cc'); root.update(); a.b_apply.invoke(); root.update(); L2 = a.hist.layers[-1]
print("updated in place:", len(a.hist.layers), "layer | shape:", L2.params['shape'], "| colour:", L2.params['stroke'], "| still editable ends:", [round(L2.params[k] + (L2.x if 'x' in k else L2.y)) for k in ('x0', 'y0', 'x1', 'y1')])
a.tree.selection_set("Move layer"); root.update(); drag((370, 120), (370, 160)); L3 = a.hist.layers[-1]
a.tree.selection_set("Shapes"); root.update(); print("after moving the layer the boxes follow:", [a.pvars[k].get() for k in ('x1', 'y1')], "(layer moved by", L3.y - L2.y, "px down)")
a.ltree.selection_set('BG'); root.update()
drag((280, 240), (400, 290)); a.pvars['shape'].set('rectangle'); a.pvars['filled'].set(True); a.pvars['fill'].set('#ffee88'); a.pvars['dash'].set('dashed'); root.update(); a.b_apply.invoke(); root.update()
print("a second shape needs the Background selected:", [x.name for x in a.hist.layers])

# 2. Measure: a distance, calibrate with it, a path, an angle, an area; the table
a.tree.selection_set("Measure"); root.update()
drag((60, 130), (260, 130)); mm = a.measures[-1]
print("distance:", round(mm['value'], 1), "px |", a._measure_text(mm), "| overlay items:", len(c.find_withtag('measure')))
answers = iter([100.0, "um"])
m.simpledialog.askfloat = lambda *x, **k: next(answers)
m.simpledialog.askstring = lambda *x, **k: next(answers)
a.calibrate(); print("calibrated:", a.scale, "| info:", a.info.cget('text')[-24:], "| label now:", a._measure_text(a.measures[0]))
a.pvars['what'].set('path length'); root.update(); click((60, 60)); click((260, 60)); click((260, 200)); click((260, 200)); print("path length (double click ends):", a._measure_text(a.measures[-1]))
a.pvars['what'].set('angle'); root.update(); click((300, 260)); click((380, 260)); click((380, 190)); print("angle:", a._measure_text(a.measures[-1]))
a.pvars['what'].set('area'); root.update(); click((60, 60)); click((260, 60)); click((260, 200)); click((60, 200)); c.event_generate('<Return>'); root.update()
print("area of the 200 x 140 px object:", a._measure_text(a.measures[-1]))
print("table:", a.measurement_table().split("\n")[1:3])
a.show_measurements(); root.update(); print("window rows:", len(a.m_tree.get_children()))

# 3. Scale bar uses the scale; length 50 um = 100 px at 0.5 um/px
a.ltree.selection_set('BG'); root.update(); a.tree.selection_set("Scale bar"); root.update(); a.pvars['length'].set(50.0); a.pvars['x'].set(30); a.pvars['y'].set(260); root.update()
a.b_apply.invoke(); root.update(); sb = a.hist.layers[-1]
print("scale bar:", sb.kind, sb.params['text'], "| bar px:", round(sb.params['px_len'], 1), "| rendered bar width:", int((sb.raster()[..., 3] > 200).sum(axis=1).max()))
# 4. the project keeps shapes, the scale bar and the scale
path = os.path.join(tempfile.gettempdir(), 'shp_test.ifl')
a._write(path); b2, layers, dpi = m.load_project(path)
print("project:", [L.kind for L in layers], "| scale kept:", m.project_scale(path))
a.scale = None; a.open_path(path); print("reopened: scale", a.scale, "| layers", [L.kind for L in a.hist.layers])
a.clear_scale(); print("cleared:", a.scale)
a.tree.selection_set("Scale bar"); root.update(); a.ltree.selection_set('BG'); root.update(); n = len(a.hist.layers); a.b_apply.invoke(); print("scale bar without a scale: layers added", len(a.hist.layers) - n, "|", a.status.cget('text')[:55])
a.open_path(path)
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'shapes_ui.png'))
a.dirty = False; root.destroy()

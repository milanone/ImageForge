"""Scripted check of the drawing tools in the real window (real Tk mouse and key events): Pencil, Paintbrush (with Shift+click lines),
the foreground / background colours (Ctrl+click, Ctrl+Shift+click, X, D), Fill bucket, Gradient with its live preview, on the
Background, on a layer and inside a shape selection.

    python examples/draw_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
img = np.full((300, 440, 3), 235, np.uint8)
cv2.rectangle(img, (40, 40), (160, 140), (200, 40, 40), -1); cv2.rectangle(img, (240, 40), (400, 140), (40, 150, 60), -1)
cv2.circle(img, (120, 230), 45, (40, 40, 200), 3)
f = os.path.join(tempfile.gettempdir(), 'draw.png'); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def click(pt, state=0): ev('<ButtonPress-1>', *scr(*pt), state=state); ev('<ButtonRelease-1>', *scr(*pt), state=state)
def drag(p, q, n=8, state=0):
    ev('<ButtonPress-1>', *scr(*p), state=state)
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100 | state)
    ev('<ButtonRelease-1>', *scr(*q), state=state)
orig = a.hist.current
CTRL, SHIFT = 0x4, 0x1

# 1. the colours: Ctrl+click picks the foreground, Ctrl+Shift+click the background, X swaps, D resets
a.tree.selection_set("Pencil"); root.update()
click((100, 80), CTRL); click((320, 80), CTRL | SHIFT)
print("fg picked:", a.fg, "| bg picked:", a.bg, "| swatches:", a.b_fg.cget('bg'), a.b_bg.cget('bg'), "| history unchanged:", a.hist.current is orig)
c.focus_set(); c.event_generate('<x>'); root.update(); print("X swaps:", a.fg, a.bg)
c.event_generate('<d>'); root.update(); print("D resets:", a.fg, a.bg)
a.set_colour('fg', (230, 0, 120))

# 2. the pencil: hard, one stroke = one step, Shift+click makes a straight line from the end of the last stroke
a.pvars['size'].set(1); root.update(); n = len(a.hist.states)
drag((30, 200), (80, 200)); cur = a.hist.current
ch = (cur != orig).any(axis=2); rows = np.nonzero(ch.any(axis=1))[0]
print("pencil: one step", len(a.hist.states) - n, "| changed rows", rows.tolist(), "(a one-pixel line) | colour:", cur[rows[0], np.nonzero(ch[rows[0]])[0][0]].tolist(), "| length", int(ch.sum()))
click((80, 270), SHIFT); cur = a.hist.current; ch2 = (cur != orig).any(axis=2)
print("Shift+click: a straight line from the last end (80, 200) down to (80, 270):", int(ch2[:, 79:82].any(axis=1).sum()) > 60, "| steps", len(a.hist.states) - n)
a.undo(); a.undo()

# 3. the paintbrush: soft edge, opacity of the whole stroke, no darker where it crosses itself
a.tree.selection_set("Paintbrush"); root.update(); a.pvars['size'].set(30); a.pvars['hardness'].set(0); a.pvars['opacity'].set(50); root.update()
a.set_colour('fg', (0, 0, 0))
ev('<ButtonPress-1>', *scr(200, 200))
for p in ((260, 200), (260, 230), (200, 230), (200, 200), (260, 200)):                  # a loop that crosses itself
    ev('<B1-Motion>', *scr(*p), state=0x100)
ev('<ButtonRelease-1>', *scr(260, 200)); cur = a.hist.current
dark_line, dark_cross = int(cur[200, 230, 0]), int(cur[200, 205, 0])
print("brush stroke: centre grey", dark_line, "(50 % of black over 235 = 118) | where the loop crosses itself", dark_cross, "| soft edge:", int(cur[200 + 11, 230, 0]) > int(cur[200 + 3, 230, 0]) > 0)
a.undo()

# 4. fill bucket: tolerance, contiguous, the fringe, the background colour
a.tree.selection_set("Fill bucket"); root.update(); a.set_colour('fg', (255, 200, 0)); a.set_colour('bg', (0, 200, 255)); root.update()
click((100, 80)); cur = a.hist.current
print("fill the red box:", bool((cur[45:135, 45:155] == (255, 200, 0)).all()), "| green box kept:", np.array_equal(cur[40:140, 240:400], orig[40:140, 240:400]))
a.pvars['use'].set('background'); root.update(); click((320, 80)); cur = a.hist.current
print("fill with the background colour:", bool((cur[50:130, 250:390] == (0, 200, 255)).all()))
a.undo(); a.undo()
a.pvars['use'].set('foreground'); a.pvars['grow'].set(0); root.update(); click((120, 230)); print("clicking inside the ring leaves the outside alone:", a.hist.current[230, 120].tolist(), "| outside page:", a.hist.current[20, 20].tolist() == orig[20, 20].tolist())
a.undo()

# 5. gradient: live preview while dragging, one step on release, Esc cancels
a.tree.selection_set("Gradient"); root.update(); a.set_colour('fg', (255, 0, 0)); a.set_colour('bg', (0, 0, 255)); root.update(); n = a.hist.index
ev('<ButtonPress-1>', *scr(0, 280)); ev('<B1-Motion>', *scr(220, 280), state=0x100); ev('<B1-Motion>', *scr(439, 280), state=0x100)
print("preview while dragging:", a.preview is not None, "| no step yet:", a.hist.index == n)
ev('<ButtonRelease-1>', *scr(439, 280)); cur = a.hist.current
print("gradient committed:", a.hist.index - n, "step | left red:", int(cur[290, 2, 0]) > 240, "| right blue:", int(cur[290, 437, 2]) > 240, "| middle mixed:", 100 < int(cur[290, 220, 0]) < 160)
a.undo(); n = a.hist.index
ev('<ButtonPress-1>', *scr(0, 280)); ev('<B1-Motion>', *scr(200, 280), state=0x100); c.event_generate('<Escape>'); root.update(); ev('<ButtonRelease-1>', *scr(200, 280))
print("Esc cancels a gradient:", a.preview is None and a.hist.index == n)
a.tree.selection_set("Gradient"); root.update(); a.pvars['colours'].set('foreground to transparent'); a.pvars['kind'].set('radial'); root.update()
drag((220, 90), (320, 90)); cur = a.hist.current
print("radial, foreground to transparent: centre red", int(cur[90, 220, 0]) > 240 and int(cur[90, 220, 2]) < 20, "| far edge is the picture:", np.array_equal(cur[90, 400:], orig[90, 400:]))
a.undo()

# 6. inside a shape selection and on a layer
a.sel = m.combine_selection(None, m.polygon_mask(orig.shape[:2], [(0, 150), (150, 150), (150, 299), (0, 299)]), 'replace', orig.shape[:2]); a._after_selection()
a.tree.selection_set("Paintbrush"); root.update(); a.pvars['hardness'].set(100); a.pvars['opacity'].set(100); a.pvars['size'].set(20); root.update(); a.set_colour('fg', (0, 0, 0))
drag((20, 200), (290, 200)); cur = a.hist.current
print("brush limited by the selection:", bool((cur[200, 30:140] == 0).all()), "| outside kept:", bool((cur[200, 170:280] == orig[200, 170:280]).all()))
a.undo(); a.deselect()
a.selection_to_layer(False); root.update() if False else None
a.sel = (200, 150, 330, 280); a.selection_to_layer(False); root.update(); L0 = a.hist.layers[-1]
a.tree.selection_set("Paintbrush"); root.update(); drag((230, 200), (300, 200)); L1 = a.hist.layers[-1]
print("painted on the layer, not the Background:", not np.array_equal(L1.rgba, L0.rgba), "| base untouched:", a.hist.current is orig, "|", a.hist.states[-1][0])
a.tree.selection_set("Fill bucket"); root.update(); a.set_colour('fg', (255, 0, 255)); click((210, 160)); print("fill on the layer:", a.hist.states[-1][0], "| base untouched:", a.hist.current is orig)
root.after(300, root.quit); root.mainloop()
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'draw_ui.png'))
a.dirty = False; root.destroy()

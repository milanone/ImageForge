"""Scripted check of the workflow features in the real window: recipe recording and the History tab, saving / running a recipe, the
batch dialog on a folder, the command line, recent files and saved settings, Paste and Export.

    python examples/workflow_session.py
Uses a temporary settings file (IMAGEFORGE_SETTINGS), so it never touches yours.
"""
import sys as _s; _s.stdout.reconfigure(errors='replace')
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2, subprocess, sys, json
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
tmp = tempfile.mkdtemp()
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tmp, "settings.json")
with open(os.environ["IMAGEFORGE_SETTINGS"], "w") as f:
    json.dump({"geometry": "1100x800+30+30", "recent": []}, f)
here = os.path.dirname(os.path.abspath(__file__))
script = os.path.join(here, "..", "ImageForge.pyw")
l = importlib.machinery.SourceFileLoader("m", script)
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
src = os.path.join(tmp, "pics"); os.makedirs(os.path.join(src, "sub"))
rs = np.random.RandomState(1)
for name in ("one.png", "two.jpg", os.path.join("sub", "three.png")):
    Image.fromarray(np.clip(rs.randn(90, 140, 3) * 25 + 110, 0, 255).astype(np.uint8)).save(os.path.join(src, name))
f1 = os.path.join(src, "one.png")
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.update()
print("window geometry restored from the settings file:", root.geometry())
root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.open_path(f1); root.update(); c = a.canvas
def ev(k, x, y, **kw): c.event_generate(k, x=x, y=y, **kw); root.update()
def scr(ix, iy): return int(ix * a.zoom + a.ox), int(iy * a.zoom + a.oy)
def drag(p, q, n=5):
    ev('<ButtonPress-1>', *scr(*p))
    for i in range(1, n + 1): ev('<B1-Motion>', *scr(p[0] + (q[0] - p[0]) * i // n, p[1] + (q[1] - p[1]) * i // n), state=0x100)
    ev('<ButtonRelease-1>', *scr(*q))

# 1. steps recorded while editing; the History tab shows what a recipe can repeat
a.tree.selection_set("Levels"); root.update(); a.pvars['gamma'].set(1.4); root.update(); a.b_apply.invoke(); root.update()
a.tree.selection_set("Eraser brush"); root.update(); a.pvars['size'].set(10); root.update(); drag((20, 20), (100, 20))
a.tree.selection_set("Invert"); root.update(); a.b_apply.invoke(); root.update()
a.tree.selection_set("Reduce noise"); root.update(); a.pvars['method'].set('median'); root.update(); a.sel = (0, 0, 70, 45); a._after_selection(); a.b_apply.invoke(); root.update()
a.deselect()
rows = [(a.htree.item(i, 'text'), a.htree.set(i, 'rec')) for i in a.htree.get_children()]
print("History tab:", [(r[0], r[1].encode("ascii", "replace").decode()) for r in rows])
a.nbk.select(2); root.update(); x_, y_ = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x_, y_, x_ + root.winfo_width(), y_ + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), 'history_ui.png'))
steps, skipped = a.hist.recipe()
print("recipe:", [(st['tool'], st['region']) for st in steps], "| skipped:", skipped)

# 2. jump in the history
n_states = len(a.hist.states)
a.htree.selection_set('1'); root.update(); root.update()
print("jump to step 1: index", a.hist.index, "| recipe now:", [st['tool'] for st in a.hist.recipe()[0]], "| later rows kept for redo:", len(a.hist.states) == n_states)
a.htree.selection_set(str(n_states - 1)); root.update(); root.update(); print("jump forward again: index", a.hist.index)

# 3. save the recipe (the file dialog is answered by the script), run it on another picture of the folder
rpath = os.path.join(tmp, "demo" + m.RECIPE_EXT)
m.filedialog.asksaveasfilename = lambda *x, **k: rpath
a.save_recipe_dialog(); print("recipe file:", os.path.isfile(rpath), "|", a.status.cget('text')[:70])
print('dirty before opening the next picture:', a.dirty); m.messagebox.askyesnocancel = lambda *x, **k: False
print('opened:', a.open_path(os.path.join(src, "two.jpg")), os.path.basename(a.path))
m.filedialog.askopenfilename = lambda *x, **k: rpath
before = a.hist.current; a.run_recipe_here(); print("recipe run on this picture:", not np.array_equal(a.hist.current, before), "|", a.hist.states[-1][0])

# 4. the batch dialog on the folder (sub-folders included, PNG out)
out = os.path.join(tmp, "out")
a.batch_dialog(); top = [w for w in root.winfo_children() if w.winfo_class() == 'Toplevel'][-1]
entries = [w for w in top.winfo_children() if w.winfo_class() == 'Entry']
entries[0].insert(0, rpath); entries[1].insert(0, src); entries[2].insert(0, out)
for w in top.winfo_children():
    if w.winfo_class() == 'Radiobutton' and "recipe file" in w.cget('text'): w.invoke()
    if w.winfo_class() == 'Checkbutton' and "sub-folders" in w.cget('text'): w.invoke()
for w in top.winfo_children():
    if w.winfo_class() == 'Frame':
        for k in w.winfo_children():
            if k.winfo_class() == 'TCombobox': k.set('png')
a.b_batch.invoke()
for _ in range(200):
    root.update()
    if a.l_batch.cget('text').startswith("Done"): break
    root.after(20)
print("batch:", a.l_batch.cget('text'), "| outputs:", sorted(os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs))
top.destroy()

# 5. the command line
r = subprocess.run([sys.executable, script, "--batch", src, os.path.join(tmp, "cli"), "--apply", "Invert", "--apply", "Levels:gamma=1.5,in_black=10", "--format", "png", "--suffix", "_cli", "--recursive"],
                   capture_output=True, text=True, timeout=120)
print("CLI:", r.returncode, "|", r.stdout.strip().splitlines()[-1], "| files:", sum(len(fs) for _, _, fs in os.walk(os.path.join(tmp, "cli"))))
r = subprocess.run([sys.executable, script, "--list-tools"], capture_output=True, text=True, timeout=60)
print("--list-tools:", r.returncode, "| mentions Levels and its gamma box:", "Levels" in r.stdout and "gamma" in r.stdout)
r = subprocess.run([sys.executable, script, "--batch", src, os.path.join(tmp, "cli2"), "--apply", "Nope"], capture_output=True, text=True, timeout=60)
print("CLI with a wrong tool name:", r.returncode, "|", r.stdout.strip()[:70])

# 6. recent files
a.open_path(f1)
labels = [a.m_recent.entrycget(i, 'label') for i in range(a.m_recent.index('end') + 1) if a.m_recent.type(i) == 'command']
print("recent menu:", [os.path.basename(x) for x in labels])

# 7. paste from the clipboard (Windows)
png = os.path.join(tmp, "clip.png"); Image.fromarray(np.full((30, 50, 3), (200, 30, 30), np.uint8)).save(png)
ps = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                     "[System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile('%s'))" % png], capture_output=True, text=True, timeout=60)
n = len(a.hist.layers); a.paste_clipboard()
print("paste:", "layers %d -> %d" % (n, len(a.hist.layers)), "|", a.status.cget('text')[:60], "| powershell rc", ps.returncode)

# 8. export with options (the dialog is clicked by the script)
exp = os.path.join(tmp, "exported.jpg")
m.filedialog.asksaveasfilename = lambda *x, **k: exp
def click_export():
    d = [w for w in root.winfo_children() if w.winfo_class() == 'Toplevel'][-1]
    for w in d.winfo_children():
        if w.winfo_class() == 'Button' and w.cget('text') == "Export": w.invoke()
root.after(200, click_export); a.export_dialog()
print("export:", os.path.isfile(exp), "|", a.status.cget('text')[:70], "| layers still there:", len(a.hist.layers))

# 9. closing saves the settings
a.dirty = False; a.on_close()
st = m.load_settings(os.environ["IMAGEFORGE_SETTINGS"])
print("settings saved:", sorted(st), "| recent:", [os.path.basename(x) for x in st.get('recent', [])][:3])

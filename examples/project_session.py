"""Scripted check of project save/open with the real window (dialogs replaced by fixed answers).

    python examples/project_session.py
"""
import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2
from unittest import mock
from PIL import Image
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
tmp = tempfile.mkdtemp()
img = np.full((300, 400, 3), 235, np.uint8); cv2.circle(img, (120, 120), 50, (200, 50, 40), -1)
src = os.path.join(tmp, "src.png"); Image.fromarray(img).save(src)
proj = os.path.join(tmp, "work.ifl"); png = os.path.join(tmp, "out.png")

root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1100x760"); root.update(); a.open_path(src); root.update()
a.tree.selection_set("Add text"); root.update(); a.pvars['text'].set("Keep me"); a.pvars['size'].set(36); root.update()
a.b_apply.invoke(); a.sel = (60, 60, 180, 180); a.selection_to_layer(False); root.update()
print("layers before:", [L.name for L in a.hist.layers], "dirty", a.dirty)
with mock.patch.object(m.filedialog, "asksaveasfilename", return_value=png) as ask:
    a.save()                                                      # no project yet: the dialog opens, asked for a PNG
    print("Ctrl+S asked", ask.call_count, "time(s); types start with:", ask.call_args.kwargs["filetypes"][0][1])
print("after exporting a flat PNG: dirty still", a.dirty, "| save_path", a.save_path)
with mock.patch.object(m.filedialog, "asksaveasfilename", return_value=proj):
    a.save_as()
print("after saving the project: dirty", a.dirty, "| save_path ends", os.path.basename(a.save_path), "| title", root.title())
flat = a._flat().copy(); a.dirty = False; root.destroy()

root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
b = m.ImageForge(root); root.geometry("1100x760"); root.update(); b.open_path(proj); root.update()
print("reopened layers:", [(L.name, L.kind) for L in b.hist.layers], "same picture:", np.array_equal(b._flat(), flat),
      "| save_path is the project:", b.save_path == proj, "| dirty", b.dirty)
b.ltree.selection_set('L0'); root.update(); b.tree.selection_set("Add text"); root.update()
print("text boxes loaded from the file:", repr(b.pvars['text'].get()), b.pvars['size'].get())
b.pvars['text'].set("Edited after reopening"); b.b_apply.invoke()
before = os.path.getmtime(proj)
b.save()                                                           # straight to the project: no dialog
print("saved back to the project without a dialog:", os.path.getmtime(proj) >= before, "| dirty", b.dirty)
b.dirty = False; root.destroy()
base, layers, dpi = m.load_project(proj)
print("file now holds:", [(L.name, L.params['text'] if L.kind == 'text' else '-') for L in layers])

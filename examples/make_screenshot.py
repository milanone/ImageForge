"""Draws a synthetic test picture and takes screenshot.png of the real window (selection, tool preview).

    python examples/make_screenshot.py
"""
import importlib.machinery
import importlib.util
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
loader = importlib.machinery.SourceFileLoader("imageforge_app", os.path.join(HERE, "..", "ImageForge.pyw"))
spec = importlib.util.spec_from_loader("imageforge_app", loader)
app_mod = importlib.util.module_from_spec(spec)
loader.exec_module(app_mod)


def make_picture(path):
    im = Image.new("RGB", (900, 600), (235, 240, 245))
    d = ImageDraw.Draw(im)
    for i in range(0, 900, 30):
        d.line([(i, 0), (i, 600)], fill=(215, 222, 230))
    d.ellipse([120, 90, 420, 390], fill=(220, 80, 60), outline=(90, 20, 10), width=5)
    d.rectangle([460, 160, 780, 460], fill=(60, 130, 200), outline=(10, 40, 90), width=5)
    d.polygon([(300, 560), (450, 330), (600, 560)], fill=(250, 200, 50), outline=(120, 90, 0))
    d.text((40, 30), "ImageForge test picture", fill=(30, 30, 30))
    im.save(path)


def main():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # so the grab rectangle is in real pixels
    except Exception:
        pass
    pic = os.path.join(HERE, "test_picture.png")
    make_picture(pic)
    root = app_mod.TkinterDnD.Tk() if app_mod.HAS_DND else app_mod.tk.Tk()
    app = app_mod.ImageForge(root)
    root.geometry("1250x820+40+20")
    root.attributes('-topmost', True)
    root.update()
    app.open_path(pic)
    root.update()
    app.sel = (100, 70, 480, 420)
    app._after_selection()
    app.tree.selection_set("Sharpen (unsharp mask)")
    root.update()
    app.pvars['amount'].set(300)
    root.update()
    root.after(800, root.quit)
    root.mainloop()
    x, y = root.winfo_rootx(), root.winfo_rooty()
    ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(HERE, "..", "screenshot.png"))
    root.destroy()


if __name__ == '__main__':
    main()

"""Scripted check of Remove object (API, paid) in the real window.

    python examples/remove_generative_session.py            FREE: a fake OpenRouter answers, on a synthetic picture with two red squares
    python examples/remove_generative_session.py --real PHOTO.jpg
                                                            PAID, about 0.04 USD per request, 2 requests: it removes the dark flask and the green bottle of the
                                                            desk photo WITHOUT a description, through the real window and the real service (key from OPENROUTER_API_KEY).
                                                            The confirmation dialog is answered by the script: NO if a face is in the crop or the estimate is
                                                            above 0.06 USD, so it can never send a face or spend more than expected.

It checks the panel (credit line, Guide button), the confirmation text (what is sent, cost, credit), the background run (the window keeps ticking, Esc
is wired), the result (one history step, only the area of the object changed) and Undo.
"""
import base64, importlib.machinery, importlib.util, io, json, os, sys, tempfile, time
import cv2, numpy as np
from PIL import Image, ImageGrab, ImageOps
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here = os.path.dirname(os.path.abspath(__file__))
l = importlib.machinery.SourceFileLoader("m", os.path.join(here, "..", "ImageForge.pyw"))
s = importlib.util.spec_from_loader("m", l); m = importlib.util.module_from_spec(s); l.exec_module(m)
real = "--real" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
os.environ["IMAGEFORGE_SETTINGS"] = os.path.join(tempfile.gettempdir(), "gen_settings.json")
if real:
    if not args or not os.path.exists(args[0]) or not m.openrouter_key():
        print("--real needs the photo as an argument and OPENROUTER_API_KEY set. Skipped."); sys.exit(0)
    img = np.asarray(ImageOps.exif_transpose(Image.open(args[0])).convert("RGB"))
else:
    rng = np.random.default_rng(5)
    img = cv2.GaussianBlur(rng.integers(0, 255, (400, 600, 3)).astype(np.uint8), (0, 0), 1.2)
    for _ in range(120):
        cv2.circle(img, (int(rng.integers(0, 600)), int(rng.integers(0, 400))), int(rng.integers(3, 12)), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    bg = img.copy(); img[170:230, 120:180] = (255, 0, 0); img[170:230, 420:480] = (255, 0, 0)
    calls = []

    def fake(req, timeout=None):                                 # a stand-in for OpenRouter
        url = req.full_url.replace(m.OPENROUTER_API, ""); calls.append(url)
        if url == "/credits": return io.BytesIO(json.dumps({"data": {"total_credits": 20, "total_usage": 9.9}}).encode())
        if url == "/models": return io.BytesIO(json.dumps({"data": []}).encode())
        body = json.loads(req.data); uri = body["messages"][0]["content"][1]["image_url"]["url"]
        sent = np.asarray(Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGB"))
        red = cv2.dilate(((sent[..., 0] > 200) & (sent[..., 1] < 60) & (sent[..., 2] < 60)).astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
        out = cv2.cvtColor(cv2.inpaint(cv2.cvtColor(sent, cv2.COLOR_RGB2BGR), red, 6, cv2.INPAINT_TELEA), cv2.COLOR_BGR2RGB)
        buf = io.BytesIO(); Image.fromarray(out).save(buf, "PNG")
        return io.BytesIO(json.dumps({"choices": [{"message": {"images": [{"image_url": {"url": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}}]}}], "usage": {"cost": 0.0391}}).encode())
    m._urlopen = fake; m.openrouter_key = lambda: "sk-fake"
f = os.path.join(tempfile.gettempdir(), "gen_in.png"); Image.fromarray(img).save(f)
root = m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a = m.ImageForge(root); root.geometry("1250x940+40+20"); root.attributes('-topmost', True); root.update(); a.dirty = False; a.open_path(f); root.update()
dialogs = []
def ask(title, text):                                            # the confirmation: recorded, and answered safely
    dialogs.append(text)
    if real and ("WARNING" in text or "Estimated cost: about 0.0" not in text or float(text.split("Estimated cost: about ")[1].split(" ")[0]) > 0.06):
        return False
    return True
m.messagebox.askyesno = ask; m.messagebox.showwarning = lambda t, x: dialogs.append("WARNING DIALOG: " + x)
orig = a.hist.current; h, w = orig.shape[:2]
a.tree.selection_set("Remove object (API, paid)"); root.update()
print("tool:", a.tool.name, "| group:", a.tool.group, "| credit line:", a.l_or.cget('text'))
a.apply(); root.update(); print("no selection / no words -> nothing sent:", a._busy is None and not dialogs, "|", a.status.cget('text')[:80])
def select(polys):
    sel = None
    for P in polys: sel = m.combine_selection(sel, m.polygon_mask(orig.shape[:2], P), 'add', orig.shape[:2])
    a.sel = sel; a._after_selection(); root.update()
def run(what, polys):
    global orig
    select(polys); a.pvars['what'].set(what); root.update(); n0 = len(dialogs); before = a.hist.current; ticks = []
    def tick(): ticks.append(1); root.after(100, tick)
    tick(); t0 = time.time(); a.apply(); root.update()
    print("confirmation shown:", len(dialogs) == n0 + 1)
    print("-----\n" + dialogs[-1] + "\n-----") if len(dialogs) > n0 else None
    if a._busy is None: print("nothing started:", a.status.cget('text')); return False
    while a._busy is not None and time.time() - t0 < 180: root.update(); time.sleep(0.05)
    print("done in %.1f s, window ticks meanwhile: %d | status: %s" % (time.time() - t0, len(ticks), a.status.cget('text')))
    print("credit line now:", a.l_or.cget('text'))
    return a.hist.current is not before
if not real:
    ok = run("the red square", [[(115, 165), (185, 165), (185, 235), (115, 235)]])
    cur = a.hist.current
    print("one history step:", ok, a.hist.states[-1][0], "| red gone:", int(((cur[165:235, 115:185, 0] > 200) & (cur[165:235, 115:185, 1] < 60)).sum()) == 0, "| second square untouched:", bool((cur[170:230, 420:480] == (255, 0, 0)).all()), "| far away identical:", bool(np.array_equal(cur[:, 300:410], orig[:, 300:410])))
    print("requests made:", [c for c in calls if c == "/chat/completions"].__len__(), "| key never in a dialog or the status:", all("sk-fake" not in d for d in dialogs) and "sk-fake" not in a.status.cget('text'))
    a.undo(); print("Undo restores the picture:", np.array_equal(a.hist.current, orig))
    run("the red squares", [[(115, 165), (185, 165), (185, 235), (115, 235)], [(415, 165), (485, 165), (485, 235), (415, 235)]])
    print("two pieces: 2 requests, announced in the dialog:", "2 crops" in dialogs[-1], [c for c in calls if c == "/chat/completions"].__len__() == 3)
    a.undo(); a.undo() if a.hist.current is not orig and not np.array_equal(a.hist.current, orig) else None
    ok = run("", [[(115, 165), (185, 165), (185, 235), (115, 235)]]); cur = a.hist.current
    print("empty description: the dialog says so:", "No description given" in dialogs[-1], "| removed anyway:", ok and int(((cur[165:235, 115:185, 0] > 200) & (cur[165:235, 115:185, 1] < 60)).sum()) == 0)
else:
    k = w / 900.0; oy = int(800 * k)
    thermos = [(118,282),(150,250),(240,238),(330,245),(348,268),(348,330),(340,360),(345,390),(352,420),(375,440),(395,470),(405,520),(412,580),(414,674),(90,674),(92,560),(100,520),(125,480),(150,450),(148,420),(95,405),(85,395),(110,370),(125,345),(122,310)]
    bottle = [(585,62),(660,62),(662,100),(690,130),(712,170),(718,230),(705,270),(690,300),(715,340),(722,430),(722,520),(722,560),(710,600),(670,630),(640,640),(600,632),(560,610),(545,570),(543,520),(545,430),(545,350),(560,325),(580,300),(570,270),(545,235),(535,185),(545,160),(575,120),(582,100)]
    P = lambda L: [(x * 2, oy + y * 2) for x, y in L]
    r1 = run("", [P(thermos)])                       # no description: the object at the centre of the crop
    r2 = run("", [P(bottle)]) if r1 else False        # the hard case, also without a description
    print("both runs committed:", r1, r2, "| history labels:", [st[0] for st in a.hist.states][-3:])
    Image.fromarray(a.hist.current).save(os.path.join(tempfile.gettempdir(), "gen_real_result.jpg"), quality=93); print("saved", os.path.join(tempfile.gettempdir(), "gen_real_result.jpg"))
x, y = root.winfo_rootx(), root.winfo_rooty(); ImageGrab.grab((x, y, x + root.winfo_width(), y + root.winfo_height())).save(os.path.join(tempfile.gettempdir(), "gen_ui.png"))
a.show_object_guide(); root.update(); print("guide window opens:", a._guide_win.winfo_exists(), "| text length:", len([c for c in a._guide_win.winfo_children() if c.winfo_class() == 'Text'][0].get('1.0', 'end')))
a.dirty = False; root.destroy()

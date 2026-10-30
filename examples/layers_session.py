import importlib.machinery, importlib.util, numpy as np, tempfile, os, cv2, sys
from PIL import Image, ImageGrab
try:
    import ctypes; ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception: pass
here=os.path.dirname(os.path.abspath(__file__))
l=importlib.machinery.SourceFileLoader("m",os.path.join(here,"..","ImageForge.pyw")); s=importlib.util.spec_from_loader("m",l); m=importlib.util.module_from_spec(s); l.exec_module(m)
img=np.full((400,600,3),235,np.uint8); cv2.circle(img,(150,150),60,(200,50,40),-1); cv2.rectangle(img,(350,200),(500,320),(40,110,190),-1)
f=os.path.join(tempfile.gettempdir(),'lay.png'); Image.fromarray(img).save(f)
root=m.TkinterDnD.Tk() if m.HAS_DND else m.tk.Tk()
a=m.ImageForge(root); root.geometry("1250x820+40+20"); root.attributes('-topmost',True); root.update(); a.open_path(f); root.update(); c=a.canvas
def ev(k,x,y,**kw): c.event_generate(k,x=x,y=y,**kw); root.update()
def scr(ix,iy): return int(ix*a.zoom+a.ox), int(iy*a.zoom+a.oy)
def drag(p,q,n=4,state=0):
    ev('<ButtonPress-1>',*p,state=state)
    for i in range(1,n+1): ev('<B1-Motion>',p[0]+(q[0]-p[0])*i//n,p[1]+(q[1]-p[1])*i//n,state=0x100|state)
    ev('<ButtonRelease-1>',*q,state=state)
base0=a.hist.current
# 1. text layer
a.tree.selection_set("Add text"); root.update(); a.pvars['text'].set("Hello"); a.pvars['size'].set(40); a.pvars['color'].set('#1a7f37'); root.update()
ev('<ButtonPress-1>',*scr(300,40)); ev('<ButtonRelease-1>',*scr(300,40))
a._update_preview(); print('preview layer shown', a.lprev is not None, 'button', a.b_apply.cget('text'))
a.b_apply.invoke(); print('layers',[ (L.name,L.x,L.y) for L in a.hist.layers],'active',a.active,'button',a.b_apply.cget('text'),'base untouched',a.hist.current is base0)
# 2. edit the text layer
a.pvars['text'].set("Hello world"); a.pvars['size'].set(30); root.update(); a._update_preview(); a.b_apply.invoke()
print('after edit',[(L.name,L.params['size']) for L in a.hist.layers],'steps',[s[0] for s in a.hist.states])
# 3. selecting Background then Add -> second layer
a.ltree.selection_set('BG'); root.update(); a.pvars['text'].set("Second"); a.b_apply.invoke(); print('two layers',[L.name for L in a.hist.layers])
# 4. copy selection to a layer and move it with the mouse
a.tree.selection_set("Invert"); root.update()
drag(scr(80,80),scr(220,220)); a.selection_to_layer(True); root.update()
print('layer from cut',a.hist.layers[-1].name,'hole filled',(a.hist.current[90:100,90:100]==255).all(), 'tool',a.tool.name,'active',a.active)
L0=a.hist.layers[-1]; n=len(a.hist.states)
drag(scr(150,150),scr(300,250)); L1=a.hist.layers[-1]
print('dragged layer',(L0.x,L0.y),'->',(L1.x,L1.y),'one step',len(a.hist.states)-n)
a.nbk.select(1); root.update()
# 5. properties
a.v_lop.set(60); a._layer_prop(); print('opacity',a.hist.layers[-1].opacity)
a.v_lvis.set(False); a._layer_prop(); print('hidden composite equals base-with-others', a.hist.layers[-1].visible)
a.v_lvis.set(True); a._layer_prop()
root.after(300,root.quit); root.mainloop()
x,y=root.winfo_rootx(),root.winfo_rooty(); ImageGrab.grab((x,y,x+root.winfo_width(),y+root.winfo_height())).save(os.path.join(tempfile.gettempdir(),'layers_ui.png'))
# 6. undo all the way, redo, merge, flatten, save
for _ in range(len(a.hist.states)-1): a.undo()
print('undo to original',a.hist.layers==() and a.hist.current is base0)
for _ in range(len(a.hist.states)-1): a.redo()
flat_before=a._flat().copy(); nl=len(a.hist.layers)
a.active=len(a.hist.layers)-1; a.layer_merge_down(); print('merge down',nl,'->',len(a.hist.layers),'same look',np.abs(a._flat().astype(int)-flat_before.astype(int)).max())
a.flatten_all(); print('flatten layers',len(a.hist.layers),'same look',np.abs(a.hist.current.astype(int)-flat_before.astype(int)).max())
out=os.path.join(tempfile.gettempdir(),'lay_out.png')
a.undo(); a._write(out); saved=np.array(Image.open(out).convert('RGB')); print('saved equals composite',np.array_equal(saved,flat_before) or np.abs(saved.astype(int)-flat_before.astype(int)).max())
a.dirty=False; root.destroy()

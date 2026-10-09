"""Optional browser integration check. Requires PySide6 QtWebEngine; uses localhost fixtures only."""
import importlib.util,json,os,tempfile,threading,sys,time
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX','1')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS','--disable-gpu --disable-dev-shm-usage')
from PySide6.QtCore import QUrl,QPoint,Qt,QEventLoop,QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from PySide6.QtWebEngineWidgets import QWebEngineView
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'src'))
from ksound_hub.soundboard_library import remote_folder_move
app=QApplication([]); view=QWebEngineView();view.resize(920,760)
tmp=tempfile.TemporaryDirectory(); config=Path(tmp.name)
os.environ['KSH_CONFIG_DIR']=str(config)
doc={'folders':['Memes','Alerts'],'remote_columns':4,'slots':[{'id':f'sb{i}','label':f'Sound {i}','path':f'{i}.wav','folder':'Memes' if i<80 else 'Alerts'} for i in range(160)]}
(config/'soundboard.json').write_text(json.dumps(doc))
spec=importlib.util.spec_from_file_location('web_edit',root/'tools/soundboard_remote/ksound_soundboard_web.py'); web=importlib.util.module_from_spec(spec);spec.loader.exec_module(web)
commands=[]; unavailable=False
lock=threading.Lock()
def ipc(payload):
 commands.append(payload)
 if unavailable: return False,'offline'
 with lock:
  path=config/'soundboard.json';data=json.loads(path.read_text())
  if payload['command']=='soundboard-move-to-folder':
   data,_,_=remote_folder_move(data,payload['ids'],payload['folder'],expected_folder=payload.get('expected_folder'))
  elif payload['command']=='soundboard-reorder-slots':
   byid={s['id']:s for s in data['slots']};data['slots']=[byid[i] for i in payload['order']]
  else:return True,'OK'
  path.write_text(json.dumps(data))
 return True,'OK'
web.send_ipc=ipc
server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
def js(script):
 loop=QEventLoop();box=[]
 view.page().runJavaScript(script,lambda v:(box.append(v),loop.quit()))
 QTimer.singleShot(5000,loop.quit);loop.exec()
 assert box,'JS timed out'
 return box[0]
def state(expr):return json.loads(js('JSON.stringify('+expr+')'))
def point(selector):
 p=state(f'(()=>{{const r=document.querySelector({json.dumps(selector)}).getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]}})()')
 assert 0<=p[0]<=920 and 0<=p[1]<=760,(selector,p)
 return QPoint(int(p[0]),int(p[1]))
def click(selector):
 QTest.mouseClick(view.focusProxy(),Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,point(selector));QTest.qWait(80)
def waitfor(expr):
 end=time.monotonic()+5
 while time.monotonic()<end:
  if js(expr):return
  QTest.qWait(40)
 raise AssertionError('Timed out: '+expr)
loaded=[];view.loadFinished.connect(loaded.append);view.load(QUrl(f'http://127.0.0.1:{server.server_port}/?token={web.TOKEN}'));view.show()
try:
 load_deadline=time.monotonic()+10
 while not loaded and time.monotonic()<load_deadline:QTest.qWait(50)
 assert loaded==[True]
 QTest.qWait(400)
 click('#editButton'); click('.pad-wrap[data-slot="sb0"] .pad'); click('.pad-wrap[data-slot="sb1"] .pad')
 assert state('[...selectedSoundIds]')==['sb0','sb1']
 assert not js('document.getElementById("swapSelectedButton").disabled')
 assert not commands,commands
 js('selectFolder("Alerts")'); assert state('[...selectedSoundIds]')==['sb0','sb1']
 click('.pad-wrap[data-slot="sb80"] .pad')
 assert js('document.getElementById("swapSelectedButton").disabled')
 click('#moveSelectedButton')
 assert js('document.getElementById("folderMoveDialog").open')
 js('document.getElementById("moveFolderSelect").value="__new__";updateMoveDestination();document.getElementById("newFolderName").value="Grouped"')
 QTest.qWait(700)
 click('#confirmFolderMove');waitfor('!selectionMovePending')
 assert not js('document.getElementById("folderMoveDialog").open')
 assert state('visiblePadWraps.map(w=>w.dataset.slot)')==['sb0','sb1','sb80']
 assert state('[...selectedSoundIds]')==[]
 assert len(commands)==1
 click('.pad-wrap[data-slot="sb0"] .pad');unavailable=True
 click('#moveSelectedButton');js('document.getElementById("moveFolderSelect").value="Alerts"');click('#confirmFolderMove');waitfor('!selectionMovePending')
 assert state('[...selectedSoundIds]')==['sb0']
 assert 'unavailable' in js('document.getElementById("moveError").textContent')
 js('closeMoveFolderDialog();clearSoundSelection();setEditMode(false);selectFolder("__all__");window.scrollTo(0,0)');unavailable=False;QTest.qWait(100)
 start=point('.pad-wrap[data-slot="sb0"]');target=point('.pad-wrap[data-slot="sb2"]')+QPoint(50,0)
 QTest.mousePress(view.focusProxy(),Qt.LeftButton,Qt.NoModifier,start);QTest.qWait(470)
 assert js('!!document.querySelector(".drag-ghost")')
 QTest.mouseMove(view.focusProxy(),target,30);QTest.qWait(100)
 assert js('dragState.active')
 assert js('!!document.querySelector(".swap-target")')
 assert state('dragState.ghostWidth')<state('dragState.wrap.getBoundingClientRect().width')
 QTest.mouseRelease(view.focusProxy(),Qt.LeftButton,Qt.NoModifier,target);QTest.qWait(200)
 assert not js('!!document.querySelector(".drag-ghost")')
 assert state('padWraps().slice(0,3).map(w=>w.dataset.slot)')==['sb2','sb1','sb0']
 assert state('[...selectedSoundIds]')==[]
 assert commands[-1]['command']=='soundboard-reorder-slots'
 count=len(commands);QTest.qWait(400)
 start=point('.pad-wrap[data-slot="sb1"]');target=point('.pad-wrap[data-slot="sb2"]')
 QTest.mousePress(view.focusProxy(),Qt.LeftButton,Qt.NoModifier,start);QTest.mouseMove(view.focusProxy(),target,30);QTest.qWait(450)
 assert not js('!!document.querySelector(".drag-ghost")')
 QTest.mouseRelease(view.focusProxy(),Qt.LeftButton,Qt.NoModifier,target);QTest.qWait(100)
 assert len(commands)==count and state('[...selectedSoundIds]')==[]
 js('window.scrollTo(0,700)');QTest.qWait(200)
 assert abs(state('document.querySelector(".folder-bar").getBoundingClientRect().top'))<1

 js('setEditMode(false);selectFolder("Grouped");window.scrollTo(0,0)');QTest.qWait(600)
 click('.pad-wrap[data-slot="sb0"] .pad')

 end=time.monotonic()+3
 while commands[-1]['command']!='soundboard-play' and time.monotonic()<end:QTest.qWait(50)
 assert commands[-1]['command']=='soundboard-play',commands[-1]
 js('setEditMode(true);selectFolder("__all__");window.scrollTo(0,0)');QTest.qWait(400)
 assert js('getComputedStyle(document.querySelector(".pad")).touchAction')=='none','Edit mode must own touch gestures for live pinch'
 device=QTest.createTouchDevice()
 targetWidget=view.focusProxy()
 start=point('.pad-wrap[data-slot="sb1"]');down=QPoint(start.x(),start.y()+70)
 QTest.touchEvent(targetWidget,device).press(0,down,targetWidget).commit();QTest.qWait(60)
 for dy in range(10,151,20):
  QTest.touchEvent(targetWidget,device).move(0,QPoint(down.x(),down.y()-dy),targetWidget).commit();QTest.qWait(25)
 QTest.touchEvent(targetWidget,device).release(0,QPoint(down.x(),down.y()-150),targetWidget).commit();QTest.qWait(300)
 assert js('window.scrollY')>10, state('({scrollY,drag:!!dragState,pointers:activePointers.size})')
 assert state('[...selectedSoundIds]')==[]
 js('window.scrollTo(0,0)');QTest.qWait(600)
 # Edit mode: a long hold promotes the pad to insertion drag (tap still selects).
 start=point('.pad-wrap[data-slot="sb1"]')
 QTest.touchEvent(targetWidget,device).press(0,start,targetWidget).commit();QTest.qWait(470)
 assert js('!!dragState && dragState.active && dragState.inserting'),'edit mode hold did not start insertion drag'
 QTest.touchEvent(targetWidget,device).release(0,start,targetWidget).commit();QTest.qWait(180)
 # Outside Edit, hold and drag swaps pads. Pointer cancellation must not drop it early.
 js('setEditMode(false);window.scrollTo(0,0)');QTest.qWait(300)
 start=point('.pad-wrap[data-slot="sb1"]');target=point('.pad-wrap[data-slot="sb2"]')
 QTest.touchEvent(targetWidget,device).press(0,start,targetWidget).commit();QTest.qWait(470)
 assert js('!!dragState && dragState.active && !dragState.inserting'),'touch long press swap failed'
 assert js('(()=>{const s=dragState;onPadPointerCancel({pointerId:s.pointerId,pointerType:"touch",clientX:s.x,clientY:s.y});return !!dragState&&dragState.active&&dragState.pointerCancelled;})()'),'active touch drag did not survive pointercancel'
 QTest.touchEvent(targetWidget,device).move(0,target,targetWidget).commit();QTest.qWait(100)
 assert js('!!dragState && dragState.active'),'touch drag was cancelled by browser scroll'
 assert js('!!document.querySelector(".swap-target")')
 js(f'onGlobalTouchEnd({{type:"touchend",touches:[],changedTouches:[{{clientX:{target.x()},clientY:{target.y()}}}],preventDefault(){{}},stopPropagation(){{}},stopImmediatePropagation(){{}}}})')
 QTest.qWait(100)
 assert not js('!!dragState'),'touchend fallback did not finish drag'
 QTest.touchEvent(targetWidget,device).release(0,target,targetWidget).commit();QTest.qWait(250)
 # A second finger in Edit owns the gesture. Grid density changes immediately in discrete proportional steps.
 js('setEditMode(true);window.scrollTo(0,0)');QTest.qWait(500)
 p1=point('.pad-wrap[data-slot="sb2"]');p2=point('.pad-wrap[data-slot="sb0"]')
 columns=js('currentColumns')
 QTest.touchEvent(targetWidget,device).press(0,p1,targetWidget).press(1,p2,targetWidget).commit();QTest.qWait(100)
 assert js('!!pinchState'),'pinch did not start'
 QTest.touchEvent(targetWidget,device).move(0,QPoint(p1.x()-12,p1.y()),targetWidget).move(1,QPoint(p2.x()+14,p2.y()),targetWidget).commit();QTest.qWait(100)
 stepped=js('currentColumns')
 assert stepped != columns,'pinch step did not apply immediately'
 assert js('document.getElementById("padsGrid").style.gridTemplateColumns')==f'repeat({stepped}, minmax(0, 1fr))','live pinch did not repaint the real grid before release'
 assert abs(stepped-columns) >= 1,'pinch did not move by at least one column step'
 QTest.touchEvent(targetWidget,device).release(0,QPoint(p1.x()-12,p1.y()),targetWidget).release(1,QPoint(p2.x()+14,p2.y()),targetWidget).commit();QTest.qWait(300)
 assert not js('!!pinchState || !!dragState')
 assert js('currentColumns') == stepped,'pinch release changed the already-applied grid step'
 assert state('[...selectedSoundIds]')==[]
 # Five extra zoom-out levels are genuinely reachable in one normal proportional pinch.
 js('applyColumns(4,false)')
 assert js('currentColumns') == 4
 js('activePointers=new Map([[1,{x:0,y:0}],[2,{x:60,y:0}]]);pinchState={startDistance:120,startColumns:4};updatePinchColumns()')
 assert js('currentColumns') == 13,'proportional pinch did not reach the 13-column maximum'
 assert js('document.getElementById("padsGrid").style.gridTemplateColumns')=='repeat(13, minmax(0, 1fr))'
 # Reversing the same gesture is relative to the original pinch distance, so there is no accumulated pixel dead-zone.
 js('activePointers.set(2,{x:120,y:0});updatePinchColumns()')
 assert js('currentColumns') == 4
 js('activePointers.set(2,{x:152,y:0});updatePinchColumns()')
 assert js('currentColumns') == 1,'proportional pinch did not reach the 1-column minimum'
 js('pinchState=null;activePointers.clear()')
 # The option can explicitly allow the same live pinch ownership outside Edit.
 js('setEditMode(false);applyRemoteOption("remote_pinch_outside_edit",true)')
 assert js('pinchAllowed()')
 assert js('document.body.classList.contains("pinch-play-mode")')
 assert js('getComputedStyle(document.querySelector(".pad")).touchAction')=='none'
 js('applyRemoteOption("remote_pinch_outside_edit",false);setEditMode(true)')
 assert not js('document.body.classList.contains("pinch-play-mode")')
 # Edit mode swaps only through exactly two selected pads.
 before=state('padWraps().map(w=>w.dataset.slot)')
 js(f'toggleSoundSelection(document.querySelector(".pad-wrap[data-slot=\\"{before[0]}\\"]"));toggleSoundSelection(document.querySelector(".pad-wrap[data-slot=\\"{before[2]}\\"]"))')
 assert not js('document.getElementById("swapSelectedButton").disabled')
 click('#swapSelectedButton');waitfor('!orderSavePending')
 expected=before[:];expected[0],expected[2]=expected[2],expected[0]
 assert state('padWraps().map(w=>w.dataset.slot)')==expected
 assert state('[...selectedSoundIds]')==[]
 # Outside Edit, hold and drag swaps with the target and never plays the source pad.
 js('setEditMode(false);selectFolder("__all__");applyColumns(4,false);window.scrollTo(0,0)');QTest.qWait(600)
 before=state('padWraps().map(w=>w.dataset.slot)');plays=sum(c['command']=='soundboard-play' for c in commands)
 start=point('.pad-wrap[data-slot="'+before[0]+'"]');target=point('.pad-wrap[data-slot="'+before[2]+'"]')
 QTest.touchEvent(targetWidget,device).press(0,start,targetWidget).commit();QTest.qWait(470)
 assert js('dragState.active && !dragState.inserting'),'normal mode hold did not start swap'
 QTest.touchEvent(targetWidget,device).move(0,target,targetWidget).commit();QTest.qWait(200)
 assert js('!!document.querySelector(".swap-target")')
 if os.environ.get('KSH_QA_IMAGES'):
  QTest.qWait(500);view.grab().save(str(Path(os.environ['KSH_QA_IMAGES'])/'swap-preview.png'))
 QTest.touchEvent(targetWidget,device).release(0,target,targetWidget).commit();QTest.qWait(500)
 expected=before[:];expected[0],expected[2]=expected[2],expected[0]
 assert state('padWraps().map(w=>w.dataset.slot)')==expected
 assert sum(c['command']=='soundboard-play' for c in commands)==plays,'hold accidentally played a sound'
 # Removing from a real folder keeps the global library and other folder selections intact.
 js('setEditMode(true);selectFolder("__all__");window.scrollTo(0,0)');QTest.qWait(200)
 assert js('document.getElementById("removeFolderButton").hidden')
 js('selectFolder("Grouped")');QTest.qWait(100)
 click('.pad-wrap[data-slot="sb0"] .pad')
 assert not js('document.getElementById("removeFolderButton").hidden')
 click('#removeFolderButton')
 assert js('document.getElementById("moveFolderSelect").disabled')
 click('#confirmFolderMove');waitfor('!selectionMovePending')
 assert state('visiblePadWraps.map(w=>w.dataset.slot)')==['sb1','sb80']
 assert state('activeFolder')=='Grouped'
 saved=json.loads((config/'soundboard.json').read_text())
 assert next(s for s in saved['slots'] if s['id']=='sb0')['folder']==''
 assert len(saved['slots'])==160
 js('selectFolder("__all__")');assert js('visiblePadWraps.length')==160
 if os.environ.get('KSH_QA_IMAGES'):
  QTest.qWait(600);view.grab().save(str(Path(os.environ['KSH_QA_IMAGES'])/'edit-selection.png'))
 print('PASS: edit selection + insertion drag; normal-mode hold swaps without playback; folder removal preserves All',flush=True)
 print('PASS: manual touch scroll, pointercancel-safe held drag and live stepped pinch resize',flush=True)
 print('PASS: multi-select across folders, create and move, offline selection retained, edit-only actions, quick swipe cancellation, sticky folders, normal playback',flush=True)
finally:
 view.close();server.shutdown();server.server_close();tmp.cleanup()

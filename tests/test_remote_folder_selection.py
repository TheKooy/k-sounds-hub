"""Exercise the live-document bridge and authenticated HTTP confirmation."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from ksound_hub.soundboard_library import remote_folder_move

class Signal:
    def emit(self, *args): pass

class FolderMoveTests(unittest.TestCase):
    def setUp(self):
        self.document = {'folders':['Memes','Alerts'], 'volume':57, 'slots':[
            {'id':'a','path':'a.wav','folder':'Memes','trim_db':-3},
            {'id':'b','path':'b.wav','folder':'Alerts','trim_db':2},
            {'id':'c','path':'c.wav','folder':'','trim_db':0}]}

    def test_move_is_by_identity_preserving_live_order_and_properties(self):
        before=copy.deepcopy(self.document)
        self.document['slots'].reverse()
        updated,count,folder=remote_folder_move(self.document,['a','b'],'New folder')
        self.assertEqual(count,2)
        self.assertEqual([s['id'] for s in updated['slots']],['c','b','a'])
        self.assertEqual([s['trim_db'] for s in updated['slots']],[0,2,-3])
        self.assertEqual([s['folder'] for s in updated['slots']],['','New folder','New folder'])
        self.assertEqual(updated['volume'],57)
        self.assertEqual(before['slots'][0],self.document['slots'][2])
        self.assertEqual(updated['folders'].count('New folder'),1)

    def test_existing_and_main_destinations_are_canonical(self):
        updated,_,folder=remote_folder_move(self.document,['b'],'memes')
        self.assertEqual(folder,'Memes')
        self.assertEqual(updated['folders'],self.document['folders'])
        updated,count,folder=remote_folder_move(updated,['a','b'],'Main')
        self.assertEqual(folder,'')
        self.assertEqual(count,2)
        self.assertEqual(remote_folder_move(updated,['a','b'],'Main')[1],0)

    def test_stale_duplicate_and_invalid_requests_do_not_partially_move(self):
        before=copy.deepcopy(self.document)
        for ids,folder in [(['a','gone'],'New'),(['a','a'],'New'),([], 'New'),
                            ('a','New'),([{}],'New'),(['a'],'All'),(['a'],'x'*33),(['a'],'a\nb')]:
            with self.subTest(ids=ids,folder=folder), self.assertRaises(ValueError):
                remote_folder_move(self.document,ids,folder)
        self.assertEqual(before,self.document)

    def test_remove_checks_the_current_folder_and_preserves_all_sounds(self):
        before=copy.deepcopy(self.document)
        updated,count,_=remote_folder_move(self.document,['a'],'Main',expected_folder='Memes')
        self.assertEqual(count,1)
        self.assertEqual(len(updated['slots']),3)
        self.assertEqual(updated['slots'][0]['folder'],'')
        self.assertEqual(updated['slots'][0]['path'],'a.wav')
        with self.assertRaises(ValueError):
            remote_folder_move(self.document,['a','b'],'Main',expected_folder='Memes')
        with self.assertRaises(ValueError):
            remote_folder_move(self.document,['a'],'Main',expected_folder='__all__')
        self.assertEqual(before,self.document)

    def test_glass_bridge_uses_latest_service_document(self):
        tree=ast.parse((ROOT/'src/ksound_hub/glass_app.py').read_text())
        method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_handle_soundboard_command')
        namespace={'__package__':'ksound_hub'}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<glass-bridge>','exec'),namespace)
        owner=self
        class Service:
            writes=0
            def read_document(self): return owner.document
            def write_document(self,document): owner.document=document; self.writes+=1
        class Bridge:
            soundboard_service=Service()
            soundboard_changed=Signal()
            status_changed=Signal()
        bridge=Bridge()
        invoke=namespace['_handle_soundboard_command']
        self.document['slots'].reverse()
        invoke(bridge,{'ids':['a','b'],'folder':'Grouped'},'soundboard-move-to-folder')
        self.assertEqual(bridge.soundboard_service.writes,1)
        self.assertEqual(self.document['slots'][0]['id'],'c')
        self.assertEqual(self.document['slots'][0]['folder'],'')
        invoke(bridge,{'ids':['a','deleted'],'folder':'Bad'},'soundboard-move-to-folder')
        self.assertEqual(bridge.soundboard_service.writes,1)

    def test_http_requires_authentication_and_waits_for_saved_state(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            path=root/'soundboard.json'
            path.write_text(json.dumps(self.document))
            previous=os.environ.get('KSH_CONFIG_DIR')
            os.environ['KSH_CONFIG_DIR']=raw
            try:
                spec=importlib.util.spec_from_file_location('web_folder_test',ROOT/'tools/soundboard_remote/ksound_soundboard_web.py')
                web=importlib.util.module_from_spec(spec); spec.loader.exec_module(web)
            finally:
                if previous is None: os.environ.pop('KSH_CONFIG_DIR',None)
                else: os.environ['KSH_CONFIG_DIR']=previous
            calls=[]
            def save(payload):
                calls.append(payload)
                def later():
                    data,_,_=remote_folder_move(json.loads(path.read_text()),payload['ids'],payload['folder'])
                    path.write_text(json.dumps(data))
                threading.Timer(.10,later).start()
                return True,'queued'
            web.send_ipc=save
            server=web.ThreadingHTTPServer(('127.0.0.1',0),web.Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            def post(ids, folder='Grouped',token=web.TOKEN):
                request=Request(f'http://127.0.0.1:{server.server_port}/move-to-folder?token={token}',
                    data=urlencode({'ids':json.dumps(ids),'folder':folder}).encode())
                try:
                    with urlopen(request,timeout=4) as response: return response.status,json.load(response)
                except HTTPError as e:
                    return e.code,e.read().decode()
            try:
                self.assertEqual(post(['a'],token='wrong')[0],403)
                self.assertEqual(calls,[])
                self.assertEqual(post(['a','gone'])[0],409)
                self.assertEqual(calls,[])
                started=time.monotonic()
                code,result=post(['a','b'])
                self.assertEqual(code,200)
                self.assertGreaterEqual(time.monotonic()-started,.08)
                self.assertEqual(result['folder'],'Grouped')
                self.assertEqual([s['folder'] for s in result['state']['slots']],['Grouped','Grouped',''])
                web.send_ipc=lambda payload:(False,'unavailable')
                self.assertEqual(post(['a'],'Alerts')[0],503)
                web.send_ipc=lambda payload:(True,'queued but ignored')
                code,error=post(['a'],'Alerts')
                self.assertEqual(code,409)
                self.assertIn('not confirmed',error)
            finally:
                server.shutdown(); server.server_close()

if __name__=='__main__': unittest.main()

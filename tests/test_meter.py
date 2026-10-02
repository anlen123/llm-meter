import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import llm_meter as m


class MeterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = m.Store(self.root/'usage.db')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def log(self, records):
        path = self.root/'sessions'/'a.jsonl'
        path.parent.mkdir(exist_ok=True)
        path.write_text(''.join(json.dumps(r)+'\n' for r in records))

    def record(self, kind, payload):
        return {'timestamp':time.time(), 'type':kind, 'payload':payload}

    def test_codex_precise_and_legacy_do_not_double_count(self):
        base = [self.record('session_meta',{'id':'s','model_provider':'relay'}),
                self.record('turn_context',{'model':'m'})]
        legacy = self.record('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':100,'output_tokens':20}}})
        self.log(base+[legacy,legacy])
        m.sync_codex(self.store,self.root)
        self.assertEqual(sum(r['input_tokens'] for r in self.store.rows(1)),100)
        precise = self.record('token_usage_record',{'response_id':'r','usage':{'input_tokens':100,'output_tokens':20}})
        self.log(base+[precise,legacy,precise])
        m.sync_codex(self.store,self.root)
        m.sync_codex(self.store,self.root)
        rows = self.store.rows(1)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['source'],'codex:relay')
        self.assertEqual(rows[0]['input_tokens'],100)

    def test_legacy_delta_reset_and_null_info(self):
        records = [self.record('event_msg',{'type':'token_count','info':None})]
        for n in (100,100,180,30):
            records.append(self.record('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':n}}}))
        self.log(records)
        m.sync_codex(self.store,self.root)
        self.assertEqual(sum(r['input_tokens'] for r in self.store.rows(1)),210)
        self.assertEqual(len(self.store.rows(1)),3)

    def test_import_atomic_idempotent_and_export_shape(self):
        path=self.root/'input.jsonl'
        row={'id':'a','timestamp':time.time(),'model':'m','usage':{'prompt_tokens':10,'completion_tokens':3,'prompt_tokens_details':{'cached_tokens':4}}}
        path.write_text(json.dumps(row)+'\n')
        m.import_jsonl(self.store,path,'import:test')
        m.import_jsonl(self.store,path,'import:test')
        self.assertEqual(len(self.store.rows(1)),1)
        self.assertEqual(self.store.rows(1)[0]['cached_tokens'],4)
        path.write_text(json.dumps({**row,'id':'b'})+'\n{}\n')
        with self.assertRaises(ValueError): m.import_jsonl(self.store,path,'import:test')
        self.assertEqual(len(self.store.rows(1)),1)
        out=io.StringIO()
        with contextlib.redirect_stdout(out): m.main(['--db',str(self.store.path),'export'])
        exported=json.loads(out.getvalue())
        self.assertEqual(exported['usage']['input_tokens'],10)

    def test_sse_arbitrary_chunks_and_responses(self):
        data=b'data: {"response":{"model":"m","usage":{"input_tokens":17,"output_tokens":3}}}\r\n\r\ndata: [DONE]\n\n'
        obs=m.UsageObserver()
        for byte in data: obs.feed(bytes([byte]))
        obs.finish()
        self.assertEqual(obs.model,'m')
        self.assertEqual(obs.usage['input_tokens'],17)

    def test_openrouter_snapshot_replacement(self):
        from unittest.mock import patch
        def get(url,key):
            if url.endswith('/key'): return {'data':{'usage':1,'usage_daily':0.2,'label':'secret'}}
            return {'data':[{'date':time.strftime('%Y-%m-%d',time.gmtime()),'endpoint_id':'e','model':'m',
                             'prompt_tokens':40,'completion_tokens':10,'cached_tokens':5,'requests':2,'usage':0.1}]}
        with patch.object(m,'get_json',get):
            m.sync_openrouter(self.store,'key','management')
            m.sync_openrouter(self.store,'key','management')
        self.assertEqual(len(self.store.rows(1)),1)
        self.assertEqual(self.store.rows(1)[0]['requests'],2)
        snapshot=self.store.db.execute("SELECT data FROM snapshots WHERE source='openrouter:key'").fetchone()[0]
        self.assertNotIn('secret',snapshot)

    def test_proxy_nonstream_sse_missing_and_http_error(self):
        captured=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                captured.append((self.path,body,self.headers.get('Authorization')))
                if body['model']=='error':
                    data=b'{"error":{"message":"test"}}';code=429;kind='application/json'
                elif body.get('stream'):
                    data=b'data: {"model":"actual","usage":{"prompt_tokens":25,"completion_tokens":4}}\n\ndata: [DONE]\n\n';code=200;kind='text/event-stream'
                else:
                    data=json.dumps({'model':'actual','usage':{'input_tokens':20,'output_tokens':3}} if body['model']!='missing' else {'model':'actual'}).encode();code=200;kind='application/json'
                self.send_response(code);self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        proc=subprocess.Popen([sys.executable,str(Path(m.__file__)),'--db',str(self.store.path),'proxy',
                               '--upstream',f'http://127.0.0.1:{upstream.server_port}/v1','--port',str(port),
                               '--local-key-env','TEST_LOCAL'],env={**os.environ,'OPENAI_API_KEY':'upstream-test','TEST_LOCAL':'local-test'},
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1): break
                except OSError: time.sleep(.03)
            for model,stream in [('normal',False),('stream',True),('missing',False),('error',False)]:
                req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/chat/completions',
                     data=json.dumps({'model':model,'messages':[],'stream':stream}).encode(),
                     headers={'Authorization':'Bearer local-test','Content-Type':'application/json'})
                try:
                    with urllib.request.urlopen(req,timeout=5) as response: response.read()
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code,429)
                    exc.close()
            for _ in range(100):
                rows=self.store.rows(1)
                if len(rows)==4: break
                time.sleep(.02)
            self.assertEqual(len(rows),4)
            self.assertEqual(sum(r['input_tokens'] for r in rows),45)
            self.assertEqual({r['status'] for r in rows},{'ok','missing_usage','error'})
            self.assertTrue(captured[1][1]['stream_options']['include_usage'])
            self.assertTrue(all(r[2]=='Bearer upstream-test' for r in captured))
            self.assertTrue(all(r[0]=='/v1/chat/completions' for r in captured))
            req=urllib.request.Request(f'http://127.0.0.1:{port}/v1/models')
            with self.assertRaises(urllib.error.HTTPError) as ctx: urllib.request.urlopen(req,timeout=5)
            self.assertEqual(ctx.exception.code,401)
            ctx.exception.close()
        finally:
            proc.terminate();proc.communicate(timeout=5)
            upstream.shutdown();upstream.server_close();thread.join()


if __name__=='__main__': unittest.main()

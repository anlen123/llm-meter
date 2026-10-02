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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from llm_meter_connections import Connections,balance_url,extract_balance


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'connections.json'
        self.sites=Connections(self.path)
    def tearDown(self):self.temp.cleanup()
    def test_nullish_extractor_zero_false_and_fallbacks(self):
        self.assertEqual(extract_balance({'remaining':0,'balance':90,'is_active':False})['remaining'],0)
        self.assertFalse(extract_balance({'remaining':0,'is_active':False})['isValid'])
        self.assertEqual(extract_balance({'remaining':None,'quota':{'remaining':'12.5','unit':'CNY'}})['unit'],'CNY')
        self.assertEqual(extract_balance({'balance':-2,'isValid':False})['remaining'],-2)
        for row in ({},{'remaining':True},{'remaining':'NaN'},{'remaining':'bad'}):
            with self.assertRaises(ValueError):extract_balance(row)
    def test_currency_override_keeps_original_amount_and_unit(self):
        result=extract_balance({'remaining':'12.5','unit':'USD'},'CNY')
        self.assertEqual(result['remaining'],12.5)
        self.assertEqual(result['unit'],'CNY')
        self.assertEqual(result['reported_unit'],'USD')
        self.assertEqual(result['currency_source'],'configured')
        self.assertEqual(extract_balance({'remaining':8,'unit':'CNY'},'USD')['unit'],'USD')
        self.assertEqual(extract_balance({'remaining':8,'unit':'RMB'})['unit'],'RMB')
        with self.assertRaises(ValueError):extract_balance({'remaining':8},'BTC')
    def test_currency_is_persisted_and_old_configs_default_to_auto(self):
        data={'name':'x','provider':'x','base_url':'https://example.com','api_key':'secret','currency':'CNY'}
        ident=self.sites.save(data)
        self.assertEqual(Connections(self.path).get(ident)['currency'],'CNY')
        with patch('llm_meter_connections.get_json',return_value={'balance':100}):
            result=self.sites.query(ident)
        self.assertEqual(result['unit'],'CNY')
        self.assertEqual(result['remaining'],100)
        # Editing other fields without a currency field keeps the saved selection.
        self.sites.save({**{k:v for k,v in data.items() if k!='currency'},'id':ident})
        self.assertEqual(self.sites.get(ident)['currency'],'CNY')
        with self.assertRaises(ValueError):self.sites.save({**data,'currency':'BTC'})
        old=self.sites.get(ident);old.pop('currency')
        self.path.write_text(json.dumps({'connections':[old]}))
        loaded=Connections(self.path)
        with patch('llm_meter_connections.get_json',return_value={'balance':7,'unit':'CNY'}):
            self.assertEqual(loaded.query(ident)['unit'],'CNY')
    def test_openrouter_currency_is_usd(self):
        ident=self.sites.save({'name':'Router','provider':'openrouter','type':'openrouter','api_key':'key'})
        self.assertEqual(self.sites.get(ident)['currency'],'USD')
        with self.assertRaises(ValueError):
            self.sites.save({'name':'Router','provider':'openrouter','type':'openrouter','api_key':'key','currency':'CNY'})
    def test_config_persistence_secret_redaction_and_edit(self):
        ident=self.sites.save({'name':'AIHub','provider':'aihub','base_url':'https://example.com/v1',
                              'api_key':'secret-value','aliases':['my-aihub']})
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertNotIn('secret-value',json.dumps(self.sites.public()))
        self.assertTrue(self.sites.public()[0]['has_api_key'])
        self.assertEqual(Connections(self.path).get(ident)['api_key'],'secret-value')
        self.sites.save({'id':ident,'name':'Renamed','provider':'aihub','base_url':'https://example.com',
                         'api_key':'','aliases':[]})
        self.assertEqual(self.sites.get(ident)['api_key'],'secret-value')
        self.sites.delete(ident);self.assertEqual(self.sites.public(),[])
        self.assertEqual(Connections(self.path).public(),[])
    def test_base_path_and_openrouter_distinct_account_balance(self):
        self.assertEqual(balance_url('https://example.com/v1','/v1/usage'),'https://example.com/v1/usage')
        for base,path in [('https://user:secret@example.com','/v1/usage'),('https://example.com','//evil.example/usage')]:
            with self.assertRaises(ValueError):balance_url(base,path)
        ident=self.sites.save({'name':'OpenRouter','provider':'openrouter','type':'openrouter',
                              'api_key':'normal','management_key':'management'})
        calls=[]
        def fake(url,key):
            calls.append((url,key))
            if url.endswith('/key'):return {'data':{'limit_remaining':3,'usage_daily':2}}
            return {'data':{'total_credits':100,'total_usage':25}}
        with patch('llm_meter_connections.get_json',fake):balance=self.sites.query(ident)
        self.assertEqual(balance['remaining'],75)
        self.assertEqual(balance['key_limit_remaining'],3)
        self.assertEqual(calls[1][1],'management')
    def test_openrouter_normal_key_is_not_account_balance(self):
        ident=self.sites.save({'name':'OpenRouter','provider':'openrouter','type':'openrouter','api_key':'normal'})
        with patch('llm_meter_connections.get_json',return_value={'data':{'limit_remaining':3}}):
            result=self.sites.query(ident)
        self.assertIsNone(result['remaining']);self.assertEqual(result['key_limit_remaining'],3)
    def test_missing_balance_error_and_retains_last_success(self):
        ident=self.sites.save({'name':'x','provider':'x','base_url':'https://example.com','api_key':'secret'})
        with patch('llm_meter_connections.get_json',return_value={'balance':5}):self.sites.query(ident)
        with patch('llm_meter_connections.get_json',return_value={}):
            with self.assertRaises(ValueError):self.sites.query(ident)
        public=self.sites.public()[0]
        self.assertEqual(public['balance']['remaining'],5)
        self.assertTrue(public['last_error'])
        self.assertNotIn('secret',json.dumps(public))


class ConfiguredWebHTTPTests(unittest.TestCase):
    def test_save_query_edit_restart_and_delete_over_http(self):
        seen=[]
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                seen.append((self.path,self.headers.get('Authorization')))
                body=json.dumps({'remaining':0,'quota':{'remaining':99},'unit':'USD','is_active':False}).encode()
                self.send_response(200);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        thread=threading.Thread(target=upstream.serve_forever,daemon=True);thread.start()
        temp=tempfile.TemporaryDirectory()
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        proc=None
        def start():
            p=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'llm_meter.py'),
                                '--db',str(Path(temp.name)/'usage.db'),'web','--no-sync','--port',str(port)],
                               stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            for _ in range(100):
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):return p
                except OSError:time.sleep(.03)
            p.terminate();p.communicate(timeout=5);raise RuntimeError('server startup failed')
        def post(path,data):
            req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(data).encode(),
                                       headers={'Content-Type':'application/json','X-Meter-Request':'1'})
            with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)
        def get():
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/connections',timeout=5) as r:return json.load(r)
        try:
            proc=start()
            row={'name':'Test AIHub','provider':'aihub','base_url':f'http://127.0.0.1:{upstream.server_port}/v1',
                 'api_key':'test-secret','aliases':['my-relay'],'currency':'CNY'}
            saved=post('/api/connections/save',row);ident=saved['id']
            self.assertNotIn('test-secret',json.dumps(saved))
            queried=post('/api/connections/query',{'id':ident})
            self.assertEqual(queried['balance']['remaining'],0)
            self.assertEqual(queried['balance']['unit'],'CNY')
            self.assertEqual(queried['balance']['reported_unit'],'USD')
            self.assertFalse(queried['balance']['isValid'])
            self.assertEqual(seen,[('/v1/usage','Bearer test-secret')])
            post('/api/connections/save',{**row,'id':ident,'api_key':''})
            proc.terminate();proc.communicate(timeout=5);proc=start()
            self.assertTrue(get()['connections'][0]['has_api_key'])
            post('/api/connections/query',{'id':ident})
            self.assertEqual(seen[-1][1],'Bearer test-secret')
            post('/api/connections/delete',{'id':ident});self.assertEqual(get()['connections'],[])
        finally:
            if proc:proc.terminate();proc.communicate(timeout=5)
            upstream.shutdown();upstream.server_close();thread.join();temp.cleanup()


if __name__=='__main__':unittest.main()

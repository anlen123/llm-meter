import datetime as dt
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from llm_meter import load_env_file, Store, sync_pi_family, import_jsonl, sync_openrouter
from llm_meter_web import stats,parse_filters,summarize


class WebStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root/'usage.sqlite3')
    def tearDown(self):
        self.store.close();self.temp.cleanup()
    def test_pi_omp_cache_token_and_cost_semantics(self):
        session=self.root/'sessions'/'test.jsonl';session.parent.mkdir()
        entries=[{'type':'session','id':'s'},
                 {'type':'message','id':'a','timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),
                  'message':{'role':'assistant','model':'m','provider':'openrouter','stopReason':'stop',
                    'usage':{'input':10,'output':5,'cacheRead':80,'cacheWrite':20,'totalTokens':115,
                             'cost':{'total':.125}}}},
                 {'type':'message','message':{'role':'user','content':'must not be persisted'}}]
        session.write_text(''.join(json.dumps(r)+'\n' for r in entries))
        sync_pi_family(self.store,self.root,'pi');sync_pi_family(self.store,self.root,'pi')
        r=self.store.rows(1)[0]
        self.assertEqual(r['input_tokens'],110)
        self.assertEqual(r['cached_tokens'],80)
        self.assertEqual(r['cache_write_tokens'],20)
        self.assertEqual(r['estimated_cost'],.125)
        self.assertIsNone(r['cost'])
        self.assertEqual(r['agent'],'pi');self.assertEqual(r['provider'],'openrouter')
        self.assertEqual(r['dataset'],'local');self.assertEqual(len(self.store.rows(1)),1)
        self.assertNotIn('must not be persisted',str(r))
        # A copied branch with the same recorded call does not inflate usage.
        copy=session.with_name('copy.jsonl');copy.write_text(session.read_text())
        sync_pi_family(self.store,self.root,'pi');self.assertEqual(len(self.store.rows(1)),1)
        sync_pi_family(self.store,self.root,'omp');self.assertEqual(len(self.store.rows(1)),2)
    def test_filter_aggregation_costs_and_provider_alias(self):
        for agent,provider,model,tokens,cost,estimate in [('pi','custom-a','m1',100,None,.1),
                    ('omp','openrouter','m2',300,.3,.4),('codex','custom-a','m1',600,None,None)]:
            self.store.put(agent+':'+provider,agent,time.time(),model,{'input_tokens':tokens},
                           agent=agent,provider=provider,dataset='local',cost=cost,estimated_cost=estimate)
        self.store.put('proxy:test','overlap',time.time(),'m1',{'input_tokens':1000},agent='pi')
        self.store.db.commit()
        result=stats(self.store,parse_filters({}),{'custom-a':'aihub'})
        self.assertEqual(result['summary']['tokens'],1000)
        self.assertAlmostEqual(sum(r['share'] for r in result['groups']),1)
        self.assertEqual(result['summary']['cost'],.3)
        self.assertEqual(result['summary']['estimated_cost'],.5)
        self.assertEqual(result['summary']['cost_records'],1)
        self.assertEqual(result['options']['provider'],['aihub','openrouter'])
        filtered=stats(self.store,parse_filters({'agent':['pi'],'provider':['aihub'],'model':['m1']}),{'custom-a':'aihub'})
        self.assertEqual(filtered['summary']['tokens'],100)
        billed=stats(self.store,parse_filters({'metric':['cost']}))
        self.assertEqual([r['name'] for r in billed['groups'] if r['value']],['omp'])
        estimated=stats(self.store,parse_filters({'metric':['cost'],'money':['estimated']}))
        self.assertEqual(estimated['groups'][0]['name'],'omp')
        self.assertEqual(estimated['groups'][0]['value'],.4)
    def test_unknown_agent_and_custom_date_range(self):
        self.store.put('openrouter:activity:account','r',time.time(),'m',{'input_tokens':10},requests=3,cost=.2)
        self.store.db.commit()
        result=stats(self.store,parse_filters({'dataset':['remote']}))
        self.assertEqual(result['summary']['unknown_agent'],3)
        self.assertEqual(result['groups'][0]['name'],'unknown')
        for query in ({'start':['2026-10-02'],'end':['2026-10-01']},{'group':['invalid']},
                      {'metric':['invalid']},{'money':['combined']},{'start':['invalid']},
                      {'start':['2020-01-01'],'end':['2026-01-01']}):
            with self.assertRaises(ValueError):parse_filters(query)
    def test_schema_migration_preserves_events(self):
        p=self.root/'old.db';db=sqlite3.connect(p)
        db.execute('CREATE TABLE events (source TEXT,id TEXT,ts REAL,model TEXT,requests INTEGER,input_tokens INTEGER,output_tokens INTEGER,cached_tokens INTEGER,reasoning_tokens INTEGER,cost REAL,status TEXT,PRIMARY KEY(source,id))')
        db.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)',('codex:relay','old',time.time(),'m',1,3,4,1,0,None,'ok'))
        db.commit();db.close()
        store=Store(p)
        try:
            row=store.rows(1)[0]
            self.assertEqual(row['agent'],'codex');self.assertEqual(row['provider'],'relay')
            self.assertEqual(row['dataset'],'local');self.assertEqual(row['input_tokens'],3)
            self.assertIsNone(row['estimated_cost'])
        finally:store.close()
    def test_import_preserves_dimensions_and_estimates(self):
        path=self.root/'import.jsonl'
        row={'timestamp':time.time(),'id':'r','agent':'omp','provider':'aihub','model':'m',
             'usage':{'input_tokens':7},'estimated_cost':.25,'cache_write_tokens':2}
        path.write_text(json.dumps(row)+'\n')
        import_jsonl(self.store,path,'import:custom')
        r=self.store.rows(1)[0]
        self.assertEqual((r['agent'],r['provider'],r['dataset']),('omp','aihub','import'))
        self.assertEqual(r['estimated_cost'],.25);self.assertEqual(r['cache_write_tokens'],2)
    def test_unit_cost_uses_only_corresponding_priced_tokens(self):
        self.store.put('import:test','priced',time.time(),'m',{'input_tokens':2_000_000},cost=4,estimated_cost=6)
        self.store.put('import:test','unknown',time.time(),'m',{'input_tokens':8_000_000})
        self.store.put('import:test','no_tokens',time.time(),'m',{},cost=100)
        self.store.db.commit()
        result=summarize(self.store.rows(1))
        self.assertEqual(result['cost'],104)
        self.assertEqual(result['cost_covered_tokens'],2_000_000)
        self.assertEqual(result['cost_per_million'],2)
        self.assertEqual(result['estimated_cost_per_million'],3)
        self.assertEqual(result['cost_priced_records'],1)
        empty=summarize([])
        self.assertIsNone(empty['cost_per_million'])
    def test_machine_source_prefix_preserves_namespaces(self):
        path=self.root/'remote.jsonl'
        rows=[{'source':source,'id':'same-id','timestamp':time.time(),'agent':'pi',
               'provider':'openrouter','model':'m','usage':{'input_tokens':5}} for source in ('pi:openrouter','omp:openrouter')]
        path.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        import_jsonl(self.store,path,source_prefix='server-a')
        import_jsonl(self.store,path,source_prefix='server-a')
        import_jsonl(self.store,path,source_prefix='laptop-b')
        records=self.store.rows(1)
        self.assertEqual(len(records),4)
        self.assertTrue(all(r['dataset']=='import' for r in records))
        self.assertEqual(len({r['source'] for r in records}),4)
        self.assertIn('import:server-a:pi:openrouter',{r['source'] for r in records})
    def test_env_file_literal_values_override_and_no_shell_evaluation(self):
        from unittest.mock import patch
        path=self.root/'meter.env'
        path.write_text("# config\nMETER_TEST_KEY='literal $HOME $(echo bad)'\nexport METER_TEST_EMPTY=\nMETER_TEST_NAME=abc # comment\n")
        with patch.dict(os.environ,{'METER_TEST_KEY':'old'}):
            load_env_file(path)
            self.assertEqual(os.environ['METER_TEST_KEY'],'literal $HOME $(echo bad)')
            self.assertEqual(os.environ['METER_TEST_EMPTY'],'')
            self.assertEqual(os.environ['METER_TEST_NAME'],'abc')
        path.write_text('broken line\n')
        with self.assertRaises(ValueError):load_env_file(path)

    def test_remote_agent_mapping_requires_dedicated_key(self):
        with self.assertRaises(ValueError):sync_openrouter(self.store,None,'management',agent='pi')


class WebHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sock=socket.socket();sock.bind(('127.0.0.1',0));cls.port=sock.getsockname()[1];sock.close()
        cls.proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'llm_meter.py'),
                                  'web','--demo','--port',str(cls.port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1',cls.port),timeout=.1):return
            except OSError:time.sleep(.03)
        cls.proc.terminate();out,err=cls.proc.communicate(timeout=5)
        raise RuntimeError('Web server did not start: '+out+err)
    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate();cls.proc.communicate(timeout=5)
    def get(self,path,headers=None):
        return urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{self.port}'+path,headers=headers or {}),timeout=5)
    def test_static_api_and_agent_filter(self):
        with self.get('/') as r:self.assertIn('用量观察台',r.read().decode())
        with self.get('/api/stats?dataset=demo',{'Host':'127.0.0.1:18765'}) as r:
            self.assertEqual(r.code,200)  # SSH tunnel can use a different local port.
        with self.get('/app.js') as r:self.assertIn('drawDonut',r.read().decode())
        with self.get('/api/stats?dataset=demo&group=model&agent=pi') as r:
            d=json.load(r)
            self.assertEqual(d['options']['agent'],['codex','omp','pi'])
            self.assertTrue(d['summary']['tokens']>0)
            self.assertTrue(all(r['agent']=='pi' for r in d['recent']))
            self.assertEqual(d['filters']['group'],'model')
            self.assertNotIn('OPENAI_API_KEY',json.dumps(d))
        with self.get('/api/export?dataset=demo&agent=pi') as r:
            data=r.read().decode('utf-8-sig')
            self.assertIn('estimated_cost',data.splitlines()[0])
            self.assertIn('pi',data)
    def test_rejects_host_and_cross_origin_mutations(self):
        with self.assertRaises(urllib.error.HTTPError) as e:self.get('/api/stats',{'Host':'evil.example'})
        self.assertEqual(e.exception.code,403);e.exception.close()
        for headers in ({'Content-Type':'application/json'},
                        {'Content-Type':'application/json','X-Meter-Request':'1','Origin':'https://evil.example'}):
            req=urllib.request.Request(f'http://127.0.0.1:{self.port}/api/sync',data=b'{}',headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(req,timeout=5)
            self.assertEqual(e.exception.code,403);e.exception.close()
        req=urllib.request.Request(f'http://127.0.0.1:{self.port}/api/sync',data=b'{}',
                                   headers={'Content-Type':'application/json','X-Meter-Request':'1'})
        with urllib.request.urlopen(req,timeout=5) as r:self.assertEqual(r.code,200)


if __name__=='__main__':unittest.main()

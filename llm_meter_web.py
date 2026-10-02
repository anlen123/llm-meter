"""Local web dashboard; no external runtime dependencies or third-party CDN."""
import collections
import csv
import datetime as dt
import io
import json
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from llm_meter_codex import read_codex_limits
from llm_meter_connections import Connections
from llm_meter import Store, sync_codex, sync_pi_family, sync_openrouter

STATIC = Path(__file__).with_name('meter_static')
DIMENSIONS = ('agent','provider','model','source')
DATASETS = ('local','proxy','remote','import','demo')


def seeded_demo(store):
    rng = random.Random(72)
    today = dt.datetime.now().date()
    for i in range(30):
        date = today - dt.timedelta(days=i)
        for agent in ('pi','omp','codex'):
            for provider in ('aihub','openrouter'):
                for j in range(rng.randint(2,5)):
                    model = rng.choice(('anthropic/claude-sonnet-4.5','openai/gpt-5','deepseek/deepseek-chat'))
                    inp = rng.randint(7000,70000)
                    out = rng.randint(350,3500)
                    read = int(inp*rng.uniform(.15,.8))
                    ts = dt.datetime.combine(date,dt.time(rng.randint(0,23),rng.randint(0,59))).timestamp()
                    ts = min(ts,time.time()-rng.randint(1,100))
                    billed = (inp-read)*2/1e6+read*.2/1e6+out*8/1e6
                    store.put('demo:'+agent+':'+provider,f'{i}:{agent}:{provider}:{j}',ts,model,
                              {'input_tokens':inp,'output_tokens':out,'cached_tokens':read},
                              cost=billed if j%4 else None,estimated_cost=billed*1.12,
                              agent=agent,provider=provider,dataset='demo')
    store.snapshot('demo:notice',{'note':'演示数据，非真实调用或价格'})
    store.db.commit()


def parse_filters(query):
    one = lambda key, default=None: query.get(key,[default])[0]
    end = dt.date.fromisoformat(one('end',dt.date.today().isoformat()))
    start = dt.date.fromisoformat(one('start',(end-dt.timedelta(days=13)).isoformat()))
    if end < start or (end-start).days>365:
        raise ValueError('日期范围必须为 1–366 天，开始日期不能晚于结束日期')
    dataset = one('dataset','local')
    if dataset != 'all' and dataset not in DATASETS:
        raise ValueError('无效的采集来源')
    group = one('group','agent')
    if group not in DIMENSIONS: raise ValueError('无效的分组维度')
    metric = one('metric','tokens')
    if metric not in ('tokens','input_tokens','output_tokens','cached_tokens','requests','cost'):
        raise ValueError('无效的指标')
    money = one('money','billed')
    if money not in ('billed','estimated'): raise ValueError('无效的费用类型')
    return {'start':start,'end':end,'dataset':dataset,'group':group,'metric':metric,'money':money,
            **{k:one(k) or None for k in DIMENSIONS}}


def selected_rows(store, filters):
    sql = 'SELECT * FROM events WHERE ts>=? AND ts<? AND ts<=?'
    params = [dt.datetime.combine(filters['start'],dt.time()).timestamp(),
              dt.datetime.combine(filters['end']+dt.timedelta(days=1),dt.time()).timestamp(),time.time()]
    if filters['dataset']!='all':
        sql += ' AND dataset=?';params.append(filters['dataset'])
    for key in DIMENSIONS:
        if filters[key]: sql += f' AND {key}=?';params.append(filters[key])
    return [dict(r) for r in store.db.execute(sql+' ORDER BY ts DESC',params)]


def summarize(rows):
    sums = {k:sum(r[k] for r in rows) for k in ('requests','input_tokens','output_tokens','cached_tokens','reasoning_tokens','cache_write_tokens')}
    sums['tokens'] = sums['input_tokens']+sums['output_tokens']
    for field in ('cost','estimated_cost'):
        known = [r for r in rows if r[field] is not None]
        sums[field] = sum(r[field] for r in known)
        sums[field+'_records'] = len(known)
        # Compare only the same records with both known fee and positive token usage.
        priced = [r for r in known if r['input_tokens']+r['output_tokens']>0 and r['status']!='missing_usage']
        covered_tokens = sum(r['input_tokens']+r['output_tokens'] for r in priced)
        sums[field+'_covered_tokens'] = covered_tokens
        sums[field+'_priced_records'] = len(priced)
        sums[field+'_per_million'] = sum(r[field] for r in priced)/covered_tokens*1_000_000 if covered_tokens else None
    sums['records']=len(rows)
    sums['errors']=sum(r['requests'] for r in rows if r['status']=='error')
    sums['missing_usage']=sum(r['requests'] for r in rows if r['status']=='missing_usage')
    sums['unknown_agent']=sum(r['requests'] for r in rows if r['agent']=='unknown')
    return sums


def metric_value(row, filters):
    key = filters['metric']
    if key=='tokens': return row['input_tokens']+row['output_tokens']
    if key=='cost': return row['cost' if filters['money']=='billed' else 'estimated_cost'] or 0
    return row[key]


def aggregate(rows, dimension, filters):
    groups=collections.defaultdict(list)
    for row in rows: groups[row[dimension]].append(row)
    result=[]
    total=sum(metric_value(r,filters) for r in rows)
    for name,items in groups.items():
        value=sum(metric_value(r,filters) for r in items)
        result.append({'name':name,'value':value,'share':value/total if total else 0,**summarize(items)})
    return sorted(result,key=lambda r:(-r['value'],r['name']))


def stats(store, filters, provider_map=None):
    # Alias mapping changes only presentation and filter values; never rewrites raw records.
    raw_filters=dict(filters)
    raw_filters['provider']=None
    rows=selected_rows(store,raw_filters)
    mapping=provider_map or {}
    for row in rows:
        row['provider']=mapping.get(row['provider'],row['provider'])
    if filters['provider']:rows=[r for r in rows if r['provider']==filters['provider']]
    dates={}
    day=filters['start']
    while day<=filters['end']:
        dates[day.isoformat()]=[];day+=dt.timedelta(days=1)
    for row in rows:
        dates[dt.datetime.fromtimestamp(row['ts']).date().isoformat()].append(row)
    series=[{'date':day,'value':sum(metric_value(r,filters) for r in items),**summarize(items)} for day,items in dates.items()]
    options={k:set() for k in DIMENSIONS}
    for row in store.db.execute('SELECT DISTINCT agent,provider,model,source FROM events'+
                               (' WHERE dataset=?' if filters['dataset']!='all' else ''),
                               (filters['dataset'],) if filters['dataset']!='all' else ()):
        for key in DIMENSIONS: options[key].add(mapping.get(row[key],row[key]) if key=='provider' else row[key])
    snapshots=[{'source':r['source'],'timestamp':r['ts'],'data':json.loads(r['data'])} for r in store.db.execute('SELECT * FROM snapshots ORDER BY source')]
    return {'summary':summarize(rows),'series':series,'groups':aggregate(rows,filters['group'],filters),
            'provider_groups':aggregate(rows,'provider',filters),
            'options':{k:sorted(v) for k,v in options.items()},'recent':rows[:50],
            'snapshots':snapshots,'filters':{k:v.isoformat() if isinstance(v,dt.date) else v for k,v in filters.items()},
            'timezone':str(dt.datetime.now().astimezone().tzinfo),'updated_at':time.time()}


def run_web(args):
    if args.host not in ('127.0.0.1','localhost'):
        raise ValueError('网页服务仅监听本机；远程访问请使用 SSH 隧道')
    aliases={}
    if args.provider_map:
        aliases=json.loads(Path(args.provider_map).expanduser().read_text())
        if not isinstance(aliases,dict) or not all(isinstance(k,str) and isinstance(v,str) and v for k,v in aliases.items()):
            raise ValueError('--provider-map 必须是 provider 名称到显示名称的 JSON 对象')
    temp=tempfile.TemporaryDirectory(prefix='llm-meter-web-') if args.demo else None
    db_path=str(Path(temp.name)/'demo.sqlite3') if temp else str(Path(args.db).expanduser().resolve())
    config_path=str(Path(db_path).with_suffix('.connections.json')) if args.demo or not args.connections_file else args.connections_file
    sites=Connections(Path(config_path).expanduser())
    def active_aliases():
        return {**aliases,**sites.aliases()}
    store=Store(db_path)
    if args.demo: seeded_demo(store)
    store.close()
    lock=threading.Lock()
    state={'last_sync':0,'warnings':[]}

    def sync_local(force=False):
        with lock:
            if args.demo or args.no_sync: return
            if not force and time.time()-state['last_sync']<15: return
            store=Store(db_path)
            try:
                warnings=[]
                _,w=sync_codex(store,args.codex_home);warnings+=w
                for agent,home in (('pi',args.pi_home),('omp',args.omp_home)):
                    _,w=sync_pi_family(store,home,agent);warnings+=w
                state['warnings']=warnings[:10]
                state['last_sync']=time.time()
            finally: store.close()
    sync_local()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_): pass
        def send(self, status, body, mime='application/json; charset=utf-8'):
            if isinstance(body,(dict,list)): body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
            if isinstance(body,str):body=body.encode()
            self.send_response(status)
            self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Referrer-Policy','no-referrer')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def valid_host(self):
            host=self.headers.get('Host','')
            match=re.fullmatch(r'(127\.0\.0\.1|localhost):([0-9]{1,5})',host)
            return bool(match and 1<=int(match[2])<=65535)
        def do_GET(self):
            if not self.valid_host():return self.send(403,{'error':'Invalid Host'})
            url=urllib.parse.urlsplit(self.path)
            try:
                if url.path=='/api/stats':
                    sync_local()
                    filters=parse_filters(urllib.parse.parse_qs(url.query))
                    store=Store(db_path)
                    try:
                        data=stats(store,filters,active_aliases())
                        data['demo']=args.demo
                        data['warnings']=list(state['warnings'])
                        selected_site=sites.first_openrouter()
                        data['connections']={'openrouter_key':bool(selected_site.get('api_key') if selected_site else os.environ.get('OPENROUTER_API_KEY')),
                                             'openrouter_management':bool(selected_site.get('management_key') if selected_site else os.environ.get('OPENROUTER_MANAGEMENT_KEY')),
                                             'local_sync':not args.no_sync,'last_local_sync':state['last_sync']}
                        self.send(200,data)
                    finally:store.close()
                elif url.path=='/api/connections':
                    self.send(200,{'connections':sites.public(),'demo':args.demo})
                elif url.path=='/api/export':
                    filters=parse_filters(urllib.parse.parse_qs(url.query))
                    store=Store(db_path)
                    try:
                        rows=selected_rows(store,{**filters,'provider':None})
                        for r in rows:r['provider']=active_aliases().get(r['provider'],r['provider'])
                        if filters['provider']:rows=[r for r in rows if r['provider']==filters['provider']]
                        fields=('id','source','agent','provider','model','dataset','timestamp','requests','input_tokens',
                                'output_tokens','cached_tokens','cache_write_tokens','reasoning_tokens','cost','estimated_cost','status')
                        output=io.StringIO();writer=csv.DictWriter(output,fieldnames=fields);writer.writeheader()
                        for r in rows:
                            r['timestamp']=dt.datetime.fromtimestamp(r['ts'],dt.timezone.utc).isoformat()
                            # Protect spreadsheet formulas from model/provider names in untrusted logs.
                            for k,v in r.items():
                                if isinstance(v,str) and v[:1] in ('=','+','-','@','\t','\r'):r[k]="'"+v
                            writer.writerow({k:r[k] for k in fields})
                        self.send(200,'\ufeff'+output.getvalue(),'text/csv; charset=utf-8')
                    finally:store.close()
                else:
                    files={'/':('index.html','text/html; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),
                           '/style.css':('style.css','text/css; charset=utf-8')}
                    if url.path not in files:return self.send(404,{'error':'Not found'})
                    name,mime=files[url.path];self.send(200,(STATIC/name).read_bytes(),mime)
            except (ValueError,TypeError) as exc:self.send(400,{'error':str(exc)})
            except Exception:
                self.send(500,{'error':'读取统计失败，请查看数据库路径和日志目录权限'})
        def do_POST(self):
            if not self.valid_host():return self.send(403,{'error':'Invalid Host'})
            origin=self.headers.get('Origin')
            if origin and origin != 'http://'+self.headers.get('Host',''):
                return self.send(403,{'error':'Invalid Origin'})
            if self.headers.get('X-Meter-Request')!='1' or self.headers.get('Content-Type')!='application/json':
                return self.send(403,{'error':'Missing same-origin request header'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if length<0 or length>32768:raise ValueError('请求体过大')
                body=json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body,dict):raise ValueError('请求体必须是 JSON 对象')
                if self.path=='/api/connections/save':
                    ident=sites.save(body)
                    self.send(200,{'message':'站点配置已保存','id':ident,'connections':sites.public()})
                elif self.path=='/api/connections/delete':
                    sites.delete(body.get('id'))
                    self.send(200,{'message':'站点配置已删除；已有用量记录保留','connections':sites.public()})
                elif self.path=='/api/connections/query':
                    if args.demo:raise ValueError('演示模式不连接真实服务；请在普通模式查询余额')
                    self.send(200,{'message':'余额已更新','balance':sites.query(body.get('id'))})
                elif self.path=='/api/connections/history':
                    if args.demo:raise ValueError('演示模式不查询远端历史')
                    item=sites.get(body.get('id'))
                    if item['type']!='openrouter':raise ValueError('当前仅 OpenRouter 支持远端模型历史')
                    with lock:
                        store=Store(db_path)
                        try:warnings=sync_openrouter(store,item.get('api_key'),item.get('management_key'),namespace=item['id'])
                        finally:store.close()
                    self.send(200,{'message':'OpenRouter 历史已同步','warnings':warnings})
                elif self.path=='/api/codex/limits':
                    if args.demo:raise ValueError('演示模式不查询真实 Codex 额度')
                    with lock:
                        result=read_codex_limits(args.codex_home)
                        store=Store(db_path)
                        try:store.snapshot('codex:live',result);store.db.commit()
                        finally:store.close()
                    self.send(200,{'message':'Codex 实时额度已更新'})
                elif self.path=='/api/sync':
                    sync_local(True)
                    self.send(200,{'message':'本地会话已同步','warnings':state['warnings']})
                elif self.path=='/api/sync/openrouter':
                    if args.demo:return self.send(200,{'message':'演示模式，无需同步'})
                    selected=sites.first_openrouter()
                    key,management=(selected.get('api_key'),selected.get('management_key')) if selected else (os.environ.get('OPENROUTER_API_KEY'),os.environ.get('OPENROUTER_MANAGEMENT_KEY'))
                    if not key and not management:raise ValueError('请点击配置站点填写 OpenRouter key，或设置服务端环境变量')
                    with lock:
                        store=Store(db_path)
                        try:warnings=sync_openrouter(store,key,management,body.get('key_hash'),body.get('agent'),namespace=selected['id'] if selected else None)
                        finally:store.close()
                    self.send(200,{'message':'OpenRouter 已同步','warnings':warnings})
                else:self.send(404,{'error':'Not found'})
            except (ValueError,TypeError) as exc:self.send(400,{'error':str(exc)})
            except Exception as exc:
                from urllib.error import HTTPError
                message=f'OpenRouter HTTP {exc.code}，请检查 key 类型和权限' if isinstance(exc,HTTPError) else '同步失败，请检查网络和目录权限'
                self.send(502,{'error':message})
    server=ThreadingHTTPServer((args.host,args.port),Handler)
    print(f'网页看板：http://127.0.0.1:{server.server_port}'+(' （演示模式）' if args.demo else ''),flush=True)
    print('仅本机访问；Ctrl-C 停止。',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        if temp:temp.cleanup()

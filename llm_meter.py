#!/usr/bin/env python3
"""Local-first terminal meter for Codex and OpenAI-compatible APIs. Python 3.10+."""
import argparse
import collections
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import shlex
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UTC = dt.timezone.utc
FIELDS = ('input_tokens', 'output_tokens', 'cached_tokens', 'reasoning_tokens')


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def number(value):
    n = float(value or 0)
    if not math.isfinite(n) or n < 0:
        raise ValueError('用量必须是非负有限数值')
    return n


def normalize(usage):
    return dict(input_tokens=int(number(usage.get('input_tokens', usage.get('prompt_tokens')))),
                output_tokens=int(number(usage.get('output_tokens', usage.get('completion_tokens')))),
                cached_tokens=int(number(usage.get('cached_input_tokens', usage.get('cached_tokens',
                    (usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}).get('cached_tokens'))))),
                reasoning_tokens=int(number(usage.get('reasoning_output_tokens', usage.get('reasoning_tokens',
                    (usage.get('output_tokens_details') or usage.get('completion_tokens_details') or {}).get('reasoning_tokens'))))))


def load_env_file(path):
    """Load literal KEY=value configuration, without shell evaluation or expansion."""
    values = {}
    for index, line in enumerate(Path(path).expanduser().read_text(encoding='utf-8').splitlines(),1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '): line = line[7:].lstrip()
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key):
            raise ValueError(f'配置文件第 {index} 行格式无效，应为 NAME=value')
        try:
            parts = shlex.split(value,comments=True,posix=True)
        except ValueError:
            raise ValueError(f'配置文件第 {index} 行引号不匹配') from None
        if len(parts)>1:
            raise ValueError(f'配置文件第 {index} 行的值含空格时请加引号')
        values[key] = parts[0] if parts else ''
    os.environ.update(values)


class Store:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS events (
                source TEXT, id TEXT, ts REAL, model TEXT, requests INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                reasoning_tokens INTEGER, cost REAL, status TEXT,
                PRIMARY KEY(source,id));
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS snapshots (source TEXT PRIMARY KEY, ts REAL, data TEXT);
            CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, signature TEXT);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(events)')}
        additions = {'agent': "TEXT NOT NULL DEFAULT 'unknown'", 'provider': "TEXT NOT NULL DEFAULT 'unknown'",
                     'dataset': "TEXT NOT NULL DEFAULT 'import'", 'estimated_cost': 'REAL',
                     'cache_write_tokens': 'INTEGER NOT NULL DEFAULT 0'}
        needs_backfill = any(name not in columns for name in additions)
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE events ADD COLUMN {name} {definition}')
        # Backfill safe dimensions on databases created by version 0.1.
        if needs_backfill:
            self.db.execute("UPDATE events SET agent='codex',dataset='local',provider=substr(source,7) WHERE source LIKE 'codex:%'")
            self.db.execute("UPDATE events SET dataset='proxy',provider=substr(source,7) WHERE source LIKE 'proxy:%' AND provider='unknown'")
            self.db.execute("UPDATE events SET dataset='remote',provider='openrouter' WHERE source LIKE 'openrouter:activity:%'")
        self.db.commit()

    def put(self, source, ident, ts, model, usage=None, requests=1, cost=None, status='ok',
            agent=None, provider=None, dataset=None, estimated_cost=None, cache_write_tokens=0):
        u = normalize(usage or {})
        if cost is not None:
            cost = number(cost)
        if estimated_cost is not None:
            estimated_cost = number(estimated_cost)
        if source.startswith('codex:'):
            agent, provider, dataset = 'codex', provider or source[6:], 'local'
        elif source.startswith('proxy:'):
            provider, dataset = provider or source[6:], 'proxy'
        elif source.startswith('openrouter:activity:'):
            provider, dataset = 'openrouter', 'remote'
        self.db.execute("""INSERT OR REPLACE INTO events
            (source,id,ts,model,requests,input_tokens,output_tokens,cached_tokens,reasoning_tokens,cost,status,
             agent,provider,dataset,estimated_cost,cache_write_tokens) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, str(ident), timestamp(ts), str(model or 'unknown'), int(number(requests)),
             *(u[k] for k in FIELDS), cost, status, agent or 'unknown', provider or 'unknown',
             dataset or 'import', estimated_cost, int(number(cache_write_tokens))))

    def snapshot(self, source, data, ts=None):
        self.db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?)',
                        (source, ts or time.time(), json.dumps(data, ensure_ascii=False)))

    def rows(self, days, source=None, model=None):
        end = time.time()
        # Include exactly N calendar days, today included (local timezone).
        start = dt.datetime.combine(dt.datetime.now().date() - dt.timedelta(days=days-1), dt.time()).timestamp()
        sql, params = 'SELECT * FROM events WHERE ts>=? AND ts<=?', [start, end]
        if source:
            sql += ' AND source=?'; params.append(source)
        if model:
            sql += ' AND model=?'; params.append(model)
        return [dict(r) for r in self.db.execute(sql + ' ORDER BY ts', params)]

    def close(self):
        self.db.close()


def sync_codex(store, home):
    home = Path(home).expanduser()
    added, warnings = 0, []
    paths = set(home.glob('sessions/**/*.jsonl')) | set(home.glob('archived_sessions/**/*.jsonl'))
    for path in sorted(paths):
        stat = path.stat()
        signature = f'{stat.st_mtime_ns}:{stat.st_size}'
        old = store.db.execute('SELECT signature FROM files WHERE path=?', (str(path),)).fetchone()
        if old and old[0] == signature:
            continue
        records = []
        try:
            with path.open(encoding='utf-8') as f:
                for ordinal, line in enumerate(f):
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict) and record.get('type') in ('session_meta', 'turn_context', 'token_usage_record', 'event_msg'):
                            if record.get('type') != 'event_msg' or (record.get('payload') or {}).get('type') == 'token_count':
                                record['_line'] = ordinal
                                records.append(record)
                    except (ValueError, UnicodeError):
                        pass  # Active log may end with a partial line; reread when changed.
        except OSError as exc:
            warnings.append(f'{path.name}: {exc.strerror}'); continue
        has_precise = any(r.get('type') == 'token_usage_record' for r in records)
        model, provider, session = 'unknown', 'openai', path.stem
        previous = None
        for r in records:
            index = r['_line']
            p = r.get('payload') or {}
            kind = r.get('type')
            if kind == 'session_meta':
                provider = p.get('model_provider') or provider
                session = p.get('id') or p.get('session_id') or session
            if kind == 'turn_context':
                model = p.get('model') or model
                provider = p.get('model_provider') or provider
            ts = r.get('timestamp')
            if not ts:
                continue
            source = 'codex:' + provider
            try:
                if kind == 'event_msg' and p.get('type') == 'token_count':
                    if p.get('rate_limits'):
                        latest = store.db.execute('SELECT ts FROM snapshots WHERE source=?', (source,)).fetchone()
                        if not latest or timestamp(ts) >= latest[0]:
                            store.snapshot(source, {'rate_limits': p['rate_limits']}, timestamp(ts))
                    if has_precise:
                        continue
                    info = p.get('info') or {}
                    total = info.get('total_token_usage')
                    if total:
                        current = normalize(total)
                        if previous == current:
                            continue
                        usage = {k: current[k] - (previous or {}).get(k, 0) for k in FIELDS}
                        if any(v < 0 for v in usage.values()):
                            usage = normalize(info.get('last_token_usage') or total)
                        previous = current
                    else:
                        usage = info.get('last_token_usage')
                    if usage:
                        store.put(source, f'{session}:legacy:{index}', ts, model, usage)
                        added += 1
                if kind == 'token_usage_record' and p.get('usage') is not None:
                    # Migrating a file from legacy events to precise records must remove old rows.
                    store.db.execute('DELETE FROM events WHERE source=? AND id LIKE ?',
                                     (source, f'{session}:legacy:%'))
                    store.put(source, p.get('response_id') or f'{session}:{index}', ts,
                              p.get('model') or model, p['usage'])
                    added += 1
            except (ValueError, TypeError, AttributeError):
                warnings.append(f'{path.name}:{index+1}: 无法解析用量记录')
        store.db.execute('INSERT OR REPLACE INTO files VALUES (?,?)', (str(path), signature))
    store.db.commit()
    return added, warnings


def sync_pi_family(store, home, agent):
    """Pi/OMP session token input excludes cacheRead/cacheWrite; costs are estimates."""
    count, warnings = 0, []
    home = Path(home).expanduser()
    for path in sorted(home.glob('sessions/**/*.jsonl')):
        try:
            stat = path.stat()
            signature = f'pi-v1:{stat.st_mtime_ns}:{stat.st_size}'
            marker = agent + ':' + str(path.resolve())
            old = store.db.execute('SELECT signature FROM files WHERE path=?', (marker,)).fetchone()
            if old and old[0] == signature:
                continue
            with path.open(encoding='utf-8') as f:
                for index, line in enumerate(f):
                    try:
                        r = json.loads(line)
                        msg = r.get('message') or {}
                        if r.get('type') != 'message' or msg.get('role') != 'assistant':
                            continue
                        raw = msg.get('usage')
                        if not isinstance(raw, dict):
                            continue
                        cache_read = int(number(raw.get('cacheRead')))
                        cache_write = int(number(raw.get('cacheWrite')))
                        usage = {'input_tokens': int(number(raw.get('input'))) + cache_read + cache_write,
                                 'output_tokens': raw.get('output'), 'cached_tokens': cache_read,
                                 'reasoning_tokens': raw.get('reasoning', raw.get('reasoningTokens', 0))}
                        ts = r.get('timestamp') or msg.get('timestamp')
                        if isinstance(ts, (int, float)) and ts > 100_000_000_000:
                            ts /= 1000
                        provider = msg.get('provider') or 'unknown'
                        model = msg.get('model') or 'unknown'
                        ident = hashlib.sha256(json.dumps([r.get('id') or f'{path}:{index}',ts,provider,model]).encode()).hexdigest()
                        estimate = (raw.get('cost') or {}).get('total') if isinstance(raw.get('cost'),dict) else None
                        store.put(agent + ':' + provider, ident, ts, model, usage,
                                  status='error' if msg.get('stopReason') in ('error','aborted') else 'ok',
                                  agent=agent, provider=provider, dataset='local', estimated_cost=estimate,
                                  cache_write_tokens=cache_write)
                        count += 1
                    except (ValueError, TypeError, KeyError, AttributeError):
                        # A partial last line will be retried once the file changes.
                        if line.strip(): warnings.append(f'{agent}/{path.name}:{index+1}: 无法解析会话条目')
            store.db.execute('INSERT OR REPLACE INTO files VALUES (?,?)', (marker,signature))
        except OSError as exc:
            warnings.append(f'{agent}/{path.name}: {exc.strerror}')
    store.db.commit()
    return count, warnings


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward authorization across redirects.


OPENER = urllib.request.build_opener(NoRedirect)


def get_json(url, key):
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'})
    with OPENER.open(request, timeout=30) as response:
        return json.load(response)


def sync_openrouter(store, key, management_key=None, key_hash=None, agent=None, namespace=None):
    if agent and not key_hash:
        raise ValueError("OpenRouter 历史指定 agent 时必须同时提供 --key-hash，且该 key 应专供此 agent 使用")
    warnings = []
    if key:
        raw = get_json('https://openrouter.ai/api/v1/key', key)['data']
        allowed = ('usage', 'usage_daily', 'usage_weekly', 'usage_monthly', 'limit', 'limit_remaining',
                   'byok_usage', 'byok_usage_daily', 'byok_usage_weekly', 'byok_usage_monthly')
        store.snapshot('openrouter:key'+(':'+namespace if namespace else ''), {k: raw.get(k) for k in allowed})
        store.db.commit()
    if not management_key:
        return ['未配置 OPENROUTER_MANAGEMENT_KEY：仅费用汇总，无远端模型/token 历史']
    query = '?' + urllib.parse.urlencode({'api_key_hash': key_hash}) if key_hash else ''
    rows = get_json('https://openrouter.ai/api/v1/activity' + query, management_key)['data']
    # Replace the completed-day snapshot atomically, do not add it to proxy records.
    source = 'openrouter:activity' + (':'+namespace if namespace else '') + (':' + key_hash if key_hash else ':account')
    with store.db:
        store.db.execute('DELETE FROM events WHERE source=?', (source,))
        for row in rows:
            ident = hashlib.sha256(json.dumps([row.get(k) for k in ('date', 'model', 'endpoint_id', 'provider_name')]).encode()).hexdigest()
            store.put(source, ident, row['date'] + 'T00:00:00+00:00', row.get('model'), row,
                      row.get('requests', 0), row.get('usage'), agent=agent)
        store.snapshot(source, {'note': '最近 30 个已结束的 UTC 日；管理 key 数据；账户范围' if not key_hash
                               else '最近 30 个已结束的 UTC 日；指定 key 范围', 'rows': len(rows)})
    return warnings


def import_jsonl(store, path, source=None, source_prefix=None):
    count = 0
    with store.db, open(path, encoding='utf-8') as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                ident = row.get('id') or hashlib.sha256(line.strip().encode()).hexdigest()
                target_source = source if source is not None else f'import:{source_prefix}:{row.get("source") or "unknown"}'
                store.put(target_source, ident, row['timestamp'], row.get('model'), row.get('usage', row),
                          row.get('requests', 1), row.get('cost'), row.get('status', 'ok'),
                          agent=row.get('agent'), provider=row.get('provider'),
                          estimated_cost=row.get('estimated_cost'), cache_write_tokens=row.get('cache_write_tokens',0))
                count += 1
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise ValueError(f'第 {n} 行无效：{exc}') from exc
    return count


def short(n):
    if n >= 1_000_000: return f'{n / 1_000_000:.2f}M'
    if n >= 1000: return f'{n / 1000:.1f}k'
    return f'{n:g}'


def clean(value):
    return ''.join(c for c in str(value) if c.isprintable())


def chart(values, labels, width=72, height=10):
    width = max(12, width)
    max_value = max(values or [0]) or 1
    canvas = [[' ']*width for _ in range(height)]
    points = [(round(i*(width-1)/max(1,len(values)-1)),
               height-1-round(v/max_value*(height-1))) for i,v in enumerate(values)]
    for i, (x,y) in enumerate(points):
        if i:
            px,py = points[i-1]
            steps = max(abs(x-px),abs(y-py),1)
            for s in range(steps+1):
                xx, yy = round(px+(x-px)*s/steps), round(py+(y-py)*s/steps)
                canvas[yy][xx] = '·'
    for x, y in points:
        canvas[y][x] = '●'
    lines = [f'{short(max_value*(height-1-y)/(height-1)):>8} │' + ''.join(row) for y,row in enumerate(canvas)]
    lines.append('         └' + '─'*width)
    if labels:
        lines.append('          ' + labels[0] + ' ' * max(1,width-len(labels[0])-len(labels[-1])) + labels[-1])
    return lines


def render(store, args, width=100, warnings=()):
    rows = store.rows(args.days, args.source, args.model)
    lines = [f'LLM METER  |  {dt.datetime.now():%Y-%m-%d %H:%M:%S}  |  最近 {args.days} 天',
             f'来源: {args.source or "全部（不同来源可能覆盖同一调用）"}  模型: {args.model or "全部"}', '']
    sums = {k:sum(r[k] for r in rows) for k in FIELDS}
    calls = sum(r['requests'] for r in rows)
    missing = sum(r['requests'] for r in rows if r['status'] == 'missing_usage')
    errors = sum(r['requests'] for r in rows if r['status'] == 'error')
    known_cost = [r['cost'] for r in rows if r['cost'] is not None]
    lines += [f'调用 {calls:,}  |  Token {sums["input_tokens"]+sums["output_tokens"]:,}  |  输入 {short(sums["input_tokens"])}  输出 {short(sums["output_tokens"])}',
              f'缓存 {short(sums["cached_tokens"])}（输入子集）  推理 {short(sums["reasoning_tokens"])}（输出子集）  |  已知费用 ' +
              (f'${sum(known_cost):.4f} ({len(known_cost)}/{len(rows)} 条)' if known_cost else '未知'),
              f'错误 {errors}  缺失 usage {missing}  |  Codex 调用数来自本地用量事件；不代表计费次数', '']
    today = dt.datetime.now().date()
    dates = [today-dt.timedelta(days=i) for i in reversed(range(args.days))]
    buckets = collections.defaultdict(float)
    for r in rows:
        day = dt.datetime.fromtimestamp(r['ts']).date()
        value = (r['input_tokens']+r['output_tokens']) if args.metric == 'tokens' else (
            r['requests'] if args.metric == 'requests' else r['cost'] or 0)
        buckets[day] += value
    lines.append(f'每日 {args.metric}（本地时区；cost 仅含已知费用）')
    lines.extend(chart([buckets[d] for d in dates], [d.strftime('%m-%d') for d in dates],
                       min(100,max(12,width-12))))
    lines += ['', f'{"MODEL / SOURCE":<46} {"CALLS":>7} {"INPUT":>9} {"OUTPUT":>9} {"CACHE":>9} {"USD*":>9}']
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r['model'],r['source'])].append(r)
    for (model,source), items in sorted(groups.items(), key=lambda g:-sum(r['input_tokens']+r['output_tokens'] for r in g[1]))[:args.top]:
        costs = [r['cost'] for r in items if r['cost'] is not None]
        lines.append(f'{clean(model+" / "+source)[:46]:<46} {sum(r["requests"] for r in items):>7} ' +
                     ' '.join(f'{short(sum(r[k] for r in items)):>9}' for k in FIELDS[:3]) +
                     f' {"$"+format(sum(costs),".4f") if costs else "?":>9}')
    if not rows:
        lines.append('暂无记录：运行 sync、import 或 proxy；可用 --demo 预览。')
    lines += ['', '额度 / 远端摘要（独立于上面的时间和模型筛选；显示采集时间）']
    for snap in store.db.execute('SELECT * FROM snapshots ORDER BY source'):
        data = json.loads(snap['data'])
        stamp = dt.datetime.fromtimestamp(snap['ts']).strftime('%m-%d %H:%M')
        if 'rate_limits' in data:
            limits = data['rate_limits']
            if isinstance(limits, dict): limits = [limits]
            for limit in limits:
                if not isinstance(limit, dict): continue
                for key in ('primary','secondary'):
                    window = limit.get(key)
                    if not window: continue
                    used = window.get('used_percent',0)
                    reset = dt.datetime.fromtimestamp(window['resets_at']).strftime('%m-%d %H:%M') if window.get('resets_at') else '?'
                    lines.append(f'{snap["source"]} [{stamp}] {key}: 已用 {used}% / {window.get("window_minutes","?")} 分钟窗口；重置 {reset}')
        elif snap['source'] == 'openrouter:key':
            lines.append(f'OpenRouter key [{stamp}] USD 今日 {data.get("usage_daily")} / 本周 {data.get("usage_weekly")} / 本月 {data.get("usage_monthly")} / 累计 {data.get("usage")} / key 限额剩余 {data.get("limit_remaining")}')
        else:
            lines.append(f'{snap["source"]} [{stamp}] {data.get("note", "")}')
    lines += ['', '注意：Codex / 代理 / 远端历史可能重叠，请用 --source 分别查看；未知 token 不估算。']
    lines += ['! '+clean(w) for w in warnings[:5]]
    return '\n'.join(lines)


def extract_usage(obj):
    if not isinstance(obj, dict): return None, None, None
    response = obj.get('response') if isinstance(obj.get('response'),dict) else obj
    usage = response.get('usage')
    return usage, response.get('model'), usage.get('cost') if isinstance(usage,dict) else None


class UsageObserver:
    """Consume SSE incrementally; only retain usage metadata, not response text."""
    def __init__(self):
        self.pending = bytearray()
        self.data = []
        self.usage = self.model = self.cost = None

    def observe(self, obj):
        usage,model,cost = extract_usage(obj)
        if usage is not None:
            self.usage, self.cost = usage,cost
        if model: self.model = model

    def feed(self, chunk):
        self.pending.extend(chunk)
        while b'\n' in self.pending:
            line,_,rest = self.pending.partition(b'\n')
            self.pending = bytearray(rest)
            line = line.rstrip(b'\r')
            if not line:
                if self.data:
                    try: self.observe(json.loads(b'\n'.join(self.data)))
                    except (ValueError, UnicodeError): pass
                self.data.clear()
            elif line.startswith(b'data:'):
                self.data.append(line[5:].lstrip())
            if sum(map(len,self.data)) > 2_000_000:
                self.data.clear()
        if len(self.pending)>2_000_000: self.pending.clear()

    def finish(self):
        self.feed(b'\n\n')


def run_proxy(args):
    base = args.upstream.rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ('https','http') or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ValueError('--upstream 必须是无凭证和查询参数的 http(s) base URL，例如 https://example.com/v1')
    key = os.environ.get(args.key_env)
    if not key: raise ValueError(f'请设置环境变量 {args.key_env}')
    local_token = os.environ.get(args.local_key_env) if args.local_key_env else None
    if args.local_key_env and not local_token: raise ValueError(f'请设置环境变量 {args.local_key_env}')
    if args.host not in ('127.0.0.1','localhost','::1') and not local_token:
        raise ValueError('监听非本机地址需要 --local-key-env')
    hop = {'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer',
           'transfer-encoding','upgrade','host','content-length','authorization','accept-encoding'}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *_): pass
        def do_GET(self): self.forward()
        def do_POST(self): self.forward()
        def fail(self, code, message):
            body = json.dumps({'error': {'message':message}}).encode()
            self.send_response(code); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def forward(self):
            self.close_connection = True
            if local_token and not hmac.compare_digest(self.headers.get('Authorization',''), 'Bearer '+local_token):
                return self.fail(401,'Invalid local API key')
            path = urllib.parse.urlsplit(self.path)
            if path.scheme or path.netloc or not path.path.startswith('/v1/') or '..' in urllib.parse.unquote(path.path).split('/'):
                return self.fail(400,'Use /v1/... routes')
            if self.headers.get('Transfer-Encoding'):
                return self.fail(400,'Chunked request bodies are not supported')
            try: length = int(self.headers.get('Content-Length','0'))
            except ValueError: return self.fail(400,'Invalid Content-Length')
            if length < 0 or length > 16_000_000: return self.fail(413,'Request too large')
            self.connection.settimeout(120)
            payload = self.rfile.read(length) if length else None
            model,body = None,None
            if payload:
                try:
                    body = json.loads(payload)
                    model = body.get('model')
                    if body.get('stream') and path.path.endswith('/chat/completions') and not args.no_include_usage:
                        body['stream_options'] = {**(body.get('stream_options') or {}), 'include_usage':True}
                        payload = json.dumps(body).encode()
                except (ValueError, AttributeError, TypeError): return self.fail(400,'Invalid JSON request')
            route = path.path[len('/v1'):]
            url = base + route + ('?'+path.query if path.query else '')
            headers = {k:v for k,v in self.headers.items() if k.lower() not in hop}
            headers.update(Authorization='Bearer '+key, **{'Accept-Encoding':'identity'})
            request = urllib.request.Request(url, data=payload, headers=headers, method=self.command)
            tracked = self.command == 'POST' and path.path in ('/v1/chat/completions','/v1/completions','/v1/responses','/v1/embeddings')
            observer, status, response_started = UsageObserver(), 'error', False
            try:
                try: upstream = OPENER.open(request, timeout=120)
                except urllib.error.HTTPError as exc: upstream = exc
                with upstream:
                    self.send_response(upstream.code)
                    for k,v in upstream.headers.items():
                        if k.lower() not in hop: self.send_header(k,v)
                    self.send_header('Connection','close'); self.end_headers(); response_started = True
                    stream = 'text/event-stream' in upstream.headers.get('Content-Type','')
                    buffer = bytearray()
                    disconnected = False
                    while True:
                        chunk = upstream.read1(65536)
                        if not chunk: break
                        if stream: observer.feed(chunk)
                        elif len(buffer)+len(chunk) <= 16_000_000: buffer.extend(chunk)
                        if not disconnected:
                            try: self.wfile.write(chunk); self.wfile.flush()
                            except (BrokenPipeError,ConnectionResetError): disconnected = True
                    if stream: observer.finish()
                    else:
                        try: observer.observe(json.loads(buffer))
                        except (ValueError,UnicodeError): pass
                    status = 'ok' if 200 <= upstream.code < 300 else 'error'
            except (OSError,urllib.error.URLError):
                if not response_started:
                    try: self.fail(502,'Upstream connection failed')
                    except OSError: pass
            finally:
                if tracked:
                    store = Store(args.db)
                    try:
                        store.put('proxy:'+args.name, str(uuid.uuid4()), time.time(), observer.model or model,
                                  observer.usage, cost=observer.cost, agent=args.agent, provider=args.provider or args.name,
                                  status='missing_usage' if status=='ok' and observer.usage is None else status)
                        store.db.commit()
                    finally: store.close()
    server = ThreadingHTTPServer((args.host,args.port), Handler)
    print(f'代理已启动：http://{args.host}:{server.server_port}/v1 → {base}  来源 proxy:{args.name}',flush=True)
    print('只保存用量元数据；Ctrl-C 停止。',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


def demo(store):
    for i in range(14):
        day = dt.datetime.now().replace(hour=12,minute=0,second=0,microsecond=0)-dt.timedelta(days=i)
        if day.timestamp()>time.time(): day=dt.datetime.now()
        for j,model in enumerate(('demo/model-a','demo/model-b')):
            inp = (14-i)*3000 + int(8000*(1+math.sin(i+j)))
            store.put('demo',f'{i}:{j}',day.timestamp(),model,
                      {'input_tokens':inp,'output_tokens':inp//5,'cached_tokens':inp//2},
                      requests=5+i,cost=inp/1_000_000)
    store.db.commit()


def positive(value):
    n = int(value)
    if n < 1: raise argparse.ArgumentTypeError('必须大于 0')
    return n


def main(argv=None):
    default_dir = Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share')))/'llm-meter'
    parser = argparse.ArgumentParser(description='Codex / OpenRouter / OpenAI 兼容中转站的终端用量看板')
    parser.add_argument('--env-file',help='读取 KEY=value 配置文件；放在子命令前，覆盖同名进程环境变量')
    parser.add_argument('--db',default=str(default_dir/'usage.sqlite3'),help='SQLite 数据库路径（放在子命令前）')
    sub = parser.add_subparsers(dest='command')
    dash = sub.add_parser('dashboard',help='折线图与模型统计；默认命令')
    dash.add_argument('--days',type=positive,default=14)
    dash.add_argument('--source',help='精确来源名，例如 codex:openai 或 proxy:relay')
    dash.add_argument('--model',help='精确模型名')
    dash.add_argument('--metric',choices=('tokens','requests','cost'),default='tokens')
    dash.add_argument('--top',type=positive,default=10)
    dash.add_argument('--watch',action='store_true',help='实时刷新；Ctrl-C 退出')
    dash.add_argument('--interval',type=positive,default=5)
    dash.add_argument('--demo',action='store_true',help='内存演示，不写入真实数据库')
    dash.add_argument('--no-sync',action='store_true',help='不自动读取 Codex 日志')
    dash.add_argument('--codex-home',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    sync = sub.add_parser('sync',help='同步本地 Codex / 远端 OpenRouter')
    sync.add_argument('--codex-home',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    sync.add_argument('--openrouter',action='store_true')
    sync.add_argument('--key-hash',help='限制 OpenRouter 管理 key 查询范围')
    sync.add_argument('--agent',help='按 key-hash 查询时指定该专用 key 所属 agent')
    sync.add_argument('--pi-home',default=str(Path.home()/'.pi/agent'))
    sync.add_argument('--omp-home',default=str(Path.home()/'.omp/agent'))
    imp = sub.add_parser('import',help='导入标准 JSONL 用量数据')
    imp.add_argument('file')
    import_source = imp.add_mutually_exclusive_group(required=True)
    import_source.add_argument('--source',help='所有行导入固定来源')
    import_source.add_argument('--source-prefix',help='按机器前缀保留原来源，生成 import:机器:原来源')
    exp = sub.add_parser('export',help='导出 JSONL 到标准输出')
    exp.add_argument('--days',type=positive,default=30); exp.add_argument('--source'); exp.add_argument('--model')
    exp.add_argument('--dataset',choices=('local','proxy','remote','import','demo'),help='只导出指定数据集')
    sub.add_parser('sources',help='列出可用来源')
    proxy = sub.add_parser('proxy',help='采集通用中转站的调用用量')
    proxy.add_argument('--upstream',required=True,help='含 /v1 的服务商 base URL')
    proxy.add_argument('--name',default='relay')
    proxy.add_argument('--agent',default='unknown',help='此代理专属客户端，如 pi、omp、codex')
    proxy.add_argument('--provider',help='中转站名，例如 aihub、openrouter；默认同 name')
    proxy.add_argument('--key-env',default='OPENAI_API_KEY')
    proxy.add_argument('--local-key-env',help='客户端鉴权 token 所在环境变量')
    proxy.add_argument('--host',default='127.0.0.1'); proxy.add_argument('--port',type=int,default=8787)
    proxy.add_argument('--no-include-usage',action='store_true',help='兼容不支持 stream_options 的服务商')
    web = sub.add_parser('web',help='启动本地网页用量看板')
    web.add_argument('--port',type=int,default=8765)
    web.add_argument('--host',default='127.0.0.1')
    web.add_argument('--demo',action='store_true')
    web.add_argument('--no-sync',action='store_true')
    web.add_argument('--codex-home',default=os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    web.add_argument('--pi-home',default=str(Path.home()/'.pi/agent'))
    web.add_argument('--omp-home',default=str(Path.home()/'.omp/agent'))
    web.add_argument('--connections-file',help='界面配置的服务端凭证文件；默认数据库旁的 *.connections.json')
    web.add_argument('--provider-map',help='provider 名称映射 JSON 文件（不放 key）')
    args = parser.parse_args(argv)
    if args.env_file:
        load_env_file(args.env_file)
    if args.command is None:
        return main(['--db',args.db,'dashboard'])
    if args.command == 'web':
        from llm_meter_web import run_web
        run_web(args); return
    if args.command == 'proxy':
        run_proxy(args); return
    store = Store(':memory:' if getattr(args,'demo',False) else args.db)
    try:
        if args.command == 'sync':
            count,warnings = sync_codex(store,args.codex_home)
            print(f'Codex：处理 {count} 条用量记录（重复同步不会累加）')
            for name,home in (('pi',args.pi_home),('omp',args.omp_home)):
                n,w = sync_pi_family(store,home,name)
                warnings += w
                print(f'{name}：处理 {n} 条用量记录')
            if args.openrouter:
                key,management = os.environ.get('OPENROUTER_API_KEY'),os.environ.get('OPENROUTER_MANAGEMENT_KEY')
                if not key and not management: raise ValueError('请设置 OPENROUTER_API_KEY 或 OPENROUTER_MANAGEMENT_KEY')
                warnings += sync_openrouter(store,key,management,args.key_hash,args.agent)
            for w in warnings: print('! '+w)
        elif args.command == 'import': print(f'已导入 {import_jsonl(store,args.file,args.source,args.source_prefix)} 条记录')
        elif args.command == 'sources':
            for r in store.db.execute('SELECT source,COUNT(*) records,SUM(requests) requests FROM events GROUP BY source'):
                print(f'{r["source"]}\t记录 {r["records"]}\t调用 {r["requests"]}')
        elif args.command == 'export':
            for r in store.rows(args.days,args.source,args.model):
                if args.dataset and r['dataset'] != args.dataset:
                    continue
                print(json.dumps({'id':r['id'],'source':r['source'],'timestamp':dt.datetime.fromtimestamp(r['ts'],UTC).isoformat(),
                                  'model':r['model'],'requests':r['requests'],'usage':{k:r[k] for k in FIELDS},
                                  'cost':r['cost'],'estimated_cost':r['estimated_cost'],'status':r['status'],
                                  'agent':r['agent'],'provider':r['provider'],'cache_write_tokens':r['cache_write_tokens']},ensure_ascii=False))
        elif args.command == 'dashboard':
            if args.days>366: raise ValueError('--days 最大 366')
            if args.demo: demo(store); args.source='demo'
            if args.watch and not sys.stdout.isatty(): raise ValueError('--watch 需要交互式终端')
            while True:
                warnings=[]
                if not args.no_sync and not args.demo: _,warnings=sync_codex(store,args.codex_home)
                if args.watch: print('\033[2J\033[H',end='')
                print(render(store,args,shutil.get_terminal_size((100,40)).columns,warnings),flush=True)
                if not args.watch: break
                time.sleep(args.interval)
    finally: store.close()


def cli():
    try: main()
    except KeyboardInterrupt: pass
    except (ValueError,OSError,sqlite3.Error,urllib.error.URLError,KeyError) as exc:
        if isinstance(exc,urllib.error.HTTPError): message=f'远端接口 HTTP {exc.code}；请检查 key 类型、权限和服务可用性'
        elif isinstance(exc,urllib.error.URLError): message='远端连接失败；请检查网络'
        else: message=str(exc)
        print('错误：'+message,file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    cli()

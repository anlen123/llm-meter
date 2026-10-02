"""Server-only site credentials and balance adapters. No executable extractors."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import uuid

from llm_meter import get_json


def normalize_base(url):
    url=str(url or '').strip().rstrip('/')
    parts=urllib.parse.urlsplit(url)
    if parts.scheme not in ('https','http') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Base URL 必须是无凭证、查询参数的 http(s) 地址')
    try:parts.port
    except ValueError:raise ValueError('Base URL 端口无效') from None
    return url


def balance_url(base,path):
    base=normalize_base(base)
    if not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or '#' in path or '\\' in path:
        raise ValueError('余额路径必须是站点内的绝对路径，例如 /v1/usage')
    origin=urllib.parse.urlsplit(base)
    # /v1/usage always refers to the origin, even if the supplied API base ends in /v1.
    return urllib.parse.urlunsplit((origin.scheme,origin.netloc,path,'',''))


def first_defined(*values):
    return next((v for v in values if v is not None),None)


def extract_balance(response,currency="auto"):
    if currency not in ("auto","USD","CNY"):
        raise ValueError("币种必须是 auto、USD 或 CNY")
    if not isinstance(response,dict):raise ValueError('余额接口应返回 JSON 对象')
    quota=response.get('quota') if isinstance(response.get('quota'),dict) else {}
    remaining=first_defined(response.get('remaining'),quota.get('remaining'),response.get('balance'))
    unit=first_defined(response.get('unit'),quota.get('unit'),'USD')
    valid=first_defined(response.get('is_active'),response.get('isValid'),True)
    if remaining is None:raise ValueError('响应没有 remaining、quota.remaining 或 balance 字段')
    if isinstance(remaining,bool):raise ValueError('余额字段不是有效数字')
    try:remaining=float(remaining)
    except (ValueError,TypeError):raise ValueError('余额字段不是有效数字') from None
    if not math.isfinite(remaining):raise ValueError('余额字段不是有限数字')
    if isinstance(valid,str):valid=valid.lower() not in ('false','0','no','inactive')
    return {'remaining':remaining,'unit':str(unit) if currency=='auto' else currency,
            'reported_unit':str(unit),'currency_source':'interface' if currency=='auto' else 'configured',
            'isValid':bool(valid),'scope':'接口返回余额'}


def extract_deepseek_balance(response, currency="auto"):
    if not isinstance(response, dict):raise ValueError('DeepSeek 余额响应格式无效')
    rows=response.get('balance_infos')
    if not isinstance(rows,list) or not rows:raise ValueError('DeepSeek 未返回账户余额')
    if currency not in ('auto','USD','CNY'):raise ValueError('DeepSeek 币种无效')
    selected=next((r for r in rows if isinstance(r,dict) and (currency=='auto' or r.get('currency')==currency)),None)
    if selected is None:raise ValueError('DeepSeek 未返回所选币种余额；请选择跟随接口')
    result=extract_balance({'remaining':selected.get('total_balance'),'unit':selected.get('currency'),
                            'is_active':response.get('is_available',True)})
    result.update(scope='DeepSeek 账户余额（含赠金和充值）',balances=rows,
                  granted_balance=selected.get('granted_balance'),topped_up_balance=selected.get('topped_up_balance'))
    return result


class Connections:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.RLock()
        if self.path.exists():
            self.items=json.loads(self.path.read_text(encoding='utf-8')).get('connections',[])
            if not isinstance(self.items,list):raise ValueError('站点配置文件格式无效')
        else:self.items=[]

    def persist(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        fd,name=tempfile.mkstemp(prefix='.meter-connections-',dir=self.path.parent)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                json.dump({'version':1,'connections':self.items},f,ensure_ascii=False,allow_nan=False,indent=2)
                f.flush();os.fsync(f.fileno())
            os.replace(name,self.path)
        finally:
            if os.path.exists(name):os.unlink(name)

    def public(self):
        with self.lock:
            return [{k:v for k,v in item.items() if k not in ('api_key','management_key')} |
                    {'has_api_key':bool(item.get('api_key')),'has_management_key':bool(item.get('management_key'))}
                    for item in self.items]

    def get(self,ident):
        with self.lock:
            item=next((x for x in self.items if x['id']==ident),None)
            if item is None:raise ValueError('站点不存在')
            return dict(item)

    def first_openrouter(self):
        with self.lock:
            return next((dict(x) for x in self.items if x['type']=='openrouter'),None)

    def aliases(self):
        with self.lock:
            return {alias:item['provider'] for item in self.items for alias in item.get('aliases',[])}

    def save(self,data):
        if not isinstance(data,dict):raise ValueError('配置必须是 JSON 对象')
        ident=data.get('id') or uuid.uuid4().hex
        if not isinstance(ident,str) or len(ident)>128:raise ValueError('站点 ID 无效')
        with self.lock:
            old=next((x for x in self.items if x['id']==ident),{})
            name=str(data.get('name') or '').strip()
            provider=str(data.get('provider') or '').strip()
            kind=data.get('type','generic')
            if not name or len(name)>80 or not provider or len(provider)>80:raise ValueError('请填写站点名称和 Provider ID（最多 80 字符）')
            if kind not in ('generic','openrouter','deepseek'):raise ValueError('无效的查询类型')
            base='https://openrouter.ai/api/v1' if kind=='openrouter' else normalize_base(data.get('base_url'))
            path=data.get('balance_path') or ('/user/balance' if kind=='deepseek' else '/v1/usage')
            if kind in ('generic','deepseek'):balance_url(base,path)
            currency=data.get('currency',old.get('currency','auto'))
            if currency not in ('auto','USD','CNY'):raise ValueError('币种必须是跟随接口、USD 或 CNY')
            if kind=='openrouter':
                if currency=='CNY':raise ValueError('OpenRouter 账户余额固定使用 USD')
                currency='USD'
            aliases=data.get('aliases',[])
            if not isinstance(aliases,list) or len(aliases)>50 or not all(isinstance(v,str) and 0<len(v)<=100 for v in aliases):
                raise ValueError('Provider 别名必须是字符串列表')
            for item in self.items:
                if item['id']!=ident and set(aliases)&set(item.get('aliases',[])):
                    raise ValueError('同一个 provider 别名不能映射到多个站点')
            row={'id':ident,'name':name,'provider':provider,'type':kind,'base_url':base,
                 'balance_path':path,'currency':currency,'aliases':aliases,'balance':None,'last_error':None,'updated_at':time.time()}
            for key in ('api_key','management_key'):
                value=data.get(key)
                if value is not None and (not isinstance(value,str) or '\n' in value or '\r' in value or len(value)>4096):
                    raise ValueError('凭证格式无效')
                # Blank edit means keep stored value; explicit checkbox clears it.
                row[key]='' if data.get('clear_'+key) else (value.strip() if value and value.strip() else old.get(key,''))
            if kind in ('generic','deepseek') and not row['api_key']:raise ValueError('请填写 API key')
            if kind=='openrouter' and not row['api_key'] and not row['management_key']:raise ValueError('请填写普通 key 或管理 key')
            before=self.items
            self.items=[x for x in self.items if x['id']!=ident]+[row]
            try:self.persist()
            except Exception:self.items=before;raise
            return ident

    def delete(self,ident):
        with self.lock:
            self.get(ident)
            before=self.items
            self.items=[x for x in self.items if x['id']!=ident]
            try:self.persist()
            except Exception:self.items=before;raise

    def query(self,ident):
        item=self.get(ident)
        try:
            if item['type']=='generic':
                balance=extract_balance(get_json(balance_url(item['base_url'],item['balance_path']),item['api_key']),item.get('currency','auto'))
            elif item['type']=='deepseek':
                balance=extract_deepseek_balance(get_json(balance_url(item['base_url'],item['balance_path']),item['api_key']),item.get('currency','auto'))
            else:
                balance={'remaining':None,'unit':'USD','isValid':True,'scope':'账户余额需管理 key'}
                if item.get('api_key'):
                    raw=get_json('https://openrouter.ai/api/v1/key',item['api_key'])['data']
                    balance['key_summary']={k:raw.get(k) for k in ('usage','usage_daily','usage_monthly','limit_remaining')}
                    balance['key_limit_remaining']=raw.get('limit_remaining')
                if item.get('management_key'):
                    raw=get_json('https://openrouter.ai/api/v1/credits',item['management_key'])['data']
                    purchased,used=float(raw['total_credits']),float(raw['total_usage'])
                    if not math.isfinite(purchased) or not math.isfinite(used):raise ValueError('余额字段不是有限数字')
                    balance.update(remaining=purchased-used,scope='OpenRouter 账户余额')
            balance['queried_at']=time.time()
        except Exception as exc:
            if isinstance(exc,urllib.error.HTTPError):message=f'余额接口 HTTP {exc.code}，请检查 key 与权限'
            elif isinstance(exc,ValueError):message=str(exc)
            else:message='余额查询失败，请检查服务商地址、网络和返回格式'
            with self.lock:
                current=next((x for x in self.items if x['id']==ident),None)
                if current and current['updated_at']==item['updated_at']:
                    current['last_error']=message;self.persist()
            raise ValueError(message) from None
        with self.lock:
            current=next((x for x in self.items if x['id']==ident),None)
            if current and current['updated_at']==item['updated_at']:
                current['balance']=balance;current['last_error']=None;self.persist()
        return balance

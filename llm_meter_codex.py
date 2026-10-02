"""Read-only Codex app-server quota query; never starts an inference turn."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


def read_codex_limits(home, timeout=25):
    env=dict(os.environ, CODEX_HOME=str(Path(home).expanduser().resolve()))
    try:
        proc=subprocess.Popen(['codex','app-server','--listen','stdio://'],stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env)
    except OSError:raise ValueError('未找到 Codex CLI；请安装 Codex 并在此机器执行 codex login') from None
    messages=queue.Queue()
    def reader():
        for line in proc.stdout:
            try:messages.put(json.loads(line))
            except ValueError:pass
        messages.put(None)
    thread=threading.Thread(target=reader,daemon=True);thread.start()
    deadline=time.monotonic()+timeout
    def send(message):
        proc.stdin.write(json.dumps(message)+'\n');proc.stdin.flush()
    def rpc(ident,method,params=None):
        send({'id':ident,'method':method,'params':params or {}})
        while True:
            try:message=messages.get(timeout=max(.001,deadline-time.monotonic()))
            except queue.Empty:raise ValueError('Codex 额度查询超时；请检查网络与登录状态') from None
            if message is None:raise ValueError('Codex app-server 已退出；请检查 CLI 版本与配置')
            if message.get('id')!=ident:continue
            if 'error' in message:raise ValueError('Codex 无法查询额度；请检查登录状态，API key 模式请查询对应服务商余额')
            return message.get('result',{})
    try:
        rpc(1,'initialize',{'clientInfo':{'name':'llm_meter','title':'LLM Meter','version':'0.4.0'}})
        send({'method':'initialized','params':{}})
        result=rpc(2,'account/rateLimits/read')
        buckets=result.get('rateLimitsByLimitId') or {'codex':result.get('rateLimits')}
        limits=[v for v in buckets.values() if isinstance(v,dict)]
        if not limits:raise ValueError('Codex 未返回额度；请确认此机器已使用 ChatGPT 账户登录')
        # Do not expose account identifiers, email, tokens or raw RPC responses.
        return {'rate_limits':[{k:v.get(k) for k in ('limitId','limitName','primary','secondary','credits','planType')} for v in limits],
                'live':True,'note':'Codex 实时额度；剩余比例不是货币余额'}
    except (OSError,BrokenPipeError):raise ValueError('Codex 额度查询失败；请检查 CLI 与登录状态') from None
    finally:
        if proc.poll() is None:proc.terminate()
        try:proc.wait(timeout=3)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        thread.join(timeout=1)
        proc.stdin.close();proc.stdout.close()

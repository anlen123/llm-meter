'use strict';
const $ = id => document.getElementById(id);
let colors = ['#368a71','#88ae98','#ccb77e','#819cbd','#b498bb','#7ebbc0','#d49d83','#a4b6a0'];
function applyTheme(theme){
 const summit=theme==='summit';document.documentElement.dataset.theme=summit?'summit':'valley';$('themeSelect').value=summit?'summit':'valley';
 colors=summit?['#6289c4','#ca6a87','#749aaa','#a58dc1','#c99b59','#7380ac','#69a895','#b67753']:['#50804b','#c08a35','#669da8','#b96747','#9b7faa','#9da64e','#c08685','#648374'];
 $('sceneCaption').textContent=summit?'晴空 · 雪山观测站':'晴天 · 用量小屋';
 try{localStorage.setItem('llm-meter-theme',summit?'summit':'valley');}catch{}
 if(current)render(current);
}
const names = {agent:'Agent',model:'模型',provider:'中转站',source:'来源'};
const metricNames = {tokens:'总 Token',input_tokens:'输入 Token',output_tokens:'输出 Token',cached_tokens:'缓存 Token',requests:'调用量',cost:'金额'};
let grouping='agent', current=null, serial=0, controller=null, initialized=false;
const escaped = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const label = v => v==='unknown'?'未知':v;
const full = n => new Intl.NumberFormat('zh-CN',{maximumFractionDigits:0}).format(n||0);
function compact(n){return new Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(n||0);}
function dollars(n){return '$'+Number(n||0).toLocaleString('en-US',{minimumFractionDigits:4,maximumFractionDigits:4});}
function metricFormat(n){return $('metric').value==='cost'?dollars(n):compact(n);}
function isoLocal(d){return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}
function setRange(days){let end=new Date(),start=new Date(end);start.setDate(end.getDate()-days+1);$('startDate').value=isoLocal(start);$('endDate').value=isoLocal(end);document.querySelectorAll('[data-days]').forEach(b=>b.classList.toggle('selected',Number(b.dataset.days)===days));}
function params(){const p=new URLSearchParams({start:$('startDate').value,end:$('endDate').value,dataset:$('dataset').value,group:grouping,metric:$('metric').value,money:$('money').value});for(const k of ['agent','provider','model','source'])if($(k).value)p.set(k,$(k).value);return p;}
function feedback(message,error=false){$('feedback').hidden=!message;$('feedback').textContent=message;$('feedback').classList.toggle('error',error);}
function fillOptions(id,values){const selected=$(id).value;const text={agent:'全部 agent',provider:'全部中转站',model:'全部模型',source:'全部记录来源'}[id];$(id).replaceChildren(new Option(text,''));for(const value of values)$(id).add(new Option(label(value),value));if(selected&&!values.includes(selected))$(id).add(new Option(label(selected)+'（当前数据集无记录）',selected));$(id).value=selected;}
async function load(){const ticket=++serial;if(controller)controller.abort();controller=new AbortController();try{const response=await fetch('/api/stats?'+params(),{signal:controller.signal});const data=await response.json();if(!response.ok)throw new Error(data.error||'数据读取失败');if(ticket!==serial)return;if(!initialized&&data.demo&&$('dataset').value==='local'){$('dataset').value='demo';initialized=true;return load();}initialized=true;current=data;for(const id of ['agent','provider','model','source'])fillOptions(id,data.options[id]);render(data);const url=new URL(location.href);url.search=params().toString();history.replaceState(null,'',url);}catch(error){if(error.name!=='AbortError')feedback(error.message,true);}}
function render(data){const s=data.summary;const billedKnown=s.cost_records>0, estimateKnown=s.estimated_cost_records>0;
$('modeBadge').textContent=data.demo?'演示数据':'本地看板';
$('totalTokens').textContent=compact(s.tokens);$('tokenDetail').textContent=`输入 ${compact(s.input_tokens)} · 输出 ${compact(s.output_tokens)}`;$('inputRatio').style.width=(s.tokens?s.input_tokens/s.tokens*100:0)+'%';
$('totalRequests').textContent=full(s.requests);$('requestDetail').textContent=`${full(s.records)} 条记录 · ${full(s.errors)} 次错误`;
$('billedCost').textContent=billedKnown?dollars(s.cost):'未知';$('billedDetail').textContent=billedKnown?`${s.cost_records} / ${s.records} 条记录包含费用`:'当前范围没有服务商费用记录';
$('estimatedCost').textContent=estimateKnown?dollars(s.estimated_cost):'未知';$('estimatedDetail').textContent=estimateKnown?`${s.estimated_cost_records} / ${s.records} 条记录包含估算`:'当前范围没有本地估算记录';
for(const [field,id] of [['cost','billed'],['estimated_cost','estimated']]){
$(id+'Unit').textContent=s[field+'_per_million']===null?'未知':dollars(s[field+'_per_million']);
$(id+'UnitNote').textContent=`覆盖 ${compact(s[field+'_covered_tokens'])} Token · ${s[field+'_priced_records']} 条记录`;
}
const moneyMetric=$('metric').value==='cost';$('money').hidden=!moneyMetric;
const metricText=moneyMetric?($('money').value==='estimated'?'本地估算费用 · USD':'服务商费用 · USD'):metricNames[$('metric').value];
$('metricLegend').textContent=metricText;$('chartTotal').textContent='期间合计 '+metricFormat(data.series.reduce((a,r)=>a+r.value,0));
$('trendSubtitle').textContent='每日汇总 · '+data.timezone;$('dateLabel').textContent=data.filters.start+' — '+data.filters.end;
$('chartNote').textContent=moneyMetric?'金额仅累计有值的记录，未知费用不计入':'缓存与推理是子集，不重复计入总 Token';
$('tableSubtitle').textContent='按 '+names[grouping]+' 汇总 · *单价口径：'+($('money').value==='estimated'?'本地估算':'服务商费用')+' · 点击名称筛选';$('dimensionLabel').textContent=grouping.toUpperCase();$('groupCount').textContent=data.groups.length+' 个分组';
$('donutTotal').textContent=metricFormat(data.groups.reduce((a,r)=>a+r.value,0));$('donutUnit').textContent=moneyMetric?($('money').value==='estimated'?'估算 USD':'已知 USD'):($('metric').value==='requests'?'次调用':'Token');
$('providerCount').textContent=data.provider_groups.length;$('shareNote').textContent='按 '+metricText+' 计算占比'+(moneyMetric?'；无金额的记录不进入占比分母':'');
$('scopeNote').textContent={local:'本机 pi、omp、Codex 日志；估算费用与服务商费用分别显示。',proxy:'只显示经过代理的调用；agent 由专用代理配置指定。',remote:'服务商历史通常无法识别 agent；默认显示未知。',import:'显示导入记录；agent 与费用以导入文件为准。',demo:'演示数据，仅用于预览交互，不代表真实调用或价格。',all:'包含多个采集来源，同一调用可能重复；请谨慎比较合计。'}[data.filters.dataset];
drawTrend(data.series);drawDonut('mainDonut','mainLegend',data.groups,grouping);drawDonut('providerDonut','providerLegend',data.provider_groups,'provider');
renderTable(data.groups);renderRecent(data.recent);renderConnections(data);loadSites();
const notes=[];if(data.filters.dataset==='all')notes.push('当前为全部数据集：本地日志、代理记录和远端历史可能覆盖同一调用，不自动跨来源去重。');
if(data.filters.dataset==='remote'&&!data.filters.source&&data.options.source.length>1)notes.push('当前有多个远端历史范围，账户与单 key 数据可能重叠；请筛选一个记录来源。');
if(s.missing_usage)notes.push(`${s.missing_usage} 次成功调用未返回 usage，Token 合计不代表完整消耗。`);
if(s.unknown_agent)notes.push(`${s.unknown_agent} 次调用的 agent 未知；远端历史不会推断客户端归属。`);
if(s.records&&!billedKnown)notes.push('当前没有服务商返回费用；本地估算仅供比较，不是实际账单。');
if(data.warnings.length)notes.push(...data.warnings);
if(!s.records)notes.push('当前筛选没有记录。尝试切换数据集、日期或重置筛选；代理和远端历史需要先采集或同步。');
$('qualityNotes').textContent=notes.join(' ');$('qualityNotes').classList.toggle('warning',data.filters.dataset==='all'||s.missing_usage>0||data.warnings.length>0);
$('updatedAt').textContent='更新于 '+new Date(data.updated_at*1000).toLocaleTimeString('zh-CN')+' · '+data.timezone;
}
function drawTrend(series){const svg=$('trendChart');const W=Math.max(320,svg.clientWidth||960),H=240;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);const L=66,R=20,T=18,B=37;const width=W-L-R,height=H-T-B;const max=Math.max(...series.map(r=>r.value),0);const top=max>0?max*1.15:1;
const x=i=>L+i*width/Math.max(1,series.length-1),y=v=>T+height*(1-v/top);
let markup='<defs><linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="var(--chart)" stop-opacity=".18"/><stop offset="100%" stop-color="var(--chart)" stop-opacity=".01"/></linearGradient></defs>';
for(let i=0;i<5;i++){const v=top*i/4,yy=y(v);markup+=`<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" class="svg-grid"/><text x="${L-12}" y="${yy+3}" text-anchor="end" class="svg-label">${escaped($('metric').value==='cost'?'$'+v.toFixed(v<1?3:2):compact(v))}</text>`;}
const path=series.map((r,i)=>(i?'L':'M')+x(i).toFixed(2)+','+y(r.value).toFixed(2)).join(' ');
if(max>0){markup+=`<path d="${path} L${x(series.length-1)},${T+height} L${L},${T+height} Z" fill="url(#areaFill)"/><path d="${path}" fill="none" stroke="var(--chart)" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>`;}
else markup+=`<text x="${W/2}" y="${H/2}" text-anchor="middle" class="svg-empty">${current.summary.records?'当前指标没有已知非零用量':'当前筛选暂无数据'}</text>`;
const ticks=Math.min(W<600?4:6,series.length);const indices=new Set();for(let j=0;j<ticks;j++)indices.add(Math.round(j*(series.length-1)/Math.max(1,ticks-1)));for(const i of indices)markup+=`<text x="${x(i)}" y="${H-12}" class="svg-label" text-anchor="middle">${escaped(series[i].date.slice(5).replace('-','/'))}</text>`;
markup+='<line id="hoverLine" x1="0" y1="18" x2="0" y2="203" stroke="var(--chart)" stroke-dasharray="4 4" visibility="hidden"/><circle id="hoverPoint" r="4" fill="#fff" stroke="var(--chart)" stroke-width="2" visibility="hidden"/>';
svg.innerHTML=markup;
svg.onpointermove=e=>{const rect=svg.getBoundingClientRect();const px=(e.clientX-rect.left)/rect.width*W;const i=Math.max(0,Math.min(series.length-1,Math.round((px-L)/width*(series.length-1))));const row=series[i];const line=$('hoverLine'),point=$('hoverPoint');line.setAttribute('x1',x(i));line.setAttribute('x2',x(i));line.setAttribute('visibility','visible');point.setAttribute('cx',x(i));point.setAttribute('cy',y(row.value));point.setAttribute('visibility','visible');const tip=$('chartTooltip');tip.textContent=row.date+' · '+metricFormat(row.value);tip.hidden=false;tip.style.left=Math.min(rect.width-150,Math.max(0,e.clientX-rect.left+10))+'px';tip.style.top='18px';};
svg.onpointerleave=()=>{$('chartTooltip').hidden=true;$('hoverLine').setAttribute('visibility','hidden');$('hoverPoint').setAttribute('visibility','hidden');};
}
function drill(dimension,value){if(!['agent','provider','model','source'].includes(dimension))return;$(dimension).value=value;load();}
function drawDonut(svgId,legendId,groups,dimension){const total=groups.reduce((a,r)=>a+r.value,0);const radius=79,circ=2*Math.PI*radius;let markup='<circle cx="110" cy="110" r="79" fill="none" stroke="var(--track)" stroke-width="24"/>';let offset=0;
const visible=groups.slice(0,7).map(r=>({...r}));if(groups.length>7)visible.push({name:'其他（'+(groups.length-7)+' 个）',value:groups.slice(7).reduce((a,r)=>a+r.value,0),other:true});
for(let i=0;i<visible.length;i++){const g=visible[i];const fraction=total?g.value/total:0;const length=Math.max(0,circ*fraction-3);if(length>0){markup+=`<circle class="donut-segment" data-index="${i}" cx="110" cy="110" r="79" fill="none" stroke="${colors[i%colors.length]}" stroke-width="24" stroke-dasharray="${length} ${circ-length}" stroke-dashoffset="${-offset}" transform="rotate(-90 110 110)"><title>${escaped(label(g.name))} · ${(fraction*100).toFixed(1)}% · ${escaped(metricFormat(g.value))}</title></circle>`;}offset+=circ*fraction;}
$(svgId).innerHTML=markup;$(legendId).replaceChildren();
if(!visible.length){const empty=document.createElement('div');empty.className='legend-empty';empty.textContent='暂无记录，尝试调整筛选或同步数据。';$(legendId).append(empty);}
for(let i=0;i<visible.length;i++){const g=visible[i];const button=document.createElement('button');button.className='legend-row';button.title=label(g.name)+' · '+metricFormat(g.value);button.innerHTML=`<i class="legend-dot" style="background:${colors[i%colors.length]}"></i><span class="legend-name">${escaped(label(g.name))}</span><span class="legend-percentage">${total?(g.value/total*100).toFixed(1):'0.0'}%</span>`;if(!g.other)button.onclick=()=>drill(dimension,g.name);else button.title+='；请在下方明细查看各项';$(legendId).append(button);}
$(svgId).querySelectorAll('[data-index]').forEach(segment=>{const g=visible[Number(segment.dataset.index)];if(!g.other){segment.setAttribute('tabindex','0');segment.setAttribute('role','button');segment.setAttribute('aria-label',label(g.name)+'，点击筛选');segment.onclick=()=>drill(dimension,g.name);segment.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();drill(dimension,g.name);}};}});
}
function renderTable(groups){$('groupTable').innerHTML=groups.length?groups.map((r,i)=>`<tr><td><button class="group-button" data-index="${i}" title="${escaped(label(r.name))}">${escaped(label(r.name))}</button></td><td class="num">${full(r.requests)}</td><td class="num">${compact(r.input_tokens)}</td><td class="num">${compact(r.output_tokens)}</td><td class="num">${compact(r.cached_tokens)}</td><td class="num">${r.cost_records?dollars(r.cost):'—'}</td><td class="num">${r.estimated_cost_records?dollars(r.estimated_cost):'—'}</td><td class="num">${r[($('money').value==='estimated'?'estimated_cost':'cost')+'_per_million']===null?'—':dollars(r[($('money').value==='estimated'?'estimated_cost':'cost')+'_per_million'])}</td><td class="num"><div class="share-cell"><span class="share-bar"><i style="width:${r.share*100}%"></i></span>${(r.share*100).toFixed(1)}%</div></td></tr>`).join(''):'<tr><td colspan="9" class="empty-cell">当前筛选没有记录 · 可以切换数据集或同步本地会话</td></tr>';
$('groupTable').querySelectorAll('[data-index]').forEach(b=>b.onclick=()=>drill(grouping,groups[Number(b.dataset.index)].name));}
function renderRecent(rows){$('recentTable').innerHTML=rows.length?rows.map(r=>`<tr><td>${escaped(new Date(r.ts*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}))}</td><td>${escaped(label(r.agent))}</td><td>${escaped(label(r.provider))}</td><td title="${escaped(r.model)}">${escaped(r.model)}</td><td class="num">${compact(r.input_tokens+r.output_tokens)}</td><td class="num">${r.cost===null?'—':dollars(r.cost)}</td><td class="num">${r.estimated_cost===null?'—':dollars(r.estimated_cost)}</td><td><span class="status-chip ${['error','missing_usage'].includes(r.status)?r.status:''}">${escaped({ok:'完成',error:'错误',missing_usage:'缺少 usage'}[r.status]||r.status)}</span></td></tr>`).join(''):'<tr><td colspan="8" class="empty-cell">没有匹配的调用记录</td></tr>';}
function renderCodexQuota(data){
 const snapshots=data.snapshots.filter(s=>s.source.startsWith('codex:')&&s.data.rate_limits).sort((a,b)=>b.timestamp-a.timestamp);
 const snap=snapshots[0];$('codexQuotaCards').replaceChildren();
 if(!snap){$('codexQuotaStatus').textContent=data.demo?'演示模式 · 不查询真实账户':'尚未查询 · 点击按钮获取当前额度';$('codexQuotaCards').innerHTML='<div class="quota-empty"><strong>—</strong><div>暂无额度数据<p>在此机器执行 codex login 后，点击「查询实时额度」。</p></div></div>';return;}
 const limits=Array.isArray(snap.data.rate_limits)?snap.data.rate_limits:[snap.data.rate_limits];
 $('codexQuotaStatus').textContent=(snap.data.live?'实时查询结果':'日志快照')+' · '+new Date(snap.timestamp*1000).toLocaleString('zh-CN')+' · '+snap.source+(Date.now()/1000-snap.timestamp>300?' · 数据较旧，建议重新查询':'');
 for(const limit of limits){if(!limit)continue;for(const key of ['primary','secondary']){const w=limit[key];if(!w)continue;
 const raw=w.usedPercent??w.used_percent;const used=raw==null?NaN:Number(raw);const remaining=Number.isFinite(used)?Math.max(0,Math.min(100,100-used)):null;
 const minutes=w.windowDurationMins??w.window_minutes;const reset=w.resetsAt??w.resets_at;
 const title=(limit.limitName||limit.limitId||snap.source.replace('codex:',''))+' · '+(key==='primary'?'主额度':'次额度');
 const value=remaining===null?'未知':new Intl.NumberFormat('zh-CN',{maximumFractionDigits:1}).format(remaining)+'%';
 const card=document.createElement('article');card.className='quota-card'+(remaining!==null&&remaining<=10?' depleted':remaining!==null&&remaining<=25?' low':'');
 card.innerHTML=`<div class="quota-card-label">${escaped(title)}<span>${escaped(minutes==null?'未知窗口':minutes>=1440?minutes/1440+' 天窗口':minutes>=60?minutes/60+' 小时窗口':minutes+' 分钟窗口')}</span></div><div class="quota-value">${value}<small>剩余</small></div><div class="quota-meter" role="meter" aria-label="${escaped(title)}剩余额度" ${remaining===null?'':'aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+remaining+'"'}><i style="width:${remaining??0}%"></i></div><div class="quota-reset">${remaining!==null&&remaining<=10?'额度偏低 · ':''}${reset?'重置于 '+escaped(new Date(reset*1000).toLocaleString('zh-CN')):'重置时间未提供'}</div>`;
 $('codexQuotaCards').append(card);
 }
 if(limit.credits){const card=document.createElement('article');card.className='quota-card credits-card';card.innerHTML=`<div class="quota-card-label">可用 credits</div><div class="quota-value">${escaped(limit.credits.unlimited?'不限额':limit.credits.balance??'未知')}</div><div class="quota-reset">接口 credits 原值 · 非美元余额</div>`;$('codexQuotaCards').append(card);}}
 if(!$('codexQuotaCards').childElementCount)$('codexQuotaCards').innerHTML='<div class="quota-empty">接口尚未提供窗口额度或 credits</div>';
}
function renderConnections(data){renderCodexQuota(data);const c=data.connections;const available=c.openrouter_key||c.openrouter_management;
$('localState').textContent=data.demo?'演示':c.local_sync?'已接入':'已暂停';$('localConnection').textContent=data.demo?'虚拟 agent 数据':c.local_sync?'自动扫描 pi · omp · codex':'--no-sync 模式';
$('routerState').textContent=data.demo?'演示':available?'已配置':'待配置';$('routerConnection').textContent=c.openrouter_management?'管理 key 已配置 · 可同步模型历史':c.openrouter_key?'普通 key 已配置 · 可同步费用摘要':'点击配置站点，或设置环境变量';
$('routerSyncBtn').disabled=data.demo||!available;$('routerSyncBtn').title=available?'查询远端用量，不发起模型推理':'请先点击配置站点填写 key';$('codexLimitsBtn').disabled=data.demo;$('syncBtn').disabled=data.demo||!c.local_sync;
const entries=[];for(const snap of data.snapshots){const stamp=new Date(snap.timestamp*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});const d=snap.data;if(snap.source==='openrouter:key'){entries.push(`OpenRouter key · ${stamp} · 今日 ${d.usage_daily===null?'未知':dollars(d.usage_daily)} / 本月 ${d.usage_monthly===null?'未知':dollars(d.usage_monthly)} / key 限额剩余 ${d.limit_remaining===null?'未设置':dollars(d.limit_remaining)}（独立摘要，不受筛选影响）`);}else if(d.rate_limits){for(const limit of (Array.isArray(d.rate_limits)?d.rate_limits:[d.rate_limits]))if(limit?.credits)entries.push(`${snap.source} · ${stamp} · credits ${limit.credits.unlimited?'不限额':limit.credits.balance??'未知'}（接口 credits，非美元）`);const limits=Array.isArray(d.rate_limits)?d.rate_limits:[d.rate_limits];for(const limit of limits)for(const key of ['primary','secondary'])if(limit&&limit[key]){const w=limit[key];const used=w.usedPercent??w.used_percent;const minutes=w.windowDurationMins??w.window_minutes;const reset=w.resetsAt??w.resets_at;entries.push(`${snap.source}${limit.limitName?' / '+limit.limitName:''} · ${stamp}${d.live?' 实时查询':' 日志快照'} · ${key} 剩余 ${used==null?'未知':Math.max(0,100-used)+'%'} / ${minutes??'未知'} 分钟窗口${reset?' · 重置 '+new Date(reset*1000).toLocaleString('zh-CN'):''}`);}}else if(d.note)entries.push(`${snap.source} · ${d.note}`);}
$('snapshots').replaceChildren();for(const text of entries){const div=document.createElement('div');div.className='snapshot-item';div.textContent=text;$('snapshots').append(div);}}
async function sync(endpoint,button){button.disabled=true;feedback('正在同步，请稍候…');try{const response=await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json','X-Meter-Request':'1'},body:'{}'});const data=await response.json();if(!response.ok)throw new Error(data.error||'同步失败');feedback(data.message+(data.warnings?.length?' · '+data.warnings.join('；'):''));await load();}catch(error){feedback(error.message,true);}finally{button.disabled=false;if(current)renderConnections(current);}}
setRange(14);
const saved=new URLSearchParams(location.search);for(const [param,id] of [['start','startDate'],['end','endDate'],['dataset','dataset'],['metric','metric'],['money','money']])if(saved.has(param))$(id).value=saved.get(param);for(const key of ['agent','provider','model','source'])if(saved.has(key)){$(key).add(new Option(label(saved.get(key)),saved.get(key)));$(key).value=saved.get(key);}if(['agent','model','provider'].includes(saved.get('group')))grouping=saved.get('group');
document.querySelectorAll('[data-group]').forEach(b=>{b.classList.toggle('selected',b.dataset.group===grouping);b.onclick=()=>{grouping=b.dataset.group;document.querySelectorAll('[data-group]').forEach(x=>x.classList.toggle('selected',x===b));load();};});
for(const id of ['dataset','agent','provider','model','source','metric','money','startDate','endDate'])$(id).onchange=()=>{feedback('');if(id==='dataset')for(const k of ['agent','provider','model','source'])$(k).value='';if(id==='startDate'||id==='endDate')document.querySelectorAll('[data-days]').forEach(b=>b.classList.remove('selected'));load();};
document.querySelectorAll('[data-days]').forEach(b=>b.onclick=()=>{setRange(Number(b.dataset.days));load();});
$('resetBtn').onclick=()=>{for(const k of ['agent','provider','model','source'])$(k).value='';setRange(14);feedback('');load();};
$('codexLimitsBtn').onclick=()=>sync('/api/codex/limits',$('codexLimitsBtn'));
$('syncBtn').onclick=()=>sync('/api/sync',$('syncBtn'));$('routerSyncBtn').onclick=()=>sync('/api/sync/openrouter',$('routerSyncBtn'));
$('exportBtn').onclick=()=>{const link=document.createElement('a');link.href='/api/export?'+params();link.download='llm-meter-'+$('startDate').value+'-'+$('endDate').value+'.csv';link.click();};
setInterval(()=>{if($('autoRefresh').checked&&!document.hidden)load();},15000);
load();

let resizeTimer;window.addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{if(current)drawTrend(current.series);},100);});

let siteConfigs=[];
function settingsMessage(message,error=false){$('settingsFeedback').hidden=!message;$('settingsFeedback').textContent=message;$('settingsFeedback').classList.toggle('error',error);}
async function siteRequest(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Meter-Request':'1'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw new Error(data.error||'站点操作失败');return data;}
async function loadSites(){try{const response=await fetch('/api/connections');const data=await response.json();if(!response.ok)throw new Error(data.error||'配置读取失败');siteConfigs=data.connections;renderSites();}catch(error){if($('settingsDialog').open)settingsMessage(error.message,true);}}
function renderSites(){
$('savedSites').replaceChildren();$('balanceCards').replaceChildren();
if(!siteConfigs.length){const empty=document.createElement('p');empty.className='settings-note';empty.textContent='还没有站点。右侧填写名称、地址和 key 后保存。';$('savedSites').append(empty);}
for(const item of siteConfigs){
 const div=document.createElement('div');div.className='saved-site'+($('siteId').value===item.id?' selected':'');div.dataset.siteId=item.id;div.innerHTML=`<strong>${escaped(item.name)}</strong><small>${escaped(item.provider)} · ${item.has_api_key?'普通 key 已保存':'无普通 key'}${item.has_management_key?' · 管理 key 已保存':''}</small><div class="site-list-actions"></div>`;
 const buttons=div.querySelector('.site-list-actions');
 for(const [text,action,danger] of [['编辑',()=>editSite(item),false],['查余额',()=>querySite(item.id),false],...(item.type==='openrouter'?[['同步历史',()=>historySite(item.id),false]]:[]),['删除',()=>deleteSite(item),true]]){const b=document.createElement('button');b.type='button';b.textContent=text;b.className='text-button'+(danger?' danger-button':'');b.onclick=action;buttons.append(b);}
 $('savedSites').append(div);
 const balance=item.balance,card=document.createElement('div');card.className='balance-card';
 const number=balance?.remaining===null||balance?.remaining===undefined?(balance?'未知':'待查询'):new Intl.NumberFormat('zh-CN',{maximumFractionDigits:6}).format(balance.remaining);
 const state=balance?.isValid===false?'已停用':balance?'已查询':'未查询';
 card.innerHTML=`<div class="balance-card-header"><strong>${escaped(item.name)}</strong><span class="status-chip ${balance?.isValid===false?'error':''}">${state}</span></div><div class="balance-number">${number}<span class="balance-unit">${escaped(balance?.unit||(item.currency==='auto'?'':item.currency)||'')}</span></div><div class="balance-meta"></div><div class="balance-actions"><button class="button secondary edit-site" type="button">编辑配置</button><button class="text-button query-site" type="button">↻ 查询余额</button></div>`;
 const meta=card.querySelector('.balance-meta');
 const lines=[balance?.scope||('GET '+(item.type!=='openrouter'?balanceEndpoint(item):'OpenRouter 专用接口'))];
 if(balance?.key_limit_remaining!==null&&balance?.key_limit_remaining!==undefined)lines.push('普通 key 限额剩余：'+dollars(balance.key_limit_remaining)+'（不等于账户余额）');
 if(balance?.currency_source==='configured')lines.push('按配置币种显示，未换汇'+(balance.reported_unit!==balance.unit?'；接口单位：'+balance.reported_unit:''));
 if(balance?.balances)for(const row of balance.balances)lines.push(`${row.currency} 可用 ${row.total_balance} · 赠金 ${row.granted_balance} · 充值 ${row.topped_up_balance}`);
 if(balance?.key_summary)lines.push('普通 key 今日费用：'+(balance.key_summary.usage_daily==null?'未知':dollars(balance.key_summary.usage_daily)));
 if(balance?.queried_at)lines.push('采集于 '+new Date(balance.queried_at*1000).toLocaleString('zh-CN'));
 if(item.last_error)lines.push('本次失败：'+item.last_error+'；已显示的余额为上次成功快照');
 meta.textContent=lines.join(' · ');if(item.last_error)meta.classList.add('balance-error');card.querySelector('.query-site').onclick=()=>querySite(item.id);card.querySelector('.edit-site').onclick=()=>{editSite(item);if(!$('settingsDialog').open)$('settingsDialog').showModal();};$('balanceCards').append(card);
}}
function balanceEndpoint(item){try{return new URL(item.base_url).origin+item.balance_path;}catch{return '站点域名/v1/usage';}}
function updateSiteType(){const router=$('siteType').value==='openrouter';$('managementField').hidden=!router;$('siteBase').disabled=router;$('sitePath').disabled=router;$('siteCurrency').disabled=router;if(router)$('siteCurrency').value='USD';
 if(router){$('siteBase').value='https://openrouter.ai/api/v1';$('sitePath').value='/api/v1/credits';if(!$('siteProvider').value)$('siteProvider').value='openrouter';}
 $('queryRules').innerHTML=router?'普通 key：费用摘要与 key 限额剩余<br>管理 key：账户余额 = total_credits − total_usage；可同步模型历史':'余额：remaining → quota.remaining → balance<br>单位：'+($('siteCurrency').value==='auto'?'unit → quota.unit → USD':'使用配置 '+$('siteCurrency').value+'（不换汇）')+'<br>有效：is_active → isValid → true';
 if($('siteType').value==='deepseek')$('queryRules').textContent='DeepSeek：total_balance 为可用余额（含赠金与充值）；按接口实际币种选择，不换汇。多币种详情显示在余额卡片。';
 $('endpointPreview').textContent=router?'管理 key 查询账户余额；只有普通 key 时显示 key 摘要':`GET ${balanceEndpoint({base_url:$('siteBase').value,balance_path:$('sitePath').value||'/v1/usage'})} · Authorization: Bearer API key`;
}
function setEditState(item){$('siteFormTitle').textContent=item?'编辑站点：'+item.name:'新增站点';$('saveSiteBtn').textContent=item?'保存修改':'保存配置';for(const row of document.querySelectorAll('.saved-site'))row.classList.toggle('selected',row.dataset.siteId===$('siteId').value);}
function newSite(){ $('siteForm').reset();$('siteId').value='';$('siteType').value='generic';$('sitePath').value='/v1/usage';$('siteKey').placeholder='首次填写 API key';$('siteManagementKey').placeholder='可选，OpenRouter 管理 key';settingsMessage('');updateSiteType();setEditState(null);}
function editSite(item){$('siteId').value=item.id;$('siteName').value=item.name;$('siteType').value=item.type;$('siteProvider').value=item.provider;$('siteBase').value=item.base_url;$('sitePath').value=item.balance_path;$('siteCurrency').value=item.currency||'auto';$('siteAliases').value=(item.aliases||[]).join(', ');$('siteKey').value='';$('siteManagementKey').value='';$('clearSiteKey').checked=false;$('clearManagementKey').checked=false;$('siteKey').placeholder=item.has_api_key?'已保存 ••••••••，留空保留':'填写 API key';$('siteManagementKey').placeholder=item.has_management_key?'已保存 ••••••••，留空保留':'可选，OpenRouter 管理 key';settingsMessage('');updateSiteType();setEditState(item);}
async function saveSite(andQuery=false){if(!$('siteForm').reportValidity())return;const buttons=[$('saveSiteBtn'),$('saveQueryBtn')];buttons.forEach(b=>b.disabled=true);try{const body={id:$('siteId').value||undefined,name:$('siteName').value,type:$('siteType').value,provider:$('siteProvider').value,base_url:$('siteBase').value,balance_path:$('sitePath').value,currency:$('siteCurrency').value,api_key:$('siteKey').value,management_key:$('siteManagementKey').value,clear_api_key:$('clearSiteKey').checked,clear_management_key:$('clearManagementKey').checked,aliases:$('siteAliases').value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)};const data=await siteRequest('/api/connections/save',body);$('siteId').value=data.id;$('siteKey').value='';$('siteManagementKey').value='';$('clearSiteKey').checked=false;$('clearManagementKey').checked=false;settingsMessage(data.message);await loadSites();const savedSite=siteConfigs.find(x=>x.id===data.id);if(savedSite){setEditState(savedSite);$('siteKey').placeholder=savedSite.has_api_key?'已保存 ••••••••，留空保留':'填写 API key';$('siteManagementKey').placeholder=savedSite.has_management_key?'已保存 ••••••••，留空保留':'可选，OpenRouter 管理 key';}await load();if(andQuery)await querySite(data.id);}catch(error){settingsMessage(error.message,true);}finally{buttons.forEach(b=>b.disabled=false);}}
async function querySite(id){settingsMessage('正在查询余额…');try{const data=await siteRequest('/api/connections/query',{id});settingsMessage(data.message);feedback(data.message);}catch(error){settingsMessage(error.message,true);feedback(error.message,true);}finally{await loadSites();}}
async function historySite(id){settingsMessage('正在同步模型历史…');try{const data=await siteRequest('/api/connections/history',{id});settingsMessage(data.message+(data.warnings?.length?' · '+data.warnings.join('；'):''));await load();}catch(error){settingsMessage(error.message,true);}}
async function deleteSite(item){if(!confirm('删除 '+item.name+' 的站点配置和保存的 key？已有用量记录保留。'))return;try{await siteRequest('/api/connections/delete',{id:item.id});if($('siteId').value===item.id)newSite();await loadSites();await load();settingsMessage('站点配置已删除');}catch(error){settingsMessage(error.message,true);}}
$('configureSitesBtn').onclick=async()=>{await loadSites();if(siteConfigs.length)editSite(siteConfigs[0]);else newSite();$('settingsDialog').showModal();};$('closeSettingsBtn').onclick=()=>$('settingsDialog').close();$('newSiteBtn').onclick=newSite;
$('siteType').onchange=()=>{if($('siteType').value==='deepseek'){$('siteBase').value='https://api.deepseek.com';$('sitePath').value='/user/balance';$('siteProvider').value='deepseek';$('siteCurrency').value='auto';}if($('siteType').value==='generic'){$('siteBase').value='';$('sitePath').value='/v1/usage';}updateSiteType();};$('siteBase').oninput=updateSiteType;$('sitePath').oninput=updateSiteType;$('siteCurrency').onchange=updateSiteType;
$('siteForm').onsubmit=e=>{e.preventDefault();saveSite();};$('saveQueryBtn').onclick=()=>saveSite(true);

$('themeSelect').onchange=()=>applyTheme($('themeSelect').value);
let initialTheme='valley';try{initialTheme=localStorage.getItem('llm-meter-theme')||'valley';}catch{}applyTheme(initialTheme);

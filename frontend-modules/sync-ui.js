const EVENT_KEY='dash_sync_events_v1';
const SOURCES={tasks:'TickTick',habits:'TickTick',roles:'Obsidian',health:'健康数据',local:'VPS JSON'};

export function syncDomainForRoute(route){
  if(/^api\/tasks\//.test(route))return 'tasks';
  if(route==='api/dashboard-data'||route==='api/boot')return 'core';
  if(/^api\/habits(?:\/|$)/.test(route))return 'habits';
  if(/^api\/roles\//.test(route)||route==='api/review/save'||route==='api/review/draft')return 'roles';
  if(/^api\/(health|stats|week|health-data)/.test(route)||route==='api/health')return 'health';
  if(route==='api/local-state')return 'local';
  return null;
}

export function createSyncUI({domains,escapeHTML,fetchActions,fetchShadow,fetchConflicts,ackConflict,reconcileShadow,
  fetchOutbox,fetchReceipts,retryWrite,retryAllWrites,discardWrite,fetchRecovery,restoreRecovery,fetchOps}){
  const esc=typeof escapeHTML==='function'?escapeHTML:(v=>String(v??''));
  const stateText=s=>s==='ok'?'已同步':s==='pending'?'同步中':s==='error'?'失败':'未检查';
  let actions=[],shadow=null,conflicts=null,outbox=[],receipts=[],recovery=[],ops=null;
  let events=[];
  try{events=JSON.parse(localStorage.getItem(EVENT_KEY)||'[]');if(!Array.isArray(events))events=[];}catch(_){events=[];}
  Object.entries(domains).forEach(([key,value])=>{if(!value.source)value.source=SOURCES[key]||'本地';});
  function saveEvents(){try{localStorage.setItem(EVENT_KEY,JSON.stringify(events.slice(-20)));}catch(_){}}
  function addEvent(domain,state,detail,now){const d=domains[domain];events.push({domain,label:d&&d.label||domain,state,detail:detail||'',ts:now});events=events.slice(-20);saveEvents();}
  const shortTime=ts=>ts?new Date(ts).toLocaleString([], {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';

  function render(nextActions){
    if(Array.isArray(nextActions))actions=nextActions;
    const status=document.getElementById('sync-domain-status'),values=Object.values(domains),waiting=outbox.filter(x=>x.status!=='sending').length;
    if(status){
      status.textContent=values.map(x=>x.label+(x.state==='ok'?'✓':x.state==='pending'?'…':x.state==='error'?'!':'·')).join('  ')+(waiting?'  待同步'+waiting:'');
      status.title=values.map(x=>`${x.label}（${x.source||'本地'}）：${stateText(x.state)}${x.detail?' · '+x.detail:''}`).join('\n')+(waiting?'\n有 '+waiting+' 条写入等待人工重试':'');
      status.dataset.state=values.some(x=>x.state==='error')||waiting?'error':values.some(x=>x.state==='pending')?'pending':'ok';
    }
    const panel=document.getElementById('shadowDetailPanel');if(!panel)return;
    const rows=values.map(x=>'<div class="sd-row"><span>'+esc(x.label)+'</span><strong class="sd-'+x.state+'">'+stateText(x.state)+'</strong><small>'+esc(x.source||'本地')+' · '+(x.updatedAt?new Date(x.updatedAt).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'—')+(x.detail?' · '+esc(x.detail):'')+'</small></div>').join('');
    const summary=shadow&&shadow.shadow_summary||{};
    const shadowHTML=shadow?'<div class="sd-list"><div class="sd-muted">影子数据对账 · '+esc(summary.checked_at||shadow.checked_at||'刚刚')+'</div><div class="sd-grid"><div class="sd-cell"><span class="sd-num">'+Number(summary.changed||0)+'</span><span class="sd-label">变更</span></div><div class="sd-cell"><span class="sd-num">'+Number(summary.missing||0)+'</span><span class="sd-label">缺失</span></div><div class="sd-cell"><span class="sd-num">'+Number(summary.extra||0)+'</span><span class="sd-label">多出</span></div></div></div>':'';
    const conflictCount=Number(conflicts&&conflicts.count||0),conflictItems=(conflicts&&conflicts.items||[]).slice(-3).reverse();
    const conflictHTML=conflictCount?'<div class="sd-list sd-conflicts"><div class="sd-muted">⚠️ 待人工确认的并发冲突 · '+conflictCount+' 条</div><ul>'+((conflictItems.map((x,i)=>'<li><strong>'+esc(x.key||'未知条目')+'</strong><small>'+esc(x.reason||'两端内容不同')+(Array.isArray(x.fields)&&x.fields.length?' · 字段：'+esc(x.fields.join('、')):'')+'</small>'+(typeof ackConflict==='function'?'<button type="button" class="sd-conflict-ack" data-conflict-index="'+i+'">已知悉</button>':'')+'</li>').join(''))||'<li class="sd-muted">已有记录，详情待下一次对账</li>')+'</ul><div class="sd-muted">“已知悉”只隐藏当前提示，不会覆盖 Mac/VPS 数据。</div></div>':'';
    const outboxHTML=outbox.length?'<div class="sd-list sd-outbox"><div class="sd-muted">待同步写入 · '+outbox.length+' 条（只手动重试，不静默覆盖）</div><ul>'+outbox.slice().reverse().map((x,i)=>'<li><strong>'+(x.status==='sending'?'⏳':'⚠️')+' '+esc(x.domain||x.route)+'</strong><small>'+esc(shortTime(x.createdAt)+' · '+x.route)+'</small>'+(x.error?'<em>'+esc(x.error)+'</em>':'')+'<button type="button" class="sd-write-retry" data-outbox-index="'+i+'">重试</button><button type="button" class="sd-write-discard" data-outbox-index="'+i+'">放弃</button></li>').join('')+'</ul><button type="button" class="sd-refresh sd-retry-all">全部重试</button></div>':'<div class="sd-list"><div class="sd-muted">✅ 没有等待同步的写入</div></div>';
    const recentReceipts=receipts.slice(-5).reverse();
    const receiptHTML=recentReceipts.map(x=>'<li><strong>'+(x.verified===false?'⚠️':'✅')+' '+esc(x.domain||x.route||'写入')+'</strong><small>'+esc(shortTime((x.ts||0)*1000||x.completedAt)+' · '+(x.source||'')+(x.entityId?' · '+x.entityId:''))+'</small></li>').join('')||'<li class="sd-muted">暂无服务器写入回执</li>';
    const recent=actions.slice(-5).reverse();
    const actionHTML=recent.map(a=>{const st=a.undoed?'已撤销':(a.success?(a.status||'成功'):(a.status||'失败'));return '<li><strong>'+(a.success?'✅':'❌')+' '+esc(a.kind||'动作')+'</strong><small>'+esc(shortTime(a.ts)+' · '+st+(a.entityId?' · ID '+a.entityId:'')+(a.undoable?' · 可撤销':''))+'</small>'+(a.message||a.error?'<em>'+esc(a.message||a.error)+'</em>':'')+'</li>';}).join('')||'<li class="sd-muted">暂无 AI 写入回执</li>';
    const recoveryHTML=recovery.length?'<div class="sd-list sd-recovery"><div class="sd-muted">数据恢复点（恢复前会再次快照当前版本）</div><ul>'+recovery.map((x,i)=>'<li><strong>'+(x.valid?'✅':'❌')+' '+esc(x.label)+'</strong><small>'+Number(x.backupCount||0)+' 个快照</small>'+(x.backupCount&&typeof restoreRecovery==='function'?'<button type="button" class="sd-restore" data-recovery-index="'+i+'">恢复上一版</button>':'')+'</li>').join('')+'</ul></div>':'';
    const deploy=ops&&ops.deploy||{},weekly=ops&&ops.weeklyDraft||{},safe=ops&&ops.recovery||{};
    const opsHTML=ops?'<div class="sd-list sd-ops"><div class="sd-muted">发布与自动任务</div><ul><li><strong>'+(deploy.status==='healthy'?'✅':'⚠️')+' 当前发布</strong><small>'+esc(deploy.release||'尚无发布记录')+(deploy.deployed_at?' · '+esc(shortTime(deploy.deployed_at)):'')+'</small></li><li><strong>'+(weekly.timer==='active'||weekly.timer==='development'?'✅':'⚠️')+' 周复盘补跑</strong><small>'+esc(weekly.timer||'未知')+(weekly.week?' · '+esc(weekly.week):'')+'</small></li><li><strong>'+(Number(safe.invalid||0)===0?'✅':'❌')+' 数据恢复基线</strong><small>'+Number(safe.withBackups||0)+' / '+Number(safe.domains||0)+' 个域有快照'+(safe.invalid?' · '+Number(safe.invalid)+' 个损坏':'')+'</small></li></ul></div>':'';
    const eventHTML=events.slice(-4).reverse().map(e=>'<li><strong>'+esc(e.label)+' · '+stateText(e.state)+'</strong><small>'+new Date(e.ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})+(e.detail?' · '+esc(e.detail):'')+'</small></li>').join('')||'<li class="sd-muted">暂无同步事件</li>';
    panel.innerHTML='<h3>数据同步与恢复</h3><div class="sd-domain-list">'+rows+'</div>'+opsHTML+outboxHTML+shadowHTML+conflictHTML+'<div class="sd-list"><div class="sd-muted">最近服务器写入回执</div><ul>'+receiptHTML+'</ul><div class="sd-muted">最近 AI 动作</div><ul>'+actionHTML+'</ul><div class="sd-muted">最近同步事件</div><ul>'+eventHTML+'</ul></div>'+recoveryHTML+'<button class="sd-refresh" type="button">刷新状态</button> <button class="sd-refresh sd-reconcile" type="button">运行完整对账</button>';
    const button=panel.querySelector('.sd-refresh');if(button)button.onclick=refresh;
    panel.querySelectorAll('.sd-write-retry').forEach(button=>button.onclick=async()=>{const item=outbox.slice().reverse()[Number(button.dataset.outboxIndex)];if(!item||!retryWrite)return;button.disabled=true;try{await retryWrite(item.id);}catch(_){}outbox=fetchOutbox();render();});
    panel.querySelectorAll('.sd-write-discard').forEach(button=>button.onclick=()=>{const item=outbox.slice().reverse()[Number(button.dataset.outboxIndex)];if(item&&discardWrite&&confirm('放弃这条待同步写入？服务器端已有的成功不会被撤销。')){discardWrite(item.id);outbox=fetchOutbox();render();}});
    const retryAll=panel.querySelector('.sd-retry-all');if(retryAll)retryAll.onclick=async()=>{retryAll.disabled=true;retryAll.textContent='重试中…';try{await retryAllWrites();}finally{outbox=fetchOutbox();render();}};
    panel.querySelectorAll('.sd-conflict-ack').forEach(button=>button.onclick=async()=>{const item=conflictItems[Number(button.dataset.conflictIndex)];if(!item||typeof ackConflict!=='function')return;button.disabled=true;button.textContent='记录中…';try{const result=await ackConflict(item);if(!result||result.success===false)throw new Error((result&&result.error)||'确认失败');conflicts=result;render();}catch(error){button.disabled=false;button.textContent='重试';button.title=error&&error.message||'确认失败';}});
    panel.querySelectorAll('.sd-restore').forEach(button=>button.onclick=async()=>{const item=recovery[Number(button.dataset.recoveryIndex)];if(!item||!restoreRecovery||!confirm('确认把“'+item.label+'”恢复到上一正常版本？当前版本会先自动保存为快照。'))return;button.disabled=true;button.textContent='恢复中…';try{const result=await restoreRecovery(item);if(!result||result.success===false)throw new Error(result&&result.error||'恢复失败');await refresh();}catch(error){button.disabled=false;button.textContent='重试恢复';button.title=error&&error.message||'恢复失败';}});
    const reconcileButton=panel.querySelector('.sd-reconcile');if(reconcileButton)reconcileButton.onclick=async()=>{reconcileButton.disabled=true;reconcileButton.textContent='对账中…';try{shadow=await reconcileShadow();render();}catch(error){reconcileButton.disabled=false;reconcileButton.textContent='重试完整对账';}};
  }

  function setDomain(domain,state,detail){const now=Date.now(),targets=domain==='core'?Object.keys(domains).filter(k=>k!=='local'):[domain];let changedAny=false;targets.forEach(key=>{const d=domains[key];if(!d)return;const changed=d.state!==state||d.detail!==(detail||'');if(!changed)return;changedAny=true;d.state=state;d.detail=detail||'';d.updatedAt=now;addEvent(key,state,detail,now);});if(changedAny)render();}
  function setOutbox(next){outbox=Array.isArray(next)?next:[];render();}
  async function refresh(){
    render();
    const calls=[fetchActions(),fetchShadow(),fetchConflicts?fetchConflicts():Promise.resolve(null),Promise.resolve(fetchOutbox?fetchOutbox():[]),fetchReceipts?fetchReceipts():Promise.resolve(null),fetchRecovery?fetchRecovery():Promise.resolve(null),fetchOps?fetchOps():Promise.resolve(null)];
    const results=await Promise.allSettled(calls);
    if(results[0].status==='fulfilled')actions=results[0].value&&results[0].value.actions||[];
    if(results[1].status==='fulfilled')shadow=results[1].value;
    if(results[2].status==='fulfilled')conflicts=results[2].value;
    if(results[3].status==='fulfilled')outbox=results[3].value||[];
    if(results[4].status==='fulfilled')receipts=results[4].value&&results[4].value.receipts||[];
    if(results[5].status==='fulfilled')recovery=results[5].value&&results[5].value.items||[];
    if(results[6].status==='fulfilled')ops=results[6].value;
    render();
  }
  function toggle(){const panel=document.getElementById('shadowDetailPanel'),badge=document.querySelector('.codex-build-badge');if(!panel||!badge)return;const open=!panel.classList.contains('show');panel.classList.toggle('show',open);badge.setAttribute('aria-expanded',String(open));if(open)refresh();}
  function init(){outbox=fetchOutbox?fetchOutbox():[];render();const badge=document.querySelector('.codex-build-badge');if(badge&&!badge.dataset.syncUiBound){badge.dataset.syncUiBound='1';badge.addEventListener('click',toggle);badge.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});}document.addEventListener('keydown',e=>{if(e.key==='Escape'){const panel=document.getElementById('shadowDetailPanel');if(panel)panel.classList.remove('show');}});}
  return {domains,events,render,setDomain,setOutbox,refresh,toggle,init,stateText};
}

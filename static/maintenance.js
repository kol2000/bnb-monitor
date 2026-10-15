'use strict';
let reconcileToken=null, auditPolling=false;
$('reconcile-preview').onclick=async()=>{
 const button=$('reconcile-preview');button.disabled=true;reconcileToken=null;$('reconcile-confirm').hidden=true;
 try{
  const p=await api('sheets/reconcile-preview','POST',{});
  reconcileToken=p.token;
  $('reconcile-list').innerHTML=`<p>В Google: ${p.count}. К удалению из мониторинга: ${p.removed.length}.</p>`+
   p.removed.map(w=>`<p>${escapeHtml(w.name)} · <span class="mono">${escapeHtml(w.address)}</span></p>`).join('');
  $('reconcile-confirm').hidden=!p.removed.length;
 }catch(e){toast(e.message,true);}finally{button.disabled=false;}
};
$('reconcile-confirm').onclick=async()=>{
 if(!reconcileToken||!confirm('Удалить перечисленные кошельки из мониторинга? История сохранится, ожидающие уведомления отменятся.'))return;
 const b=$('reconcile-confirm');b.disabled=true;
 try{const r=await api('sheets/reconcile-confirm','POST',{token:reconcileToken});reconcileToken=null;b.hidden=true;$('reconcile-list').textContent='Удалено: '+r.removed;await refresh();}
 catch(e){toast(e.message,true);}finally{b.disabled=false;}
};
function renderAudit(a){
 $('audit-start').disabled=a.state==='running';
 const states={running:'Проверяем',done:'Завершено',cancelled:'Остановлено: настройки Google изменились'};
 $('audit-status').textContent=a.state?`${states[a.state]||a.state}: ${(a.results||[]).length} из ${a.total}. ${a.updated?date(a.updated):''}`:'Проверка ещё не запускалась.';
 $('audit-results').innerHTML=(a.results||[]).map(r=>`<p class="${r.ok?'positive':'negative'}">${escapeHtml(r.name)} · ${escapeHtml(r.address)}<br>${escapeHtml(r.result)} · ${date(r.checked)}</p>`).join('');
}
$('audit-start').onclick=async()=>{
 $('audit-start').disabled=true;
 try{renderAudit(await api('seed-audit','POST',{}));}catch(e){toast(e.message,true);$('audit-start').disabled=false;}
};
setInterval(async()=>{
 if(!loggedIn||document.hidden||$('tab-settings').hidden||auditPolling)return;
 auditPolling=true;try{renderAudit(await api('seed-audit'));}catch{}finally{auditPolling=false;}
},5000);

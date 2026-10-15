'use strict';
let archiveJob=null,archiveBusy=false,archivePolling=false,archiveSelection=new Set();
const archiveStates={scanning:'Проверяем историю BSC',preview:'Предпросмотр готов',queued:'Повторная проверка перед переносом',validating:'Повторная проверка перед переносом',moving:'Перенос и проверка результата Google',uncertain:'Нужно проверить результат переноса',done:'Перенос завершён',failed:'Операция остановлена',cancelled:'Проверка отменена'};
const archiveResults={candidate:'Нет операций более 30 суток',active:'Была активность',no_history:'История не найдена — пропущен',busy:'Незавершённый перевод — пропущен',error:'История не подтверждена — пропущен'};
function archiveZero(w){return w.bnb!==null&&w.usdt!==null&&/^0(?:\.0+)?$/.test(w.bnb)&&/^0(?:\.0+)?$/.test(w.usdt);}
function renderArchive(d){
 $('archive-key-status').textContent=d.key_set?'Ключ сохранён':'Ключ не задан';
 const j=d.items[0];
 const active=j&&['scanning','preview','queued','validating','moving','uncertain'].includes(j.state);
 $('archive-scan').disabled=archiveBusy||!d.key_set||!!active;
 if(!j){$('archive-status').textContent='Проверка ещё не запускалась.';return;}
 if(!archiveJob||archiveJob.id!==j.id){archiveSelection=new Set();$('archive-select-empty').checked=false;}
 archiveJob=j;
 const candidates=j.results.filter(w=>w.state==='candidate');
 const n=['queued','validating'].includes(j.state)?`${j.progress} из ${j.selected.length}`:`${j.results.length} из ${j.total}`;
 $('archive-status').textContent=`${archiveStates[j.state]||j.state}: ${n}. Кандидатов: ${candidates.length}. ${j.error||''}`;
 $('archive-cancel').hidden=!['scanning','preview','queued','validating'].includes(j.state);
 $('archive-check').hidden=j.state!=='uncertain';
 $('archive-preview').hidden=!j.results.length;
 $('archive-confirm').hidden=j.state!=='preview';
 $('archive-select-empty').disabled=j.state!=='preview';
 $('archive-wallets').innerHTML=j.results.map(w=>`<tr><td><input type="checkbox" data-archive-address="${escapeHtml(w.address)}" aria-label="Выбрать ${escapeHtml(w.name)}" ${archiveSelection.has(w.address)?'checked':''} ${w.state!=='candidate'||j.state!=='preview'?'disabled':''}></td><td>${escapeHtml(w.name)}<br><small class="mono">${escapeHtml(w.address)}</small></td><td>${w.last_at?date(w.last_at):'—'}</td><td title="Последняя проверка BNB: ${escapeHtml(date(w.balance_checked))}; USDT: ${escapeHtml(date(w.usdt_checked))}">${escapeHtml(w.bnb??'не проверен')} BNB<br>${escapeHtml(w.usdt??'не проверен')} USDT</td><td title="${escapeHtml(w.reason||'')}">${escapeHtml(archiveResults[w.state]||w.state)}</td></tr>`).join('');
 archiveCount();
}
function archiveCount(){
 $('archive-confirm').textContent=`Переместить выбранные в архив (${archiveSelection.size})`;
 $('archive-confirm').disabled=archiveBusy||!archiveSelection.size;
}
async function refreshArchive(){renderArchive(await api('archive'));}
async function archiveAction(fn){
 if(archiveBusy)return;archiveBusy=true;
 $('archive-panel').querySelectorAll('button').forEach(b=>b.disabled=true);
 try{await fn();}catch(e){toast(e.message,true);}finally{
  archiveBusy=false;$('archive-panel').querySelectorAll('button').forEach(b=>b.disabled=false);
  try{await refreshArchive();}catch(e){toast(e.message,true);}
 }
}
$('archive-key-form').onsubmit=e=>{e.preventDefault();archiveAction(async()=>{
 await api('archive/key','POST',{key:$('archive-key').value,clear:$('archive-clear-key').checked});
 $('archive-key').value='';$('archive-clear-key').checked=false;toast('Настройки истории сохранены');
});};
$('archive-scan').onclick=()=>archiveAction(async()=>{await api('archive/scan','POST',{});});
$('archive-cancel').onclick=()=>archiveAction(async()=>{if(archiveJob)await api('archive/'+archiveJob.id+'/cancel','POST',{});});
$('archive-check').onclick=()=>archiveAction(async()=>{if(archiveJob)await api('archive/'+archiveJob.id+'/check','POST',{});});
$('archive-wallets').onchange=e=>{
 const address=e.target.dataset.archiveAddress;if(!address)return;
 if(e.target.checked)archiveSelection.add(address);else archiveSelection.delete(address);archiveCount();
};
$('archive-select-empty').onchange=e=>{
 archiveSelection=new Set(e.target.checked?archiveJob.results.filter(w=>w.state==='candidate'&&archiveZero(w)).map(w=>w.address):[]);
 $('archive-wallets').querySelectorAll('input').forEach(b=>b.checked=archiveSelection.has(b.dataset.archiveAddress));archiveCount();
};
$('archive-confirm').onclick=()=>archiveAction(async()=>{
 if(!archiveJob||!archiveSelection.size)return;
 const rows=archiveJob.results.filter(w=>archiveSelection.has(w.address));
 const nonzero=rows.filter(w=>!archiveZero(w)).length;
 if(!confirm(`Переместить ${rows.length} кошельков в «bnb кошельки (архив)»?\n\nС ненулевым или неизвестным балансом: ${nonzero}.\nПереносится вся строка, включая сид-фразу. Кошельки будут удалены из активного мониторинга; история событий останется.\n\nПеред переносом бот ещё раз проверит историю. Пока операция выполняется, не редактируйте и не сортируйте оба листа Google.`))return;
 await api('archive/'+archiveJob.id+'/confirm','POST',{addresses:[...archiveSelection]});
 toast('Начата повторная проверка перед переносом');
});
$('archive-panel').addEventListener('toggle',()=>{if($('archive-panel').open&&loggedIn)refreshArchive().catch(e=>toast(e.message,true));});
setInterval(async()=>{
 if(!loggedIn||document.hidden||!$('archive-panel').open||archiveBusy||archivePolling)return;
 archivePolling=true;try{await refreshArchive();}catch{}finally{archivePolling=false;}
},5000);

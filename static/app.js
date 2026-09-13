'use strict';
const $ = id => document.getElementById(id);
let csrf = '', loadedSettings = false, loadedGoogle = false, loggedIn = false, refreshing = false;
let toastTimer;
function toast(text, error=false) { $('toast').textContent=text; $('toast').className=error?'error':''; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,7000); }
async function api(path, method='GET', body) {
  const r=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});
  let d; try {d=await r.json();} catch {throw new Error('Сервер вернул неверный ответ. Проверьте соединение.');}
  if(!r.ok){if(r.status===401 && path!=='login'){loggedIn=false;showAuth();}throw new Error(d.error||'Ошибка запроса');}return d;
}
function showAuth(){$('login').hidden=loggedIn;$('dashboard').hidden=!loggedIn;$('logout').hidden=!loggedIn;}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const date=t=>t?new Date(t*1000).toLocaleString('ru-RU'):'Ещё не проверен';
const short=a=>a.slice(0,8)+'…'+a.slice(-6);
async function refresh(){
 if(!loggedIn||refreshing)return;refreshing=true;
 try{
 const d=await api('state');$('total').textContent=d.total;$('count').textContent=d.wallets.length;
 $('checked-count').textContent='С балансом: '+d.wallets.filter(w=>w.balance!==null).length+' из '+d.wallets.length;
 const s=d.status, stale=!s.worker_seen||Date.now()/1000-s.worker_seen>180;
 const g=d.settings;
 $('google-key-status').textContent=g.google_key_set?'Ключ Google установлен на сервере.':'Ключ не установлен. Выполните configure_google.py на VM (инструкция в README).';
 $('google-status').textContent=s.sheets_error?'Ошибка: '+s.sheets_error:s.sheets_success?'Последняя синхронизация: '+date(s.sheets_success)+' · адресов: '+s.sheets_counts.valid+' · добавлено: '+s.sheets_counts.added+' · переименовано: '+s.sheets_counts.renamed+' · неверных строк: '+s.sheets_counts.invalid:'Синхронизации ещё не было.';
 $('google-status').className='small '+(s.sheets_error?'negative':'muted');
 if(!loadedGoogle){$('sheets-enabled').checked=g.sheets_enabled;$('sheets-id').value=g.sheets_id;$('sheets-tab').value=g.sheets_tab;$('sheets-interval').value=g.sheets_interval;loadedGoogle=true;}
 // A long wallet scan updates the heartbeat file, but worker_seen is also refreshed during scans.
 $('monitor-status').textContent=stale?'Нет связи с монитором':s.running?'Проверяем…':s.error?'Ошибка RPC':'Работает';
 $('monitor-status').className='status-title '+(stale||s.error?'negative':'positive');
 $('last-check').textContent=s.last_cycle?'Цикл: '+date(s.last_cycle):'Ожидание первого цикла';
 $('rpc-error').hidden=!(stale||s.error);$('rpc-error').textContent=stale?'Фоновый процесс не отвечает. Проверьте: sudo docker compose ps и sudo docker compose logs worker':s.error;
 $('wallet-empty').hidden=d.wallets.length>0;
 $('wallet-list').innerHTML=d.wallets.map(w=>`<tr><td><div class="name">${escapeHtml(w.name)}</div><small><a class="mono" title="${escapeHtml(w.address)}" href="https://bscscan.com/address/${w.address}" target="_blank" rel="noopener noreferrer">${short(w.address)} ↗</a> <button class="quiet" data-copy="${w.address}" title="Копировать адрес">⧉</button></small></td><td class="mono">${w.bnb===null?'—':escapeHtml(w.bnb)}</td><td><small>${escapeHtml(date(w.checked))}</small>${w.error?`<small class="negative">${escapeHtml(w.error)}</small>`:''}</td><td><input aria-label="Уведомления ${escapeHtml(w.name)}" type="checkbox" data-notify="${w.id}" ${w.notify?'checked':''}></td><td><button class="quiet" data-rename="${w.id}" data-name="${escapeHtml(w.name)}" title="Переименовать">✎</button><button class="quiet danger" data-delete="${w.id}" title="Удалить">×</button></td></tr>`).join('');
 const delivery={pending:'В очереди',sent:'Отправлено',off:'Не требуется',cancelled:'Отменено'};
 $('event-empty').hidden=d.events.length>0;
 $('event-list').innerHTML=d.events.map(e=>`<tr><td>${escapeHtml(date(e.created))}<small>${escapeHtml(e.name)} · ${short(e.address)}</small></td><td class="mono ${e.delta.startsWith('-')?'negative':'positive'}">${e.delta.startsWith('-')?'':'+'}${escapeHtml(e.delta_bnb)}</td><td class="mono">${escapeHtml(e.new_bnb)}</td><td><a href="https://bscscan.com/block/${e.block}" target="_blank" rel="noopener noreferrer">${e.block} ↗</a></td><td>${delivery[e.delivery]||escapeHtml(e.delivery)}${e.delivery_error?`<small class="negative">${escapeHtml(e.delivery_error)} · попыток: ${e.attempts}</small>`:''}</td></tr>`).join('');
 if(!loadedSettings){const c=d.settings;$('interval').value=c.interval;$('threshold').value=c.threshold;$('rpc-urls').value=c.rpc_urls.join('\n');$('telegram-enabled').checked=c.telegram_enabled;$('telegram-chat').value=c.telegram_chat;$('token-hint').textContent=c.telegram_token_set?'Токен сохранён. Пустое поле оставит его без изменений.':'Токен ещё не задан.';loadedSettings=true;}
 }catch(e){toast(e.message,true);}finally{refreshing=false;}
}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const d=await api('login','POST',{password:$('password').value});csrf=d.csrf;$('password').value='';loggedIn=true;loadedSettings=false;showAuth();await refresh();}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('logout').onclick=async()=>{try{await api('logout','POST',{});location.reload();}catch(e){toast(e.message,true);}};
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>{x.classList.toggle('active',x===b);$('tab-'+x.dataset.tab).hidden=x!==b;});});
$('wallet-form').onsubmit=async e=>{e.preventDefault();const wallets=$('addresses').value.split('\n').map(s=>s.trim()).filter(Boolean).map(s=>{const [address,...parts]=s.split(/\s+/);return {address,name:parts.join(' ')};});try{const d=await api('wallets','POST',{wallets});$('addresses').value='';toast('Добавлено: '+d.added+'. Повторы пропущены.');await api('check','POST',{});await refresh();}catch(e){toast(e.message,true);}};
$('wallet-list').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;try{if(b.dataset.copy){try{await navigator.clipboard.writeText(b.dataset.copy);toast('Адрес скопирован');}catch{prompt('Скопируйте адрес:',b.dataset.copy);}return;}if(b.dataset.delete){if(!confirm('Удалить кошелёк из мониторинга? История сохранится, ожидающие уведомления отменятся.'))return;await api('wallets/'+b.dataset.delete,'DELETE',{});}if(b.dataset.rename){const name=prompt('Название кошелька:',b.dataset.name);if(name===null)return;await api('wallets/'+b.dataset.rename,'POST',{name});}await refresh();}catch(e){toast(e.message,true);}});
$('wallet-list').addEventListener('change',async e=>{const input=e.target;if(!input.dataset.notify)return;try{await api('wallets/'+input.dataset.notify,'POST',{notify:input.checked});}catch(e){input.checked=!input.checked;toast(e.message,true);}});
$('check').onclick=async()=>{try{await api('check','POST',{});toast('Проверка запрошена. Результат обновится автоматически.');}catch(e){toast(e.message,true);}};
$('settings-form').onsubmit=async e=>{e.preventDefault();try{await api('settings','POST',{interval:Number($('interval').value),threshold:$('threshold').value.trim(),rpc_urls:$('rpc-urls').value.split('\n').map(s=>s.trim()).filter(Boolean),telegram_enabled:$('telegram-enabled').checked,telegram_token:$('telegram-token').value.trim(),telegram_chat:$('telegram-chat').value.trim(),clear_token:$('clear-token').checked});$('telegram-token').value='';$('clear-token').checked=false;loadedSettings=false;toast('Настройки сохранены');await refresh();}catch(e){toast(e.message,true);}};
$('telegram-test').onclick=async()=>{const b=$('telegram-test');b.disabled=true;try{await api('telegram/test','POST',{});toast('Тестовое сообщение отправлено');}catch(e){toast(e.message,true);}finally{b.disabled=false;}};
(async()=>{try{const s=await api('session');csrf=s.csrf;loggedIn=s.authenticated;showAuth();await refresh();}catch(e){toast(e.message,true);}})();
$('google-form').onsubmit=async e=>{e.preventDefault();try{await api('settings','POST',{sheets_enabled:$('sheets-enabled').checked,sheets_id:$('sheets-id').value.trim(),sheets_tab:$('sheets-tab').value.trim(),sheets_interval:Number($('sheets-interval').value)});loadedGoogle=false;toast('Настройки Google сохранены');await refresh();}catch(e){toast(e.message,true);}};
$('google-sync').onclick=async()=>{try{await api('sheets/sync','POST',{});toast('Синхронизация запрошена');}catch(e){toast(e.message,true);}};
setInterval(refresh,5000);

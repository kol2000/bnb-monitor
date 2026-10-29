let loadedEmail=false;
'use strict';
// Theme preference belongs to this browser, not the server configuration.
(function () {
  const picker = document.getElementById('theme-select');
  function applyTheme(value) {
    const theme = value === 'light' ? 'light' : 'dark';
    document.documentElement.dataset.theme = theme;
    picker.value = theme;
  }
  let saved = 'dark';
  try { saved = localStorage.getItem('bnb-monitor-theme') || 'dark'; } catch {}
  applyTheme(saved);
  picker.addEventListener('change', () => {
    applyTheme(picker.value);
    try { localStorage.setItem('bnb-monitor-theme', picker.value); } catch {}
  });
})();
const $ = id => document.getElementById(id);
let csrf = '', loadedSettings = false, loadedGoogle = false, loggedIn = false, refreshing = false;
let toastTimer;
let checkingWallet = null;
let withdrawalBusy = false, loadedWithdrawal = false;
let activeWithdrawals = [], confirmingWallet = null;
function toast(text, error=false) { $('toast').textContent=text; $('toast').className=error?'error':''; $('toast').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('toast').hidden=true,7000); }
async function api(path, method='GET', body) {
  const r=await fetch('/api/'+path,{method,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body)});
  let d; try {d=await r.json();} catch {throw new Error('Сервер вернул неверный ответ. Проверьте соединение.');}
  if(!r.ok){if(r.status===401 && path!=='login'){loggedIn=false;showAuth();}const error=new Error(d.error||'Ошибка запроса');error.status=r.status;throw error;}return d;
}
async function copyText(text) {
  // Clipboard API requires HTTPS; LAN HTTP uses the synchronous copy command.
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try { await navigator.clipboard.writeText(text); return; } catch {}
  }
  const previousFocus = document.activeElement;
  const selection = window.getSelection();
  const ranges = selection ? Array.from({length:selection.rangeCount}, (_, i)=>selection.getRangeAt(i).cloneRange()) : [];
  const field = document.createElement('textarea');
  field.value = text;
  field.readOnly = true;
  field.className = 'clipboard-copy-buffer';
  field.setAttribute('aria-label', 'Копирование');
  document.body.appendChild(field);
  try {
    field.focus({preventScroll:true});
    field.select();
    field.setSelectionRange(0, field.value.length);
    if (!document.execCommand('copy')) throw new Error('Браузер запретил копирование. Проверьте разрешения буфера обмена.');
  } finally {
    field.remove();
    previousFocus?.focus({preventScroll:true});
    if (selection) {
      selection.removeAllRanges();
      for (const range of ranges) selection.addRange(range);
    }
  }
}

function showAuth(){$('login').hidden=loggedIn;$('dashboard').hidden=!loggedIn;$('logout').hidden=!loggedIn;}
function escapeHtml(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const date=t=>t?new Date(t*1000).toLocaleString('ru-RU'):'Ещё не проверен';
const short=a=>a.slice(0,8)+'…'+a.slice(-6);

const sortModes = ['balance-desc', 'balance-asc', 'name-asc', 'name-desc', 'usdt-desc', 'usdt-asc'];
let walletSort = 'balance-desc', walletRows = [];
let hideSmallBalances = false, usdtEnabled = true, staleAfter = 300;
function matchesWalletSearch(w){
 const q=$('wallet-search').value.trim().toLowerCase();
 if(q&&!`${w.name} ${w.address}`.toLowerCase().includes(q))return false;
 switch($('wallet-filter').value){
 case 'bnb':return w.balance!=null&&BigInt(w.balance)>0n;
 case 'usdt':return usdtEnabled&&w.usdt_balance!=null&&BigInt(w.usdt_balance)>0n;
 case 'unnamed':return !w.name||w.name.toLowerCase()===w.address.slice(0,8).toLowerCase();
 case 'stale':return !!w.stale;
 case 'account2':return w.account_number===2;
 case 'error':return !!(w.error||(usdtEnabled&&w.usdt_error));
 default:return true;
 }
}
function persistWalletFilters(){
 try{localStorage.setItem('bnb-wallet-filters', JSON.stringify({search:$('wallet-search').value,filter:$('wallet-filter').value}));}catch{}
 renderWallets();
}
try{const f=JSON.parse(localStorage.getItem('bnb-wallet-filters')||'{}');$('wallet-search').value=f.search||'';$('wallet-filter').value=f.filter||'all';}catch{}
$('wallet-search').oninput=persistWalletFilters;
$('wallet-filter').onchange=persistWalletFilters;
try {
  const saved = localStorage.getItem('bnb-monitor-wallet-sort');
  if (sortModes.includes(saved)) walletSort = saved;
  hideSmallBalances = localStorage.getItem('bnb-monitor-hide-small-balances') === 'true';
} catch {}
function matchesBalanceFilter(w) {
  if (!hideSmallBalances) return true;
  // The server marks positive amounts below one cent before rounding USD.
  // Unknown balances/quotes cannot establish that a wallet meets the threshold.
  if (usdtEnabled && w.usdt_balance != null && BigInt(w.usdt_balance) >= 10000000000000000n) return true;
  return w.balance != null && BigInt(w.balance) > 0n
    && typeof w.usd === 'string' && w.usd.startsWith('$') && w.usd !== '$0.00';
}
function compareWallets(a, b) {
  const nameOrder = String(a.name || a.address).localeCompare(String(b.name || b.address), 'ru', {sensitivity:'base'});
  const tie = nameOrder || a.address.localeCompare(b.address);
  if (walletSort.startsWith('name-')) return walletSort === 'name-desc' ? -tie : tie;
  const field = walletSort.startsWith('usdt-') ? 'usdt_balance' : 'balance';
  if (a[field] == null || b[field] == null) {
    if (a[field] == null && b[field] == null) return tie;
    return a[field] == null ? 1 : -1;
  }
  const av = BigInt(a[field]), bv = BigInt(b[field]);
  const order = av < bv ? -1 : av > bv ? 1 : 0;
  return (walletSort.endsWith('-desc') ? -order : order) || tie;
}
function checkLine(asset, checked, error, enabled=true) {
 const age=checked?Math.max(0,Math.floor(Date.now()/1000-checked)):null;
 const stale=age!==null&&age>staleAfter;
 const stamp=!enabled?'Выключен':age===null?'Ещё не проверен':age<60?`${age} сек. назад`:age<3600?`${Math.floor(age/60)} мин. назад`:`${Math.floor(age/3600)} ч. назад`;
 const detail=(error?'Ошибка: '+error+'. Последний успешный баланс сохранён. ':'')+date(checked);
 return `<small class="check-line ${enabled&&(error||stale)?'negative':''}" title="${escapeHtml(detail)}">${asset}: ${escapeHtml(stamp)}${enabled&&error?' · ошибка':enabled&&stale?' · устарел':''}</small>`;
}

function walletWithdrawalControl(w) {
 const active=activeWithdrawals.find(p=>String(p.wallet_id)===String(w.id)||p.source.toLowerCase()===w.address.toLowerCase()||(p.legs!==1&&p.middle.toLowerCase()===w.address.toLowerCase()));
 const confirming=confirmingWallet===String(w.id);
 const completed=active?.state==='done';
 const failed=active?.state==='failed';
 const waitingBalance=completed&&(!(Number(w.checked)>Number(active.updated))||!!w.error);
 const hasBalance=w.balance!=null&&BigInt(w.balance)>0n;
 const locked=active&&!completed&&!failed;
 const gasReserved=w.usdt_balance!=null&&BigInt(w.usdt_balance)>0n;
 if(!active&&!confirming&&!(w.balance!=null&&BigInt(w.balance)>0n))return '';
 let label='';
 if(confirming)label='Запускаем вывод…';
 else if(completed)label=waitingBalance?'✓ Выведено · баланс обновляется…':hasBalance?'✓ Последний вывод завершён':'✓ Выведено';
 else if(failed)label='Перевод отклонён — см. «Переводы»';
 else if(active)label=active.state==='blocked'?'Вывод приостановлен — см. «Переводы»':active.state==='pending2'?'Вывод в процессе · 2/2: на биржу':active.legs===1?'Вывод в процессе · на биржу':'Вывод в процессе · 1/2: на аккаунт 2';
 return `<small class="wallet-withdraw-line"><button class="quiet wallet-withdraw" data-withdraw="${w.id}" ${withdrawalBusy||locked||confirming||waitingBalance||!hasBalance||gasReserved?'disabled':''} title="${gasReserved?'Сначала выведите USDT: BNB нужен для газа':'Вывести BNB'}">Вывести</button>${label?`<span class="wallet-withdraw-status" role="status">${escapeHtml(label)}</span>`:''}</small>`;
}
function renderWallets() {
 const wallets = walletRows.filter(matchesBalanceFilter).filter(matchesWalletSearch).sort(compareWallets);
 $('wallet-empty').hidden=walletRows.length>0;
 $('wallet-filter-empty').hidden=walletRows.length===0 || wallets.length>0;
 $('wallet-visible-count').textContent='Показано: '+wallets.length+' из '+walletRows.length;
 $('wallet-list').innerHTML=wallets.map(w=>`<tr><td><div class="name"><button class="quiet" data-detail="${w.id}">${escapeHtml(w.name)}</button>${w.account_number?` <small>Аккаунт ${w.account_number}</small>`:''} <button class="quiet" data-rename="${w.id}" data-name="${escapeHtml(w.name)}" title="Переименовать" aria-label="Редактировать название кошелька">✎</button> <button class="quiet" data-copy="${escapeHtml(w.name)}" data-copy-kind="name" title="Копировать название кошелька" aria-label="Копировать название кошелька">⧉</button></div><small><a class="mono" title="${escapeHtml(w.address)}" href="https://bscscan.com/address/${w.address}" target="_blank" rel="noopener noreferrer">${short(w.address)} ↗</a> <button class="quiet" data-copy="${w.address}" title="Копировать адрес">⧉</button></small></td><td class="mono">${w.bnb===null?'—':escapeHtml(w.bnb)}${walletWithdrawalControl(w)}${typeof walletSwapControls==='function'?walletSwapControls(w):''}</td><td class="mono">${escapeHtml(w.usd ?? '—')}</td><td class="mono" title="${escapeHtml(usdtEnabled && w.usdt != null ? w.usdt + ' USDT' : '')}">${usdtEnabled ? (w.usdt == null ? '—' : '$' + formatUsdtTotal(w.usdt).replace(/\s/g,'').replace(',','.')) : 'Выключен'}</td><td class="wallet-checks">${checkLine('BNB', w.checked, w.error)}${checkLine('USDT', w.usdt_checked, w.usdt_error, usdtEnabled)}</td><td><input aria-label="Уведомления ${escapeHtml(w.name)}" type="checkbox" data-notify="${w.id}" ${w.notify?'checked':''}></td><td><div class="wallet-actions"><button class="quiet wallet-refresh" data-check="${w.id}" title="Принудительно обновить баланс сейчас" aria-label="Обновить баланс ${escapeHtml(w.name)}" ${checkingWallet !== null ? 'disabled' : ''}>${checkingWallet === String(w.id) ? 'Обновление…' : '↻ Обновить'}</button><button class="quiet danger wallet-delete" data-delete="${w.id}" title="Удалить кошелёк">Удалить</button></div></td></tr>`).join('');
}
$('wallet-sort').value = walletSort;
$('hide-small-balances').checked = hideSmallBalances;
$('hide-small-balances').addEventListener('change', e => {
  hideSmallBalances = e.target.checked;
  try { localStorage.setItem('bnb-monitor-hide-small-balances', String(hideSmallBalances)); } catch {}
  renderWallets();
});
$('wallet-sort').addEventListener('change', e => {
  walletSort = e.target.value;
  try { localStorage.setItem('bnb-monitor-wallet-sort', walletSort); } catch {}
  renderWallets();
});

function formatUsdtTotal(value) {
  const [whole, fraction = ''] = String(value).split('.');
  const digits = fraction.padEnd(3, '0');
  const cents = BigInt(whole) * 100n + BigInt(digits.slice(0, 2)) + (digits[2] >= '5' ? 1n : 0n);
  return (cents / 100n).toLocaleString('ru-RU') + ',' + String(cents % 100n).padStart(2, '0');
}

async function refresh(){
 if(!loggedIn||refreshing)return;refreshing=true;
 try{
 const d=await api('state');$('total').textContent=d.total;$('count').textContent=d.wallets.length;
 $('checked-count').textContent='С балансом: '+d.wallets.filter(w=>w.balance!==null).length+' из '+d.wallets.length;
 $('total-usd').textContent=d.total_usd ? '≈ '+d.total_usd : 'USD —';
 const quote=d.price;
 $('price-status').textContent=quote.bnb_usd
   ? '1 BNB ≈ $'+Number(quote.bnb_usd).toLocaleString('en-US',{maximumFractionDigits:2})+' · курс: '+date(quote.updated)+(quote.stale?' · УСТАРЕЛ':'')+(quote.error?' · '+quote.error:'')
   : (quote.error || 'Ожидаем курс CoinMarketCap');
 $('price-status').className='small '+(quote.stale || quote.error?'negative':'muted');
 const s=d.status, stale=!s.worker_seen||Date.now()/1000-s.worker_seen>180;
 const g=d.settings;
 usdtEnabled=g.usdt_enabled;
 $('total-usdt').textContent=usdtEnabled ? formatUsdtTotal(d.total_usdt) : 'Выключен';
 $('usdt-count').textContent=usdtEnabled ? 'Проверено: '+d.wallets.filter(w=>w.usdt_balance!==null).length+' из '+d.wallets.length : 'Включите в настройках';
 $('google-key-status').textContent=g.google_key_set?'Ключ Google установлен на сервере.':'Ключ не установлен. Выполните configure_google.py на VM (инструкция в README).';
 $('google-status').textContent=s.sheets_error?'Ошибка: '+s.sheets_error:s.sheets_success?'Последняя синхронизация: '+date(s.sheets_success)+' · адресов: '+s.sheets_counts.valid+' · добавлено: '+s.sheets_counts.added+' · переименовано: '+s.sheets_counts.renamed+' · неверных строк: '+s.sheets_counts.invalid:'Синхронизации ещё не было.';

 $('google-export-status').textContent=s.sheets_export_error?'Запись балансов: '+s.sheets_export_error:s.sheets_export_success?'Балансы записаны: '+date(s.sheets_export_success)+' · ячеек: '+s.sheets_export_count:'Запись балансов ещё не выполнялась.';
 $('google-export-status').className='small '+(s.sheets_export_error?'negative':'muted');
 $('google-status').className='small '+(s.sheets_error?'negative':'muted');
 if(!loadedGoogle){$('sheets-enabled').checked=g.sheets_enabled;$('sheets-id').value=g.sheets_id;$('sheets-tab').value=g.sheets_tab;$('sheets-interval').value=g.sheets_interval;loadedGoogle=true;}
 // A long wallet scan updates the heartbeat file, but worker_seen is also refreshed during scans.
 $('monitor-status').textContent=stale?'Нет связи с монитором':s.running?'Проверяем…':s.error?'Ошибка RPC':'Работает';
 $('monitor-status').className='status-title '+(stale||s.error?'negative':'positive');
 $('last-check').textContent=s.last_cycle?'Цикл: '+date(s.last_cycle):'Ожидание первого цикла';
 $('rpc-error').hidden=!(stale||s.error);$('rpc-error').textContent=stale?'Фоновый процесс не отвечает. Проверьте: sudo docker compose ps и sudo docker compose logs worker':s.error;
 if(d.health){
  $('checked-count').textContent=`Свежих: ${d.health.fresh} · устаревших: ${d.health.stale} · ошибок: ${d.health.errors}`;
  $('total-usd').textContent+=(d.health.stale||d.health.errors)?' · часть данных устарела':'';
 }
 if(s.running&&s.cycle_total)$('last-check').textContent+=` · ${s.cycle_done||0}/${s.cycle_total} проверок`;
 if(s.last_full_cycle)$('last-check').textContent+=' · полный успешный: '+date(s.last_full_cycle);
 if(s.last_cycle_duration)$('last-check').textContent+=` · ${Math.round(s.last_cycle_duration)} сек.`;
 staleAfter=Math.max(180,Number(d.settings.interval)*2+Math.max(0,s.last_cycle_duration||0));activeWithdrawals=d.active_withdrawals||[];walletRows=d.wallets;renderWallets();
 const ec=d.settings;
 if(!loadedEmail){
  $('email-enabled').checked=ec.email_enabled;
  for(const key of ['smtp_host','smtp_port','smtp_security','smtp_username','email_from','email_to']) $(key.replaceAll('_','-')).value=ec[key];
  $('smtp-password-hint').textContent=ec.smtp_password_set?'Пароль сохранён. Пустое поле оставит его без изменений.':'Пароль ещё не задан.';
  loadedEmail=true;
 }
 $('email-status').textContent=s.email_error || (s.email_last_sent?'Последнее письмо принято SMTP: '+date(s.email_last_sent):'Писем пока не отправлено');
 $('email-status').className='small '+(s.email_error?'negative':'muted');
 const delivery={pending:'В очереди',sent:'Отправлено',off:'Не требуется',cancelled:'Отменено',suppressed:'Не требуется — доставлено в Telegram'};
 $('event-empty').hidden=d.events.length>0;
 $('event-list').innerHTML=d.events.map(e=>`<tr><td>${escapeHtml(date(e.created))}<small>${escapeHtml(e.name)} · ${short(e.address)}</small></td><td class="mono ${e.delta.startsWith('-')?'negative':'positive'}">${e.delta.startsWith('-')?'':'+'}${escapeHtml(e.delta_bnb)} ${escapeHtml(e.asset)}</td><td class="mono">${escapeHtml(e.new_bnb)} ${escapeHtml(e.asset)}</td><td><a href="https://bscscan.com/block/${e.block}" target="_blank" rel="noopener noreferrer">${e.block} ↗</a></td><td><strong>${e.delivery==='sent'?'Доставлено в Telegram':e.email_delivery==='sent'?'Доставлено по email':e.delivery==='pending'||e.email_delivery==='pending'?'Ожидает доставки':'Не требуется'}</strong><br>Telegram: ${delivery[e.delivery]||escapeHtml(e.delivery)}${e.delivery_error?`<small class="negative">${escapeHtml(e.delivery_error)} · попыток: ${e.attempts}</small>`:''}<small>Email: ${e.email_delivery==='pending' && e.delivery==='pending' && e.attempts<3 ? 'Резерв — ожидает Telegram' : (delivery[e.email_delivery]||'Не требуется')}</small>${e.email_error?`<small class="negative">${escapeHtml(e.email_error)} · попыток: ${e.email_attempts}</small>`:''}</td></tr>`).join('');
 if(!loadedWithdrawal){await loadExchangeDeposits();loadedWithdrawal=true;}
 if(!loadedSettings){const c=d.settings;$('interval').value=c.interval;$('threshold').value=c.threshold;$('usdt-threshold').value=c.usdt_threshold;$('usdt-enabled').checked=c.usdt_enabled;$('rpc-urls').value=c.rpc_urls.join('\n');$('telegram-enabled').checked=c.telegram_enabled;$('telegram-digest').checked=!!c.telegram_digest;$('telegram-chat').value=c.telegram_chat;$('token-hint').textContent=c.telegram_token_set?'Токен сохранён. Пустое поле оставит его без изменений.':'Токен ещё не задан.';loadedSettings=true;}
 }catch(e){toast(e.message,true);}finally{refreshing=false;}
}
$('login-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const d=await api('login','POST',{password:$('password').value});csrf=d.csrf;$('password').value='';loggedIn=true;loadedSettings=false;showAuth();await refresh();}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$('logout').onclick=async()=>{try{await api('logout','POST',{});location.reload();}catch(e){toast(e.message,true);}};
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-tab]').forEach(x=>{x.classList.toggle('active',x===b);$('tab-'+x.dataset.tab).hidden=x!==b;});if(b.dataset.tab==='withdrawals')refreshWithdrawals();if(b.dataset.tab==='binance')openBinanceTab();if(['gate','bybit'].includes(b.dataset.tab))refreshVenueJobs(b.dataset.tab);if(b.dataset.tab==='swaps')refreshSwaps();if(b.dataset.tab==='generator')refreshGenerator();});
$('wallet-form').onsubmit=async e=>{e.preventDefault();const wallets=$('addresses').value.split('\n').map(s=>s.trim()).filter(Boolean).map(s=>{const [address,...parts]=s.split(/\s+/);return {address,name:parts.join(' ')};});try{const d=await api('wallets','POST',{wallets});$('addresses').value='';toast('Добавлено: '+d.added+'. Повторы пропущены.');await api('check','POST',{});await refresh();}catch(e){toast(e.message,true);}};
$('wallet-list').addEventListener('click',async e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.check){if(checkingWallet !== null)return;checkingWallet=b.dataset.check;renderWallets();try{await api('wallets/'+checkingWallet+'/check','POST',{});toast('Баланс кошелька обновлён');}catch(e){toast(e.message,true);}finally{checkingWallet=null;renderWallets();await refresh();}return;}try{if(b.hasAttribute('data-copy')){const isName=b.dataset.copyKind==='name';await copyText(b.dataset.copy);toast(isName?'Название кошелька скопировано':'Адрес скопирован');return;}if(b.dataset.delete){if(!await uiConfirm('Удалить кошелёк из мониторинга? История сохранится, ожидающие уведомления отменятся.'))return;await api('wallets/'+b.dataset.delete,'DELETE',{});}if(b.dataset.rename){const name=await uiPrompt('Название кошелька:',b.dataset.name);if(name===null)return;await api('wallets/'+b.dataset.rename,'POST',{name});}await refresh();}catch(e){toast(e.message,true);}});
$('wallet-list').addEventListener('change',async e=>{const input=e.target;if(!input.dataset.notify)return;try{await api('wallets/'+input.dataset.notify,'POST',{notify:input.checked});}catch(e){input.checked=!input.checked;toast(e.message,true);}});
$('check').onclick=async()=>{try{await api('check','POST',{});toast('Проверка запрошена. Результат обновится автоматически.');}catch(e){toast(e.message,true);}};
$('settings-form').onsubmit=async e=>{e.preventDefault();try{await api('settings','POST',{interval:Number($('interval').value),threshold:$('threshold').value.trim(),usdt_threshold:$('usdt-threshold').value.trim(),usdt_enabled:$('usdt-enabled').checked,rpc_urls:$('rpc-urls').value.split('\n').map(s=>s.trim()).filter(Boolean),telegram_digest:$('telegram-digest').checked,telegram_enabled:$('telegram-enabled').checked,telegram_token:$('telegram-token').value.trim(),telegram_chat:$('telegram-chat').value.trim(),clear_token:$('clear-token').checked});$('telegram-token').value='';$('clear-token').checked=false;loadedSettings=false;toast('Настройки сохранены');await refresh();}catch(e){toast(e.message,true);}};
$('telegram-test').onclick=async()=>{const b=$('telegram-test');b.disabled=true;try{await api('telegram/test','POST',{});toast('Тестовое сообщение отправлено');}catch(e){toast(e.message,true);}finally{b.disabled=false;}};
(async()=>{try{const s=await api('session');csrf=s.csrf;loggedIn=s.authenticated;showAuth();await refresh();}catch(e){toast(e.message,true);}})();
$('google-form').onsubmit=async e=>{e.preventDefault();try{await api('settings','POST',{sheets_enabled:$('sheets-enabled').checked,sheets_id:$('sheets-id').value.trim(),sheets_tab:$('sheets-tab').value.trim(),sheets_interval:Number($('sheets-interval').value)});loadedGoogle=false;toast('Настройки Google сохранены');await refresh();}catch(e){toast(e.message,true);}};
$('google-sync').onclick=async()=>{try{await api('sheets/sync','POST',{});toast('Синхронизация запрошена');}catch(e){toast(e.message,true);}};
setInterval(refresh,5000);


$('email-form').onsubmit=async e=>{
 e.preventDefault();const b=e.submitter;b.disabled=true;
 try{
  const data={email_enabled:$('email-enabled').checked,smtp_password:$('smtp-password').value,clear_smtp_password:$('clear-smtp-password').checked};
  for(const key of ['smtp_host','smtp_port','smtp_security','smtp_username','email_from','email_to']) data[key]=$(key.replaceAll('_','-')).value.trim();
  await api('settings','POST',data);
  $('smtp-password').value='';$('clear-smtp-password').checked=false;loadedEmail=false;
  toast('Настройки email сохранены');await refresh();
 }catch(err){toast(err.message,true);}finally{b.disabled=false;}
};
$('email-test').onclick=async()=>{
 const b=$('email-test');b.disabled=true;
 try{await api('email/test','POST',{});toast('Тестовое письмо принято SMTP. Проверьте входящие и спам.');}
 catch(err){toast(err.message,true);}finally{b.disabled=false;}
};




$('wallet-list').addEventListener('click',async e=>{
 const b=e.target.closest('[data-withdraw]');if(!b || withdrawalBusy)return;
 withdrawalBusy=true;renderWallets();
 try{
  const choice=await chooseDepositExchange('bnb'); if(!choice)return;
  toast('Рассчитываем маршрут и комиссии…');
  const p=await api('wallets/'+b.dataset.withdraw+'/withdraw/preview','POST',{exchange:choice.exchange});
  const accepted=p.legs===1?await uiConfirm(`Аккаунт 2 → ${p.exchange} (BSC)\nИсточник: ${p.source}\nПолучатель: ${p.destination}\nПолучит: ${p.value1} BNB\nКомиссия: ${p.fee} BNB\n\nПодтвердить один перевод?`):await uiConfirm(`Вывести BNB в сети BSC?\n\nПервый аккаунт:\n${p.source}\nБаланс: ${p.balance1} BNB\n\nВторой аккаунт этой сид-фразы:\n${p.middle}\nУже есть: ${p.balance2} BNB\nПолучит: ${p.value1} BNB\n\nБиржа: ${p.exchange}\n${p.destination}\nПолучит всего: ${p.value2} BNB\n\nКомиссия каждого перевода: ${p.fee} BNB.\nВыводится также имеющийся BNB второго аккаунта. USDT не переводится.\n\nПроверьте адрес биржи, сеть BSC и минимальную сумму депозита. Подтвердить оба перевода?`);
  if(accepted){confirmingWallet=String(b.dataset.withdraw);renderWallets();const result=await api('withdrawals/'+p.id+'/confirm','POST',{});activeWithdrawals=[result];toast('Вывод запущен. Статус и хеши — на вкладке «Переводы».');}
 }catch(err){toast(err.message,true);}finally{confirmingWallet=null;withdrawalBusy=false;renderWallets();await refreshWithdrawals();}
});
const withdrawalRetries=new Set();
async function refreshWithdrawals(){
 if(!loggedIn)return;
 try{
  const d=await api('withdrawals');
  activeWithdrawals=d.wallet_states||d.items;renderWallets();
  const labels={pending1:'Первый аккаунт → второй: ожидаем подтверждение',pending2:'Второй аккаунт → биржа: ожидаем подтверждение',done:'Оба перевода подтверждены',failed:'Перевод отклонён',blocked:'Вывод приостановлен',cancelled:'Вывод отменён до отправки'};
  $('withdrawal-empty').hidden=d.items.length>0;
  $('withdrawal-items').innerHTML=d.items.map(p=>`<p><strong>${escapeHtml(p.legs===1&&p.state==='pending1'?'Аккаунт 2 → биржа: ожидаем подтверждение':p.state==='done'?'Перевод завершён':labels[p.state]||p.state)}</strong> · ${escapeHtml(date(p.created))}<br><span class="mono">${escapeHtml(short(p.source))} → ${p.legs===1?'':escapeHtml(short(p.middle))+' → '}${escapeHtml(short(p.destination))}</span><br>На ${escapeHtml(p.exchange)}: ${escapeHtml(p.value2)} BNB${p.hash1?` · <a href="https://bscscan.com/tx/${escapeHtml(p.hash1)}" target="_blank" rel="noopener noreferrer">Перевод 1 ↗</a>`:''}${p.hash2?` · <a href="https://bscscan.com/tx/${escapeHtml(p.hash2)}" target="_blank" rel="noopener noreferrer">Перевод 2 ↗</a>`:''}${p.error?`<small class="negative">${escapeHtml(p.error)}</small>`:''}${p.can_retry?`<button class="quiet" data-withdraw-retry="${p.id}" ${withdrawalRetries.has(p.id)?'disabled':''}>${withdrawalRetries.has(p.id)?'Проверяем и повторяем…':'Повторить отправку'}</button>`:''}${p.can_cancel?`<button class="quiet" data-withdraw-cancel="${p.id}">Отменить расчёт</button>`:''}</p>`).join('');
 }catch(err){toast(err.message,true);}
}
setInterval(refreshWithdrawals,5000);

$('withdrawal-items').addEventListener('click',async e=>{
 const b=e.target.closest('[data-withdraw-cancel]');if(!b)return;
 try{await api('withdrawals/'+b.dataset.withdrawCancel+'/cancel','POST',{});toast('Расчёт отменён. Можно запустить новый вывод.');await refreshWithdrawals();}catch(err){toast(err.message,true);}
});

$('withdrawal-items').addEventListener('click',async e=>{
 const b=e.target.closest('[data-withdraw-retry]');if(!b)return;
 const id=b.dataset.withdrawRetry;if(withdrawalRetries.has(id))return;
 withdrawalRetries.add(id);b.disabled=true;b.textContent='Проверяем и повторяем…';
 try{const r=await api('withdrawals/'+id+'/retry','POST',{});toast(r.message);}
 catch(err){toast(err.message,true);}
 finally{withdrawalRetries.delete(id);await refreshWithdrawals();}
});


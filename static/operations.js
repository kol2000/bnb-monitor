'use strict';
let operationsPolling=false, currentOutboundIP=null;
const connectionNames={binance:'Binance · напрямую',telegram:'Telegram · маршрут приложения',rpc:'BSC RPC',google:'Google Таблицы'};
function renderOperations(data){
 $('connection-status').innerHTML=Object.entries(connectionNames).map(([key,label])=>{
  const r=data.connections[key]||{};
  return `<div><strong>${label}</strong><p class="${r.ok?'positive':r.checked?'negative':'muted'}">${escapeHtml(r.ok?r.detail:r.error||'Ожидает проверки')}</p><small>Проверка: ${date(r.checked)}<br>Последний успех: ${date(r.success)}</small></div>`;
 }).join('');
 $('diagnostics-check').disabled=!!data.running;
 $('diagnostics-check').textContent=data.running?'Проверяем…':'Проверить подключения';
 const ip=data.ip||{};currentOutboundIP=ip.current;
 $('outbound-ip').textContent=`Прямой внешний IPv4: ${ip.current||'—'} · сохранённый: ${ip.expected||'—'} · ${date(ip.checked)}${ip.error?' · '+ip.error:''}`;
 $('ip-warning').hidden=!ip.changed;
 $('ip-warning').textContent=`Внешний IP изменился: ${ip.expected} → ${ip.current}. Проверьте разрешённые IP API-ключа Binance. Приложение не меняет список доступа биржи.`;
 $('ip-acknowledge').hidden=!ip.changed||!!ip.error;
 $('priority-status').textContent=data.priority.length?`Приоритетная проверка балансов: в очереди ${data.priority.length}${data.priority.length===20?' или больше':''}. ${data.priority.find(x=>x.error)?.error||''}`:'Очередь приоритетных проверок пуста.';
 const b=data.backup||{};
 $('backup-status').innerHTML=b.name?`<p>${escapeHtml(b.name)} · ${date(b.created)}</p><p>Размер: ${b.size?(b.size/1048576).toFixed(2)+' МиБ':'—'} · Контрольные суммы: ${b.checksum?'OK':'не подтверждены'}</p><p>Проверка восстановления: ${b.verified?date(b.verified)+' · SQLite OK':'ещё не выполнена'}</p>${b.verify_error?`<p class="negative">${escapeHtml(b.verify_error)}</p>`:''}`:'Сведения появятся после следующего резервного копирования через manage.py.';
 const states={pending:'Ожидает отправки',sending:'Отправляется',sent:'Отправлено',unknown:'Доставка не подтверждена',cancelled:'Отменено — операция уже завершилась'};
 $('operation-notices').innerHTML=data.notices.map(n=>`<article class="binance-job"><strong>${escapeHtml(states[n.state]||n.state)}</strong> · ${date(n.created)} · ${escapeHtml(n.channel||'—')}<p style="white-space:pre-wrap">${escapeHtml(n.text)}</p>${n.error?`<small class="negative">${escapeHtml(n.error)}</small>`:''}</article>`).join('')||'<p class="muted">Новых уведомлений об операциях пока нет.</p>';
}
async function refreshOperations(){
 if(!loggedIn||document.hidden||operationsPolling)return;
 operationsPolling=true;
 try{renderOperations(await api('operations'));}catch{}finally{operationsPolling=false;}
}
$('diagnostics-check').onclick=async()=>{
 $('diagnostics-check').disabled=true;
 try{await api('diagnostics','POST',{});toast('Проверка подключений запрошена.');}catch(e){toast(e.message,true);$('diagnostics-check').disabled=false;}
};
$('ip-acknowledge').onclick=async()=>{
 const approvedIP=currentOutboundIP;
 if(!approvedIP||!await uiConfirm(`Вы проверили разрешённые IP ключа Binance? Сохранить ${approvedIP} как ожидаемый IP приложения? Настройки на Binance эта кнопка не меняет.`))return;
 try{await api('ip/acknowledge','POST',{ip:approvedIP});await refreshOperations();}catch(e){toast(e.message,true);}
};
setInterval(refreshOperations,8000);
refreshOperations();

'use strict';
let binanceBusy=false;
function binanceCanSellBnb(value){
 const match=/^(\d+)(?:\.(\d+))?$/.exec(String(value));
 return !!match && (/[1-9]/.test(match[1]) || /[1-9]/.test((match[2]||'').slice(0,4)));
}
const binanceStates={buying:'Проверка покупки',bought:'BNB куплен — ожидает вывода',withdrawing:'Вывод обрабатывается',done:'Вывод завершён',closed:'Операция закрыта'};
const binanceWithdrawStates={0:'Письмо отправлено',1:'Отменён',2:'Ожидает одобрения',3:'Отклонён',4:'Обработка',5:'Ошибка',6:'Завершён'};
async function binanceAction(fn){
 if(binanceBusy)return;
 binanceBusy=true;
 $('tab-binance').querySelectorAll('button').forEach(b=>b.disabled=true);
 try{await fn();}catch(e){toast(e.message,true);}finally{binanceBusy=false;$('tab-binance').querySelectorAll('button').forEach(b=>b.disabled=b.dataset.sellUnavailable==='true');await refreshBinanceJobs();}
}
async function refreshBinanceJobs(){
 if(!loggedIn)return;
 try{
 const result=await api('binance/jobs');
 if(result.cache?.binance_balances)renderBinanceBalances(result.cache.binance_balances);
 $('binance-auto').textContent=result.cache?.binance_balance_error||('Автопроверка: '+date(result.cache?.binance_poll?.checked)+` · следующая: ${date(result.cache?.binance_poll?.next_at)}. Повторная покупка или отправка не выполняется.`);
 $('binance-jobs').innerHTML=result.items.map(j=>{const d=j.data;if(d.action==='sell')return `<article class="binance-job"><strong>${escapeHtml(j.state==='done'?'Продажа завершена':j.state==='closed'?'Продажа закрыта':'Проверка продажи')}</strong> · ${date(j.created)}<p>Продажа BNB: ${escapeHtml(d.sold||d.quantity)}<br>Получено USDT: ${escapeHtml(d.received_usdt||'ожидается')}</p>${d.order_id?`<small>Ордер: ${escapeHtml(d.order_id)}</small>`:''}${j.error?`<p class="negative">${escapeHtml(j.error)}</p>`:''}${j.state==='buying'?`<button type="button" class="secondary" data-binance-check="${j.id}">Проверить статус</button>`:''}</article>`;return `<article class="binance-job"><strong>${escapeHtml(binanceStates[j.state]||j.state)}</strong> · ${date(j.created)}<p>Покупка: ${escapeHtml(d.total)} USDT · BNB: ${escapeHtml(d.bought||'ожидается')}<br>Сеть: ${escapeHtml(d.network)} · Получатель: <span class="binance-address">${escapeHtml(d.address)}</span></p>${d.order_id?`<small>Ордер: ${escapeHtml(d.order_id)}</small>`:''}${d.withdraw_id?`<small>Вывод: ${escapeHtml(d.withdraw_id)} · ${escapeHtml(binanceWithdrawStates[d.withdraw_status]||'Принят')}</small>`:''}${d.txid?`<p class="binance-address">Tx: ${escapeHtml(d.txid)}</p>`:''}${j.error?`<p class="negative">${escapeHtml(j.error)}</p>`:''}${['buying','withdrawing'].includes(j.state)?`<button type="button" class="secondary" data-binance-check="${j.id}">Проверить статус</button>`:''}${j.state==='bought'?`<button type="button" data-binance-withdraw="${j.id}">Рассчитать вывод BNB</button> <button type="button" class="quiet" data-binance-keep="${j.id}">Оставить BNB на бирже</button>`:''}</article>`;}).join('')||'<p class="muted">Операций из приложения пока нет.</p>';
 }catch(e){toast(e.message,true);}
}
async function openBinanceTab(){
 if(!loggedIn)return;
 await refreshBinanceData();
}
function refreshBinanceData(){return binanceAction(async()=>{
 $('binance-updated').classList.remove('negative');
 $('binance-updated').textContent='Загружаем баланс, сети и историю Binance…';
 if(!$('binance-history').innerHTML.trim())$('binance-history').innerHTML='<tr><td colspan="4">Загрузка истории выводов…</td></tr>';
 try{
 const d=await api('binance/refresh','POST',{});
 renderBinanceBalances(d);
 $('binance-updated').textContent='Обновлено: '+date(d.updated)+(d.warnings?.length?' · '+d.warnings.join(' · '):'');
 $('binance-updated').classList.toggle('negative',!!d.warnings?.length);
 const old=$('binance-network').value;
 $('binance-network').innerHTML='<option value="">Выберите сеть получателя</option>'+d.networks.filter(n=>n.withdrawEnable).map(n=>`<option value="${escapeHtml(n.network)}">${escapeHtml(n.network)} · комиссия ${escapeHtml(n.withdrawFee)} BNB · минимум ${escapeHtml(n.withdrawMin)}</option>`).join('');
 if(old&&[...$('binance-network').options].some(o=>o.value===old))$('binance-network').value=old;
 else if([...$('binance-network').options].some(o=>o.value==='BSC'))$('binance-network').value='BSC';
 $('binance-history').innerHTML=d.withdrawals.map(r=>`<tr><td>${binanceHistoryDate(r.applyTime)}</td><td>${escapeHtml(r.amount)} BNB<br><small>Комиссия: ${escapeHtml(r.transactionFee||'—')}</small></td><td class="binance-address">${escapeHtml(r.address)}<br><small>${escapeHtml(r.network||'—')}</small></td><td>${escapeHtml(binanceWithdrawStates[r.status]||r.status)}<small class="binance-address">${escapeHtml(r.txId||'')}</small></td></tr>`).join('')||(d.warnings?.some(x=>x.startsWith('История:'))?'<tr><td colspan="4">История не загружена. Повторите проверку.</td></tr>':'<tr><td colspan="4">Выводов BNB за 7 дней нет.</td></tr>');
 }catch(e){
 $('binance-updated').classList.add('negative');
 $('binance-updated').textContent='Не удалось обновить данные Binance: '+e.message+' Нажмите «Проверить баланс и выводы», чтобы повторить.';
 if($('binance-history').textContent==='Загрузка истории выводов…')$('binance-history').innerHTML='<tr><td colspan="4">История не загружена. Повторите проверку данных Binance.</td></tr>';
 throw e;
 }
});}
$('binance-refresh').onclick=refreshBinanceData;
$('binance-buy-form').onsubmit=e=>{e.preventDefault();binanceAction(async()=>{
 const j=await api('binance/preview','POST',{amount:$('binance-amount').value,address:$('binance-address').value,network:$('binance-network').value,memo:$('binance-memo').value});
 const d=j.data;
 if(!confirm(`Купить BNB по рынку?\n\nВведено: ${d.amount} USDT\nКомиссия: ≈ ${d.commission_percent}%\nДобавка: ${d.commission_extra} USDT\nСумма ордера: ${d.total} USDT\nПосле торговой комиссии: ≈ ${d.net_estimate} BNB.\nКомиссия вывода отдельно: ${d.fee} BNB.\n\nЗатем отдельное подтверждение вывода:\n${d.network}\n${d.address}\n\nРыночная цена может измениться.`))return;
 await api('binance/'+j.id+'/buy','POST',{});
 toast('Запрос покупки обработан. Статус обновится автоматически.');
 });};
$('binance-jobs').onclick=e=>{
 const b=e.target.closest('button');if(!b)return;
 binanceAction(async()=>{
 if(b.dataset.binanceCheck){await api('binance/'+b.dataset.binanceCheck+'/check','POST',{});return;}
 if(b.dataset.binanceKeep){if(confirm('Оставить купленный BNB на бирже и закрыть эту операцию?'))await api('binance/'+b.dataset.binanceKeep+'/keep','POST',{});return;}
 if(b.dataset.binanceWithdraw){const id=b.dataset.binanceWithdraw;const j=await api('binance/'+id+'/withdraw-preview','POST',{});const p=j.data.withdraw_plan;
 if(confirm(`Подтвердить вывод BNB с Binance?\n\nСеть: ${p.network}\nАдрес: ${p.address}\nMemo: ${p.memo||'нет'}\nСумма заявки: ${p.amount} BNB\nКомиссия Binance: ${p.fee} BNB\n\nКомиссия дополнительно зарезервирована из купленного BNB. Если биржа удерживает её из суммы заявки, получатель получит меньше; на бирже останется резерв.\n\nПроверьте адрес и сеть. Перевод необратим.`)){await api('binance/'+id+'/withdraw','POST',{});toast('Статус вывода обновится автоматически.');}}
 });
};

$('binance-balances').addEventListener('click',e=>{
 const sell=e.target.closest('#binance-sell');
 if(!sell||sell.disabled||sell.dataset.sellUnavailable==='true')return;
 binanceAction(async()=>{
  const j=await api('binance/sell-preview','POST',{});
  const d=j.data;
  if(!confirm(`Продать весь доступный BNB на споте Binance за USDT по рынку?\n\nДоступно: ${d.available} BNB\nК продаже: ${d.quantity} BNB\nОстаток из-за округления: ${d.dust} BNB\nОриентировочно: ${d.estimate} USDT до комиссии.\n\nИтог зависит от рыночной цены. Заблокированный BNB не продаётся.`))return;
  await api('binance/'+j.id+'/sell','POST',{});
  toast('Запрос продажи обработан. Статус обновится автоматически.');
 });
});

let binanceBalanceAt=0, binancePolling=false;
function renderBinanceBalances(d){
 if(d.updated<binanceBalanceAt)return;
 binanceBalanceAt=d.updated;
 $('binance-updated').classList.remove('negative');
 $('binance-balances').innerHTML=['USDT','BNB'].map(asset=>{const b=d.balances.find(x=>x.asset===asset)||{free:'0',locked:'0'};return `<div>${asset}: доступно ${escapeHtml(b.free)} · заблокировано ${escapeHtml(b.locked)} ${asset==='BNB'?`<button type="button" id="binance-sell" class="secondary" data-sell-unavailable="${!binanceCanSellBnb(b.free)}" ${!binanceCanSellBnb(b.free)?'disabled title="Для продажи нужно минимум 0,0001 BNB"':''}>Продать</button>`:''}</div>`;}).join('');
 $('binance-updated').textContent='Баланс обновлён: '+date(d.updated);
}
setInterval(async()=>{
 if(!loggedIn||document.hidden||$('tab-binance').hidden||binanceBusy||binancePolling)return;
 binancePolling=true;try{await refreshBinanceJobs();}finally{binancePolling=false;}
},5000);

function binanceHistoryDate(value){const text=String(value||'');const ms=/^\d+$/.test(text)?Number(text):Date.parse(text.replace(' ','T')+(/Z$/.test(text)?'':'Z'));return Number.isFinite(ms)?date(ms/1000):'—';}

'use strict';
let mexcBusy=false;
const mexcStates={buying:'Проверка покупки',bought:'BNB куплен — ожидает вывода',withdrawing:'Вывод обрабатывается',done:'Вывод завершён',closed:'Операция закрыта'};
const mexcWithdrawStates={1:'Заявка',2:'Проверка',3:'Ожидание',4:'Обработка',5:'Подготовка',6:'Подтверждения сети',7:'Завершён',8:'Ошибка',9:'Отменён',10:'Ручная проверка'};
async function mexcAction(fn){
 if(mexcBusy)return;
 mexcBusy=true;
 $('tab-mexc').querySelectorAll('button').forEach(b=>b.disabled=true);
 try{await fn();}catch(e){toast(e.message,true);}finally{mexcBusy=false;$('tab-mexc').querySelectorAll('button').forEach(b=>b.disabled=false);await refreshMexcJobs();}
}
async function refreshMexcJobs(){
 if(!loggedIn)return;
 try{
 const result=await api('mexc/jobs');
 $('mexc-jobs').innerHTML=result.items.map(j=>{const d=j.data;return `<article class="mexc-job"><strong>${escapeHtml(mexcStates[j.state]||j.state)}</strong> · ${date(j.created)}<p>Покупка: ${escapeHtml(d.total)} USDT · BNB: ${escapeHtml(d.bought||'ожидается')}<br>Сеть: ${escapeHtml(d.network)} · Получатель: <span class="mexc-address">${escapeHtml(d.address)}</span></p>${d.order_id?`<small>Ордер: ${escapeHtml(d.order_id)}</small>`:''}${d.withdraw_id?`<small>Вывод: ${escapeHtml(d.withdraw_id)} · ${escapeHtml(mexcWithdrawStates[d.withdraw_status]||'Принят')}</small>`:''}${d.txid?`<p class="mexc-address">Tx: ${escapeHtml(d.txid)}</p>`:''}${j.error?`<p class="negative">${escapeHtml(j.error)}</p>`:''}${['buying','withdrawing'].includes(j.state)?`<button type="button" class="secondary" data-mexc-check="${j.id}">Проверить статус</button>`:''}${j.state==='bought'?`<button type="button" data-mexc-withdraw="${j.id}">Рассчитать вывод BNB</button> <button type="button" class="quiet" data-mexc-keep="${j.id}">Оставить BNB на бирже</button>`:''}</article>`;}).join('')||'<p class="muted">Операций из приложения пока нет.</p>';
 }catch(e){toast(e.message,true);}
}
$('mexc-refresh').onclick=()=>mexcAction(async()=>{
 const d=await api('mexc/refresh','POST',{});
 $('mexc-balances').textContent=['USDT','BNB'].map(asset=>{const b=d.balances.find(x=>x.asset===asset)||{free:'0',locked:'0'};return `${asset}: доступно ${b.free} · заблокировано ${b.locked}`;}).join('\n');
 $('mexc-updated').textContent='Обновлено: '+date(d.updated);
 const old=$('mexc-network').value;
 $('mexc-network').innerHTML='<option value="">Выберите сеть получателя</option>'+d.networks.filter(n=>n.withdrawEnable).map(n=>`<option value="${escapeHtml(n.netWork)}">${escapeHtml(n.netWork)} · комиссия ${escapeHtml(n.withdrawFee)} BNB · минимум ${escapeHtml(n.withdrawMin)}</option>`).join('');
 if([...$('mexc-network').options].some(o=>o.value===old))$('mexc-network').value=old;
 $('mexc-history').innerHTML=d.withdrawals.map(r=>`<tr><td>${date(Number(r.applyTime)/1000)}</td><td>${escapeHtml(r.amount)} BNB<br><small>Комиссия: ${escapeHtml(r.transactionFee||'—')}</small></td><td class="mexc-address">${escapeHtml(r.address)}<br><small>${escapeHtml(r.network||'—')}</small></td><td>${escapeHtml(mexcWithdrawStates[r.status]||r.status)}<small class="mexc-address">${escapeHtml(r.txId||'')}</small></td></tr>`).join('')||'<tr><td colspan="4">Выводов BNB за 7 дней нет.</td></tr>';
});
$('mexc-buy-form').onsubmit=e=>{e.preventDefault();mexcAction(async()=>{
 const j=await api('mexc/preview','POST',{amount:$('mexc-amount').value,address:$('mexc-address').value,network:$('mexc-network').value,memo:$('mexc-memo').value});
 const d=j.data;
 if(!confirm(`Купить BNB по рынку?\n\nВведено: ${d.amount} USDT\nДобавка: 0,10 USDT\nСумма ордера: ${d.total} USDT\nОриентировочно: ${d.estimate} BNB (до торговой комиссии).\n\nЗатем отдельное подтверждение вывода:\n${d.network}\n${d.address}\n\nРыночная цена может измениться.`))return;
 await api('mexc/'+j.id+'/buy','POST',{});
 toast('Запрос покупки обработан. Проверьте статус в журнале перед выводом.');
 });};
$('mexc-jobs').onclick=e=>{
 const b=e.target.closest('button');if(!b)return;
 mexcAction(async()=>{
 if(b.dataset.mexcCheck){await api('mexc/'+b.dataset.mexcCheck+'/check','POST',{});return;}
 if(b.dataset.mexcKeep){if(confirm('Оставить купленный BNB на бирже и закрыть эту операцию?'))await api('mexc/'+b.dataset.mexcKeep+'/keep','POST',{});return;}
 if(b.dataset.mexcWithdraw){const id=b.dataset.mexcWithdraw;const j=await api('mexc/'+id+'/withdraw-preview','POST',{});const p=j.data.withdraw_plan;
 if(confirm(`Подтвердить вывод BNB с MEXC?\n\nСеть: ${p.network}\nАдрес: ${p.address}\nMemo: ${p.memo||'нет'}\nСумма заявки: ${p.amount} BNB\nКомиссия MEXC: ${p.fee} BNB\n\nКомиссия дополнительно зарезервирована из купленного BNB. Если биржа удерживает её из суммы заявки, получатель получит меньше; на бирже останется резерв.\n\nПроверьте адрес и сеть. Перевод необратим.`)){await api('mexc/'+id+'/withdraw','POST',{});toast('Проверьте статус вывода в журнале.');}}
 });
};

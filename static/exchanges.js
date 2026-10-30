'use strict';
const venueNames={gate:'Gate',bybit:'Bybit'};
const venueBusy={gate:false,bybit:false};
let depositRows=[];
async function loadExchangeDeposits(){
 depositRows=(await api('exchange-deposits')).items;renderDepositFields();
}
function renderDepositFields(){
 const r=depositRows.find(x=>x.exchange===$('deposit-exchange').value)||{};
 $('withdraw-binance-destination').value=r.bnb||'';$('deposit-usdt').value=r.usdt||'';
}
$('deposit-exchange').onchange=renderDepositFields;
$('withdrawal-form').onsubmit=async e=>{
 e.preventDefault();const exchange=$('deposit-exchange').value;
 const bnb=$('withdraw-binance-destination').value.trim().toLowerCase(),usdt=$('deposit-usdt').value.trim().toLowerCase();
 if(!await uiConfirm(`Сохранить депозиты ${exchange} · BSC?\nBNB: ${bnb||'отключён'}\nUSDT: ${usdt||'отключён'}\n\nПроверьте адреса на выбранной бирже.`))return;
 try{await api('exchange-deposits','POST',{exchange,bnb,usdt,confirm_bnb:bnb,confirm_usdt:usdt});await loadExchangeDeposits();toast('Адреса '+exchange+' сохранены');}catch(e){toast(e.message,true);}
};
async function chooseDepositExchange(asset){
 if(appDialogBusy)return null;appDialogBusy=true;
 try{
 // Fetch saved values again: never use an unsaved form field as the destination.
 const rows=(await api('exchange-deposits')).items.filter(r=>r[asset]);
 if(!rows.length)throw new Error('Сначала сохраните депозит '+asset.toUpperCase()+'/BSC в настройках.');
 const dialog=$('exchange-choice'),select=$('exchange-choice-select');
 select.replaceChildren();for(const r of rows){const o=document.createElement('option');o.value=r.exchange;o.textContent=r.exchange;select.append(o);}
 const change=()=>{$('exchange-choice-address').textContent=rows.find(r=>r.exchange===select.value)[asset];};select.onchange=change;change();
 dialog.returnValue='';
 return await new Promise(resolve=>{
  dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'?rows.find(r=>r.exchange===select.value):null),{once:true});
  $('exchange-choice-cancel').onclick=()=>dialog.close('cancel');$('exchange-choice-confirm').onclick=()=>dialog.close('confirm');dialog.showModal();
 });
 }finally{appDialogBusy=false;}
}
async function venueAction(v,fn){
 if(venueBusy[v])return;venueBusy[v]=true;
 const tab=$('tab-'+v);tab.querySelectorAll('button').forEach(b=>b.disabled=true);
 try{await fn();}catch(e){toast(e.message,true);}finally{venueBusy[v]=false;tab.querySelectorAll('button').forEach(b=>b.disabled=b.dataset.unavailable==='true');await refreshVenueJobs(v);}
}
async function refreshVenueJobs(v){
 if(!loggedIn)return;
 try{
 const r=await api('exchanges/'+v+'/jobs');
 const labels={ordering:'Проверка ордера',bought:'BNB куплен',withdrawing:'Вывод обрабатывается',done:'Завершено',closed:'Закрыто'};
 $(v+'-jobs').innerHTML=r.items.map(j=>`<div class="card"><strong>${escapeHtml(labels[j.state]||j.state)} · ${j.data.action==='sell'?'Продажа':'Покупка'}</strong><p>${escapeHtml(date(j.created))}</p>${j.data.execution?`<p>Исполнено: ${escapeHtml(j.data.execution.quantity)} BNB · ${escapeHtml(j.data.execution.quote)} USDT</p>`:''}${j.data.address?`<p class="mono">${escapeHtml(j.data.address)}</p>`:''}${j.data.withdrawal?`<p>Вывод: ${escapeHtml(j.data.withdrawal.status)}</p>`:''}${j.error?`<p class="negative">${escapeHtml(j.error)}</p>`:''}${['ordering','withdrawing'].includes(j.state)?`<button class="secondary" data-exchange="${v}" data-job="${j.id}" data-venue-action="check">Проверить статус</button>`:''}${v==='bybit'&&j.state==='withdrawing'&&!j.data.withdraw_id?` <button class="secondary" data-exchange="${v}" data-job="${j.id}" data-venue-action="link">Указать ID вывода</button>`:''}${j.state==='bought'?`<button class="withdraw-confirm" data-exchange="${v}" data-job="${j.id}" data-venue-action="withdraw-preview">Вывести BNB</button> <button class="secondary" data-exchange="${v}" data-job="${j.id}" data-venue-action="keep">Оставить на бирже</button>`:''}</div>`).join('')||'<p>Операций пока нет.</p>';
 if(venueBusy[v])$('tab-'+v).querySelectorAll('button').forEach(b=>b.disabled=true);
 }catch(e){if(!$('tab-'+v).hidden)toast(e.message,true);}
}
async function refreshVenue(v){
 const r=await api('exchanges/'+v+'/refresh','POST',{});
 $(v+'-balances').textContent=`${v==='bybit'?'Единый торговый — доступно':'Доступно'}: ${r.balances.BNB||'0'} BNB · ${r.balances.USDT||'0'} USDT`;
 if(v==='bybit'){
  const f=r.funding_balances;
  $('bybit-funding').textContent=f?`Финансирование — доступно: ${f.BNB||'0'} BNB · ${f.USDT||'0'} USDT`:'Финансирование: баланс не получен.';
 }
 $(v+'-updated').textContent='Проверено: '+date(r.updated);
 $(v+'-warnings').textContent=r.warnings.join('\n');
 $(v+'-sell').dataset.unavailable=String(!binanceCanSellBnb(r.balances.BNB||'0'));
 $(v+'-history').innerHTML=r.history.map(h=>`<p>${escapeHtml(h.amount)} BNB → <span class="mono">${escapeHtml(h.address)}</span> · ${escapeHtml(h.network)} · ${escapeHtml(h.status)}<br>ID: ${escapeHtml(h.id)}${/^0x[0-9a-fA-F]{64}$/.test(h.txid)?` · <a href="https://bscscan.com/tx/${h.txid}" target="_blank" rel="noopener noreferrer">Транзакция ↗</a>`:''}</p>`).join('')||'<p>Выводов в полученной истории нет.</p>';
}
async function venueTrade(v,body){
 const j=await api('exchanges/'+v+'/preview','POST',body),d=j.data;
 const message=d.action==='sell'?`Продать BNB на ${venueNames[v]} по рынку?\nДоступно: ${d.available} BNB\nК продаже: ${d.quantity} BNB\nРезерв комиссии / округление: ${d.dust} BNB\nОжидается до комиссии: ${d.estimate} USDT`:
 `Купить BNB на ${venueNames[v]}?\nЦель: ${d.amount} USDT\nСумма ордера: ${d.total} USDT\nТорговая комиссия: ≈ ${Number(d.market.fee)*100}%\nОжидается после покупки: ${d.estimate} BNB\nКомиссия вывода: ${d.network.fee} BNB\nОценка получения с запасом комиссии: ${d.recipient_estimate} BNB\n\nBSC → ${d.address}\nВывод подтверждается отдельно. Итог зависит от цены исполнения.`;
 if(await uiConfirm(message)){await api('exchanges/'+v+'/'+j.id+'/confirm','POST',{});toast('Статус операции проверяется автоматически.');}
}
for(const v of Object.keys(venueNames)){
 $(v+'-buy-form').onsubmit=e=>{e.preventDefault();venueAction(v,()=>venueTrade(v,{action:'buy',amount:$(v+'-amount').value,address:$(v+'-address').value.trim(),goal:$(v+'-goal').value}));};
 $('tab-'+v).addEventListener('click',e=>{
 const b=e.target.closest('[data-venue-action]');if(!b||b.disabled)return;
 venueAction(v,async()=>{
 const a=b.dataset.venueAction,id=b.dataset.job,base='exchanges/'+v+'/'+id+'/';
 if(a==='refresh'){await refreshVenue(v);return;}
 if(a==='sell'){await venueTrade(v,{action:'sell'});return;}
 if(a==='link'){const id=await uiPrompt('ID вывода из истории Bybit. Сверим адрес, сеть, сумму и время.','');if(id)await api(base+'check','POST',{withdraw_id:id.trim()});return;}
 if(a==='withdraw-preview'){
 const j=await api(base+a,'POST',{}),p=j.data.withdraw_plan;
 if(await confirmBinanceWithdrawal(`${venueNames[v]} → кошелёк\nСеть: BSC\nАдрес: ${p.address}\nСумма: ${p.amount} BNB\nКомиссия: ${p.fee} BNB\nНа бирже зарезервирована комиссия. Получение зависит от правил её удержания.`))await api(base+'withdraw','POST',{});
 return;
 }
 if(a==='keep'&&!await uiConfirm('Оставить купленный BNB на бирже и закрыть операцию?'))return;
 await api(base+a,'POST',{});
 });
 });
}
setInterval(()=>{for(const v of Object.keys(venueNames))if(!$('tab-'+v).hidden&&!venueBusy[v])refreshVenueJobs(v);},10000);

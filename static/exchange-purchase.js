'use strict';
// One dialog owns a single durable job. Polling only reads; dispatch is explicit and once.
const purchaseDialog=document.createElement('dialog');
purchaseDialog.id='exchange-purchase-dialog';
purchaseDialog.setAttribute('aria-labelledby','purchase-title');
purchaseDialog.innerHTML=`<h2 id="purchase-title">Покупка BNB → внешний кошелёк</h2>
<div id="purchase-content" aria-live="polite"></div><p id="purchase-error" class="negative" role="status"></p>
<div class="save-bar"><button type="button" id="purchase-close" class="secondary">Закрыть</button><button type="button" id="purchase-next" class="withdraw-confirm" hidden></button></div>`;
document.body.append(purchaseDialog);
let purchaseSession=null;
function purchaseBase(v){return v==='binance'?'binance':'exchanges/'+v;}
function purchaseOpen(v){
 if(appDialogBusy)return null;
 const s={venue:v,job:null,busy:false,step:'loading',plan:null};
 purchaseSession=s;appDialogBusy=true;
 $('purchase-title').textContent=(usdtNames[v]||v)+' · Купить BNB → вывести на кошелёк';
 $('purchase-error').textContent='';$('purchase-content').textContent='Рассчитываем покупку и комиссии…';
 $('purchase-next').hidden=true;purchaseDialog.showModal();return s;
}
function purchaseVisible(s){return purchaseSession===s&&purchaseDialog.open;}
function purchaseError(s,e){if(purchaseVisible(s))$('purchase-error').textContent=e.message||String(e);}
function purchaseRender(s){
 if(!purchaseVisible(s)||!s.job)return;
 const j=s.job,d=j.data,esc=escapeHtml;
 if(['done','closed'].includes(j.state))s.step='finished';
 const states={preview:'1. Подтвердите покупку',buying:'2. Ожидаем исполнения покупки',ordering:d.funding_pending?'2. Переводим USDT на торговый аккаунт':'2. Ожидаем исполнения покупки',bought:'3. BNB куплен — подтвердите вывод',withdrawing:'4. Ожидаем подтверждения вывода биржей',done:'✓ Вывод завершён. Можно запускать следующий перевод.',closed:'Операция закрыта. Проверьте результат ниже.'};
 let html=`<strong class="${j.state==='done'?'positive':''}">${esc(states[j.state]||j.state)}</strong>`;
 html+=`<p>Сеть: ${esc(typeof d.network==='string'?d.network:'BSC')}<br>Получатель: <span class="binance-address">${esc(d.address||'—')}</span>${d.memo?'<br>Memo: '+esc(d.memo):''}</p>`;
 html+=`<p>Цель: ${esc(d.amount)} USDT<br>Сумма ордера: ${esc(d.total)} USDT`;
 if(d.funding_amount&&Number(d.funding_amount)>0)html+=`<br>Финансирование → торговый аккаунт: ${esc(d.funding_amount)} USDT${d.funding_completed?' · подтверждено':''}`;
 if(j.state==='preview'){
  html+=`<br>Торговая комиссия: ≈ ${esc(d.commission_percent??(Number(d.market?.fee||0)*100))}%
<br>Ожидается после покупки: ${esc(d.net_estimate||d.estimate||'—')} BNB
<br>Комиссия вывода: ${esc(d.fee??d.network?.fee??'—')} BNB
<br>Оценка получения: ${esc(d.recipient_low||d.recipient_estimate||'—')}${d.recipient_high?' — '+esc(d.recipient_high):''} BNB`;
  if(d.total_extra)html+=`<br>Общая добавка к цели: ${esc(d.total_extra)} USDT`;
  html+='</p><p>Итог зависит от исполнения по рынку. После покупки здесь появятся фактическая сумма и подтверждение вывода.</p>';
 }else html+=`<br>Куплено после комиссии: ${esc(d.bought||'ожидается')} BNB<br>Потрачено: ${esc(d.spent||d.execution?.quote||'ожидается')} USDT</p>`;
 if(s.step==='withdraw-preview'&&s.plan){const p=s.plan;html+=`<p><b>Подтвердите вывод на кошелёк</b><br>Сеть: ${esc(p.network||'BSC')}<br>Адрес: <span class="binance-address">${esc(p.address)}</span><br>Memo: ${esc(p.memo||'нет')}<br>Сумма заявки: <b>${esc(p.amount)} BNB</b><br>Комиссия: ${esc(p.fee)} BNB</p><p>Комиссия зарезервирована; получение зависит от правил её удержания биржей. Перевод необратим.</p>`;}
 if(s.step==='planning')html+='<p>Рассчитываем фактическую сумму вывода и комиссию…</p>';
 if(d.withdraw_id)html+=`<p>ID вывода: ${esc(d.withdraw_id)}</p>`;
 if(d.withdrawal?.status!=null||d.withdraw_status!=null)html+=`<p>Статус биржи: ${esc(d.withdrawal?.status??binanceWithdrawStates[d.withdraw_status]??d.withdraw_status)}</p>`;
 const tx=d.txid||d.withdrawal?.txid;
 if(tx)html+=`<p class="binance-address">Транзакция: ${esc(tx)}</p>`;
 if(d.last_checked)html+=`<p class="muted">Последняя сверка: ${esc(date(d.last_checked))}</p>`;
 if(j.error)html+=`<p class="negative">${esc(j.error)}</p>`;
 if(['buying','ordering','withdrawing'].includes(j.state))html+='<p class="muted">Статус обновляется автоматически. Повторная покупка или вывод не отправляются. Закрытие окна не отменяет операцию.</p>';
 $('purchase-content').innerHTML=html;
 const b=$('purchase-next');b.hidden=!['buy-preview','withdraw-preview','plan-error'].includes(s.step);b.disabled=s.busy;
 b.textContent=s.step==='buy-preview'?'Подтвердить покупку':s.step==='plan-error'?'Пересчитать вывод':'Подтвердить вывод BNB';
}
async function purchasePlan(s){
 if(!purchaseVisible(s)||s.busy||s.job?.state!=='bought')return;
 s.busy=true;s.step='planning';purchaseRender(s);
 try{const j=await api(`${purchaseBase(s.venue)}/${s.job.id}/withdraw-preview`,'POST',{});s.job=j;s.plan=j.data.withdraw_plan;s.step='withdraw-preview';}
 catch(e){s.step='plan-error';purchaseError(s,e);}
 finally{s.busy=false;purchaseRender(s);}
}
async function startExchangePurchase(v,body){
 const s=purchaseOpen(v);if(!s)return;
 try{s.job=await api(purchaseBase(v)+'/preview','POST',body);s.step='buy-preview';purchaseRender(s);}
 catch(e){purchaseError(s,e);if(purchaseVisible(s))$('purchase-content').textContent='Расчёт не выполнен. Закройте окно, исправьте параметры и повторите расчёт.';}
}
async function openExchangePurchase(v,id){
 const s=purchaseOpen(v);if(!s)return;
 try{const r=await api(purchaseBase(v)+'/jobs');s.job=r.items.find(j=>j.id===id);if(!s.job)throw new Error('Операция не найдена. Обновите журнал.');s.step='tracking';purchaseRender(s);if(s.job.state==='bought')await purchasePlan(s);}
 catch(e){purchaseError(s,e);}
}
$('purchase-close').onclick=()=>purchaseDialog.close();
purchaseDialog.addEventListener('close',()=>{appDialogBusy=false;});
$('purchase-next').onclick=async()=>{
 const s=purchaseSession;if(!s||s.busy||!purchaseVisible(s))return;
 if(s.step==='plan-error'){await purchasePlan(s);return;}
 const action=s.step==='buy-preview'?(s.venue==='binance'?'buy':'confirm'):s.step==='withdraw-preview'?'withdraw':null;
 if(!action)return;
 s.busy=true;s.step='tracking';
 // Disable locally before the single POST, including when its response is lost.
 s.job={...s.job,state:action==='withdraw'?'withdrawing':s.venue==='binance'?'buying':'ordering'};
 $('purchase-error').textContent='';purchaseRender(s);
 try{s.job=await api(`${purchaseBase(s.venue)}/${s.job.id}/${action}`,'POST',{});}
 catch(e){purchaseError(s,new Error(e.message+' Проверяем сохранённую операцию. Повторная отправка заблокирована.'));}
 finally{s.busy=false;purchaseRender(s);if(s.job.state==='bought')await purchasePlan(s);}
};
async function purchasePoll(){
 const s=purchaseSession;if(!s||!purchaseVisible(s)||s.busy||!s.job||s.step!=='tracking')return;
 s.busy=true;
 try{const r=await api(purchaseBase(s.venue)+'/jobs');const j=r.items.find(x=>x.id===s.job.id);
  if(j){s.job=j;if(purchaseVisible(s))$('purchase-error').textContent='';}
 }catch(e){purchaseError(s,new Error('Не удалось обновить статус: '+e.message+'. Повторим проверку автоматически.'));}
 finally{s.busy=false;purchaseRender(s);if(s.job.state==='bought')await purchasePlan(s);}
}
setInterval(purchasePoll,3000);

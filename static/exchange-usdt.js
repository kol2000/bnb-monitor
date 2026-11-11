'use strict';
// A confirmation dispatches once. All automatic refreshes read the saved job only.
const usdtNames={binance:'Binance',gate:'Gate',bybit:'Bybit'};
const usdtBusy={};
let usdtCurrent=null,usdtDispatching=false;
const usdtDialog=document.getElementById('usdt-dialog');
function usdtSummary(j){
 const d=j.data,labels={preview:'Проверьте параметры',sending:'Вывод ожидает подтверждения',done:'Вывод завершён',failed:'Вывод отклонён / отменён биржей'};
 const source=j.venue==='bybit'?(d.source==='FUND'?'Финансирование':'Единый торговый'):'Спотовый';
 let s=`<strong>${escapeHtml(usdtNames[j.venue])} · ${escapeHtml(labels[j.state]||j.state)}</strong><p>${escapeHtml(source)} → внешний кошелёк · BSC (BEP20)</p><p class="mono">${escapeHtml(d.address)}</p><p>Сумма заявки: <b>${escapeHtml(d.amount)} USDT</b><br>Комиссия по расчёту: ${escapeHtml(d.fee)} USDT<br>Сумма с резервом комиссии: ${escapeHtml(d.reserve)} USDT</p>`;
 s+=j.venue==='bybit'?'<p class="muted small">Сумма заявки — к получению. Комиссия списывается дополнительно.</p>':'<p class="muted small">Комиссия зарезервирована дополнительно. Биржа может удержать её из суммы заявки; итоговая сумма появится после сверки.</p>';
 if(d.withdraw_id)s+=`<p>ID вывода: ${escapeHtml(d.withdraw_id)}</p>`;
 if(d.withdrawal)s+=`<p>Сумма по истории биржи: ${escapeHtml(d.withdrawal.amount)} USDT<br>Статус биржи: ${escapeHtml(String(d.withdrawal.status))}</p>`;
 if(/^0x[0-9a-fA-F]{64}$/.test(d.withdrawal?.txid||''))s+=`<a href="https://bscscan.com/tx/${d.withdrawal.txid}" target="_blank" rel="noopener noreferrer">Транзакция в BscScan ↗</a>`;
 if(d.last_checked)s+=`<p class="muted small">Последняя сверка: ${escapeHtml(date(d.last_checked))}</p>`;
 if(j.state==='done')s+='<p class="positive">✓ Операция завершена. Можно запускать следующий перевод.</p>';
 if(j.error)s+=`<p class="negative">${escapeHtml(j.error)}</p>`;
 return s;
}
function usdtShow(j){
 usdtCurrent=j;
 $('usdt-dialog-title').textContent='Вывод USDT · '+usdtNames[j.venue];
 $('usdt-dialog-content').innerHTML=usdtSummary(j);
 $('usdt-dialog-confirm').hidden=j.state!=='preview';
 $('usdt-dialog-confirm').disabled=usdtDispatching;
 $('usdt-dialog-close').textContent=j.state==='preview'?'Отмена':'Закрыть';
 if(!usdtDialog.open){if(appDialogBusy)return;appDialogBusy=true;usdtDialog.showModal();}
}
async function usdtRefresh(v){
 if(!loggedIn)return [];
 const error=document.querySelector(`[data-usdt-venue="${v}"] .usdt-history-error`);
 let r;
 try{r=await api('usdt-withdrawals/'+v);if(error)error.hidden=true;}
 catch(e){if(error){error.hidden=false;error.textContent='Не удалось загрузить историю USDT: '+e.message+' Повторим автоматически.';}throw e;}
 const box=document.querySelector(`[data-usdt-venue="${v}"] .usdt-jobs`);
 box.innerHTML=r.items.map(j=>`<article class="usdt-job">${usdtSummary(j)}<p class="muted small">${escapeHtml(date(j.created))}</p><button type="button" class="secondary" data-usdt-view="${j.id}">Открыть операцию</button>${j.state==='sending'?` <button type="button" class="secondary" data-usdt-check="${j.id}">Сверить с биржей</button>${v==='bybit'&&!j.data.withdraw_id?` <button type="button" class="secondary" data-usdt-link="${j.id}">Указать ID вывода</button>`:''}`:''}</article>`).join('')||'<p>Выводов USDT из приложения пока нет.</p>';
 if(usdtDialog.open&&usdtCurrent?.venue===v&&!usdtDispatching){
  const j=r.items.find(x=>x.id===usdtCurrent.id);
  if(j){const finished=usdtCurrent.state==='sending'&&['done','failed'].includes(j.state);usdtShow(j);if(finished){if(v==='binance')$('binance-refresh').click();else openVenueTab(v);}}
 }
 box._jobs=r.items;return r.items;
}
$('usdt-dialog-close').onclick=()=>usdtDialog.close();
usdtDialog.addEventListener('close',()=>{appDialogBusy=false;});
$('usdt-dialog-confirm').onclick=async()=>{
 if(usdtDispatching||usdtCurrent?.state!=='preview')return;
 const j=usdtCurrent;usdtDispatching=true;
 $('usdt-dialog-confirm').disabled=true;$('usdt-dialog-confirm').hidden=true;
 $('usdt-dialog-error').textContent='Отправляем заявку. Не создавайте повторный вывод.';
 // Locally leave preview BEFORE request; a lost response cannot enable resend.
 usdtCurrent={...j,state:'sending'};usdtShow(usdtCurrent);
 try{usdtCurrent=await api(`usdt-withdrawals/${j.venue}/${j.id}/confirm`,'POST',{});if(usdtDialog.open)usdtShow(usdtCurrent);$('usdt-dialog-error').textContent='';}
 catch(e){$('usdt-dialog-error').textContent=e.message+' Проверьте список операций перед новым расчётом.';}
 finally{usdtDispatching=false;await usdtRefresh(j.venue).catch(()=>{});}
};
for(const v of Object.keys(usdtNames)){
 const section=document.querySelector(`[data-usdt-venue="${v}"]`),form=section.querySelector('.usdt-form');
 form.elements.maximum.onchange=()=>{form.elements.amount.disabled=form.elements.maximum.checked;};
 form.onsubmit=async e=>{
  e.preventDefault();if(usdtBusy[v]||usdtDispatching||appDialogBusy)return;usdtBusy[v]=true;
  const submit=form.querySelector('button[type="submit"]');submit.disabled=true;
  try{
   const body={address:form.elements.address.value.trim(),amount:form.elements.amount.value,network:'BSC',source:form.elements.source.value,maximum:form.elements.maximum.checked};
   const j=await api('usdt-withdrawals/'+v+'/preview','POST',body);
   $('usdt-dialog-error').textContent='';usdtShow(j);
  }catch(e){toast(e.message,true);}finally{usdtBusy[v]=false;submit.disabled=false;}
 };
 section.addEventListener('click',async e=>{
  const b=e.target.closest('[data-usdt-view],[data-usdt-check],[data-usdt-link]');if(!b||usdtBusy[v]||usdtDispatching)return;
  const id=b.dataset.usdtView||b.dataset.usdtCheck||b.dataset.usdtLink;
  if(b.dataset.usdtView){const j=section.querySelector('.usdt-jobs')._jobs?.find(x=>x.id===id);if(j&&!appDialogBusy){$('usdt-dialog-error').textContent='';usdtShow(j);}return;}
  usdtBusy[v]=true;b.disabled=true;
  try{
   const body={};
   if(b.dataset.usdtLink){const wid=await uiPrompt('ID вывода USDT из истории Bybit. Проверим сумму, адрес, сеть и время.','');if(!wid)return;body.withdraw_id=wid.trim();}
   await api(`usdt-withdrawals/${v}/${id}/check`,'POST',body);await usdtRefresh(v);
  }catch(e){toast(e.message,true);}finally{usdtBusy[v]=false;b.disabled=false;}
 });
 // Load immediately when an existing tab switch unhides the exchange section.
 new MutationObserver(()=>{if(!$('tab-'+v).hidden&&loggedIn)usdtRefresh(v).catch(e=>toast(e.message,true));}).observe($('tab-'+v),{attributes:true,attributeFilter:['hidden']});
}
setInterval(()=>{for(const v of Object.keys(usdtNames))if(loggedIn&&(!$('tab-'+v).hidden||(usdtDialog.open&&usdtCurrent?.venue===v)))usdtRefresh(v).catch(()=>{});},3000);

'use strict';
let swapBusy=false, swapJobs=[];
function walletSwapControls(w){
 const busy=swapBusy||swapJobs.some(j=>['running','blocked'].includes(j.state));
 const job=swapJobs.find(j=>j.source.toLowerCase()===w.address.toLowerCase());
 const label=job?({running:'Операция в процессе',blocked:'Приостановлено — см. журнал',done:'✓ Операция завершена',failed:'Ошибка — см. журнал'}[job.state]||''):'';
 if(w.account_number===2)return '';
 const bnb=w.balance!=null&&BigInt(w.balance)>0n;
 const usdt=w.usdt_balance!=null&&BigInt(w.usdt_balance)>0n;
 return `<small class="wallet-withdraw-line">${bnb?`<button class="quiet" data-swap="${w.id}" ${busy?'disabled':''}>BNB → USDT</button>`:''}${usdt?`<button class="quiet wallet-withdraw" data-usdt-transfer="${w.id}" ${busy?'disabled':''}>Вывести USDT</button>`:''}${label?`<span role="status">${escapeHtml(label)}</span>`:''}</small>`;
}
async function refreshSwaps(){
 if(!loggedIn)return;
 try{
  const d=await api('swaps');swapJobs=d.items;
  const labels={running:'В процессе',blocked:'Приостановлено',done:'Завершено',failed:'Ошибка сети / исполнения',cancelled:'Отменено'};
  $('swaps-items').innerHTML=d.items.map(j=>`<p><strong>${j.kind==='swap'?'BNB → USDT':'USDT → аккаунт 2 → '+escapeHtml(j.exchange||'Binance')} · ${escapeHtml(labels[j.state]||j.state)}</strong><br>${escapeHtml(short(j.source))} · ${escapeHtml(j.amount)} ${j.kind==='swap'?'BNB':'USDT'} · ${escapeHtml(date(j.created))}<br>${j.kind==='usdt'?`Получатель: <span class="mono">${escapeHtml(j.destination)}</span><br>`:''}${j.received!==null?`Получено: ${escapeHtml(j.received)} USDT<br>`:''}${[...j.hashes,...(j.hash?[j.hash]:[])].map((h,i)=>`<a href="https://bscscan.com/tx/${escapeHtml(h)}" target="_blank" rel="noopener noreferrer">Транзакция ${i+1} ↗</a>`).join(' · ')}${j.error?`<small class="negative">${escapeHtml(j.error)}</small>`:''}${j.state==='blocked'?`<button class="secondary" data-swap-resume="${j.id}">Продолжить проверку</button>`:''}${j.can_cancel?` <button class="secondary" data-swap-cancel="${j.id}">Отменить до отправки</button>`:''}</p>`).join('')||'<p class="muted">Операций пока нет.</p>';
  renderWallets();
 }catch(e){if(!$('tab-swaps').hidden)toast(e.message,true);}
}
$('wallet-list').addEventListener('click',async e=>{
 const b=e.target.closest('[data-swap],[data-usdt-transfer]');if(!b||swapBusy)return;
 const transfer=b.hasAttribute('data-usdt-transfer');
 const wid=transfer?b.dataset.usdtTransfer:b.dataset.swap;
 let choice=null;
 try{choice=transfer?await chooseDepositExchange('usdt'):null;}catch(err){toast(err.message,true);return;}
 if(transfer&&!choice)return;
 swapBusy=true;renderWallets();
 try{
  const p=await api('wallets/'+wid+(transfer?'/usdt-transfer/preview':'/swap/preview'),'POST',transfer?{exchange:choice.exchange}:{});
  const message=transfer?
   `Перевести ${p.amount} USDT\nАккаунт 1: ${p.source}\nАккаунт 2: ${p.middle}\n${p.exchange} (BSC): ${p.destination}\n\nПополнение аккаунта 2: ${p.topup} BNB\nРезерв всего маршрута на аккаунте 1: ${p.reserve} BNB\nПрежний USDT аккаунта 2 останется на месте. Подтвердить маршрут?`:
   `Аккаунт 1: ${p.source}\nПродать ${p.amount} BNB через PancakeSwap V2\nОжидается: ${p.expected} USDT\nМинимум: ${p.minimum} USDT (проскальзывание 0,5%)\n\nМаксимальный газ обмена: ${p.fee} BNB\nПосле обмена останется не менее ${p.retained} BNB\nИз них резерв будущих переводов: ${p.reserve} BNB\nПотребность аккаунта 2: ${p.middle_reserve} BNB\n\nUSDT останутся на аккаунте 1. Подтвердить обмен?`;
  if(await uiConfirm(message,{title:transfer?'Вывод USDT на '+p.exchange:'Обмен BNB → USDT',confirmLabel:transfer?'Вывести USDT':'Обменять'})){
   await api('swaps/'+p.id+'/confirm','POST',{});
   toast('Операция запущена. Статус — на вкладке «Обмен BNB / USDT».');
  }
 }catch(e){toast(e.message,true);}
 finally{swapBusy=false;await refreshSwaps();renderWallets();}
});
$('swaps-items').addEventListener('click',async e=>{
 const b=e.target.closest('[data-swap-resume],[data-swap-cancel]');if(!b)return;
 const cancel=b.hasAttribute('data-swap-cancel'),id=cancel?b.dataset.swapCancel:b.dataset.swapResume;
 b.disabled=true;
 try{await api('swaps/'+id+(cancel?'/cancel':'/resume'),'POST',{});await refreshSwaps();}
 catch(e){toast(e.message,true);b.disabled=false;}
});
$('swaps-refresh').onclick=refreshSwaps;
setInterval(refreshSwaps,5000);

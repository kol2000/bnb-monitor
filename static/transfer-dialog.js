'use strict';
let transferSession=null;
function transferProgress(p,asset){
 if(p.state==='done')return '✓ Перевод завершён. Можно запускать следующий перевод.\nПодтверждено в сети BSC. Зачисление на бирже может занять дополнительное время.';
 if(p.state==='failed')return 'Перевод завершился с ошибкой. Проверьте детали в журнале операций.';
 if(p.state==='cancelled')return 'Операция отменена до отправки.';
 if(p.state==='blocked')return 'Операция приостановлена. Требуется проверка в журнале операций; завершение не подтверждено.';
 if(asset==='bnb')return p.state==='pending2'?'Шаг 2/2: аккаунт 2 → биржа. Ожидаем подтверждение сети.':p.legs===1?'Аккаунт 2 → биржа. Ожидаем подтверждение сети.':'Шаг 1/2: аккаунт 1 → аккаунт 2. Ожидаем подтверждение сети.';
 const total=Number(p.topup)>0?3:2;
 return `Подтверждено шагов: ${p.step||0} из ${total}. `+(Number(p.topup)>0&&p.step===0?'Пополнение BNB для комиссии.':p.step===total-1?'USDT: аккаунт 2 → биржа.':'USDT: аккаунт 1 → аккаунт 2.')+' Ожидаем подтверждение сети.';
}
function transferPreview(p,asset){
 if(asset==='usdt')return `Сеть BSC · ${p.amount} USDT\nАккаунт 1: ${p.source}\nАккаунт 2: ${p.middle}\nБиржа: ${p.exchange}\nПолучатель: ${p.destination}\n\nПополнение аккаунта 2: ${p.topup} BNB\nРезерв маршрута: ${p.reserve} BNB\nПрежний USDT аккаунта 2 останется на месте.`;
 return `Сеть BSC · Биржа: ${p.exchange}\nИсточник: ${p.source}\nБаланс: ${p.balance1} BNB\n`+(p.legs===1?'':`Аккаунт 2: ${p.middle}\nНа нём уже есть: ${p.balance2} BNB\nПолучит на первом шаге: ${p.value1} BNB\n`)+`\nПолучатель: ${p.destination}\nПолучит по расчёту: ${p.legs===1?p.value1:p.value2} BNB\nКомиссия каждого перевода: ${p.fee} BNB\n\n`+(p.legs===1?'':'Выводится также BNB второго аккаунта. ')+ 'USDT не переводится. Проверьте адрес и минимальный депозит биржи.';
}
function renderTransferStatus(s,p){
 s.result=p;
 $('transfer-status').textContent=transferProgress(p,s.asset)+(p.error?'\n'+p.error:'');
 $('transfer-status').className=p.state==='done'?'positive':['failed','blocked'].includes(p.state)?'negative':'';
 $('transfer-updated').textContent='Последняя проверка: '+date(Date.now()/1000);
 const hashes=[...(p.hashes||[]),p.hash,p.hash1,p.hash2].filter(h=>/^0x[0-9a-fA-F]{64}$/.test(h||''));
 $('transfer-links').innerHTML=[...new Set(hashes)].map((h,i)=>`<a href="https://bscscan.com/tx/${h}" target="_blank" rel="noopener noreferrer">Транзакция ${i+1} ↗</a>`).join(' · ');
 $('transfer-close').textContent=['done','failed','cancelled'].includes(p.state)?'Закрыть':'Скрыть — операция продолжится';
 $('transfer-reopen').textContent=p.state==='done'?'✓ Перевод завершён — открыть':'Открыть ход перевода';
}
async function pollTransfer(s){
 if(transferSession!==s||!$('transfer-dialog').open||s.polling)return;
 s.polling=true;
 try{
  const p=await api(s.base+'/'+s.plan.id);
  if(transferSession!==s)return;
  if(p.state==='preview'){
   $('transfer-status').textContent='Подтверждение отправки пока не получено. Повторная отправка не выполняется. Продолжаем сверку; при длительном ожидании откройте журнал операций.';
  }else renderTransferStatus(s,p);
 }catch(e){if(transferSession===s)$('transfer-status').textContent='Не удалось проверить статус: '+e.message+'\nЭто не означает отмену перевода. Проверка повторится автоматически.';}
 finally{
  s.polling=false;
  if(transferSession===s&&$('transfer-dialog').open&&!['done','failed','cancelled'].includes(s.result?.state))s.timer=setTimeout(()=>pollTransfer(s),3000);
 }
}
async function openWalletTransfer(wid,asset){
 if(appDialogBusy)return;
 if(transferSession&&['sending','tracking'].includes(transferSession.phase)&&!['done','failed','cancelled'].includes(transferSession.result?.state)){$('transfer-reopen').onclick();return;}
 const dialog=$('transfer-dialog'),select=$('transfer-exchange'),next=$('transfer-next');
 const s={asset,wid,base:asset==='bnb'?'withdrawals':'swaps',phase:'choose'};transferSession=s;appDialogBusy=true;
 $('transfer-title').textContent='Вывод '+asset.toUpperCase();
 $('transfer-options').hidden=false;$('transfer-details').textContent='Загружаем биржи…';
 $('transfer-status').textContent='';$('transfer-status').className='';$('transfer-updated').textContent='';$('transfer-links').innerHTML='';
 next.hidden=false;next.disabled=true;next.textContent='Рассчитать';$('transfer-close').textContent='Отмена';
 const closed=new Promise(resolve=>dialog.addEventListener('close',()=>{clearTimeout(s.timer);appDialogBusy=false;resolve();},{once:true}));
 $('transfer-close').onclick=()=>dialog.close();
 dialog.oncancel=()=>{};dialog.showModal();
 try{
  const rows=(await api('exchange-deposits')).items.filter(r=>r[asset]);
  if(transferSession!==s||!dialog.open)return;
  if(!rows.length)throw Error('Сначала сохраните депозит '+asset.toUpperCase()+'/BSC в настройках.');
  select.replaceChildren();for(const r of rows){const o=document.createElement('option');o.value=r.exchange;o.textContent=r.exchange;select.append(o);}
  const change=()=>{$('transfer-details').textContent='Адрес биржи: '+rows.find(r=>r.exchange===select.value)[asset];};select.onchange=change;change();next.disabled=false;
  next.onclick=async()=>{
   if(next.disabled)return;next.disabled=true;
   if(s.phase==='choose'){
    s.phase='calculating';select.disabled=true;$('transfer-status').textContent='Рассчитываем маршрут и комиссии…';
    try{
     s.plan=await api('wallets/'+wid+(asset==='bnb'?'/withdraw/preview':'/usdt-transfer/preview'),'POST',{exchange:select.value});
     if(transferSession!==s||!dialog.open)return;
     $('transfer-options').hidden=true;$('transfer-details').textContent=transferPreview(s.plan,asset);
     $('transfer-status').textContent='Проверьте расчёт и подтвердите перевод.';s.phase='confirm';next.textContent='Подтвердить перевод';next.disabled=false;
    }catch(e){if(transferSession!==s||!dialog.open)return;$('transfer-status').textContent=e.message;s.phase='choose';next.disabled=false;}
    finally{if(transferSession===s)select.disabled=false;}
   }else if(s.phase==='confirm'){
    s.phase='sending';next.hidden=true;$('transfer-close').textContent='Скрыть — операция продолжится';
    $('transfer-status').textContent='Отправляем подтверждение. Не закрывайте приложение…';
    $('transfer-reopen').hidden=false;$('transfer-reopen').textContent='Открыть ход перевода';
    try{const p=await api(s.base+'/'+s.plan.id+'/confirm','POST',{});if(p?.state)renderTransferStatus(s,p);}
    catch(e){$('transfer-status').textContent='Ответ на подтверждение не получен: '+e.message+'\nПроверяем сохранённую операцию. Повторно не отправляем.';}
    s.phase='tracking';await pollTransfer(s);
    refreshWithdrawals();refreshSwaps();
   }
  };
 }catch(e){$('transfer-status').textContent=e.message;}
 return closed;
}
$('transfer-reopen').onclick=()=>{
 const s=transferSession;if(!s?.plan||appDialogBusy)return;
 appDialogBusy=true;$('transfer-dialog').showModal();
 $('transfer-dialog').addEventListener('close',()=>{appDialogBusy=false;clearTimeout(s.timer);},{once:true});
 pollTransfer(s);
};

/* Public wallet details only; no seed material leaves the server. */
$('wallet-detail-close').onclick=()=>$('wallet-detail').close();
$('wallet-list').addEventListener('click',async e=>{
 const b=e.target.closest('[data-detail]');if(!b)return;
 try{
  const d=await api('wallets/'+b.dataset.detail), w=d.wallet;
  const x=escapeHtml;
  $('wallet-detail-content').innerHTML=`<h2>${x(w.name)}</h2>
   <p class="mono">${x(w.address)}</p><button data-detail-copy="${x(w.name)}">Копировать имя</button> <button data-detail-copy="${x(w.address)}">Копировать адрес</button>
   <p><a href="https://bscscan.com/address/${w.address}" target="_blank" rel="noopener noreferrer">BscScan ↗</a></p>
   <p><b>${x(w.bnb??'—')} BNB</b> · ${x(w.usd??'—')} · <b>${x(w.usdt??'—')} USDT</b></p>
   ${checkLine('BNB',w.checked,w.error)}${checkLine('USDT',w.usdt_checked,w.usdt_error,usdtEnabled)}
   <p>${w.account_number?'Аккаунт '+w.account_number+' · Связанный адрес: '+x(w.related_address):'Связь аккаунтов не проверена. Запустите проверку адресов и сид-фраз в настройках.'}</p>
   <h3>Изменения и доставка (последние 100)</h3>`+
   (d.events.map(e=>`<p>#${e.id} · ${x(date(e.created))} · ${x(e.delta_bnb)} ${x(e.asset)} → ${x(e.new_bnb)}<br>Telegram: ${x(e.delivery)} · email: ${x(e.email_delivery)}${e.delivery_error?'<br>'+x(e.delivery_error):''}${e.email_error?'<br>'+x(e.email_error):''}</p>`).join('')||'<p>Изменений пока нет.</p>')+
   '<h3>Переводы (последние 50)</h3>'+ (d.transfers.map(t=>`<p>${x(date(t.created))} · ${x(t.state)} · ${x(t.value2)} BNB → ${x(t.exchange)}<br>${x(t.destination)}${t.hash1?` · <a href="https://bscscan.com/tx/${x(t.hash1)}" target="_blank" rel="noopener noreferrer">Транзакция ↗</a>`:''}</p>`).join('')||'<p>Переводов пока нет.</p>');
  $('wallet-detail').showModal();
 }catch(e){toast(e.message,true);}
});
$('wallet-detail').addEventListener('click',async e=>{const b=e.target.closest('[data-detail-copy]');if(b){try{await copyText(b.dataset.detailCopy);toast('Скопировано');}catch(e){toast(e.message,true);}}});

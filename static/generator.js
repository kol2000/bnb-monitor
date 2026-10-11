'use strict';
let generatorBusy=false;
const generatorStates={prepared:'Подготовлено — требуется проверка',sending:'Проверяем запись',uncertain:'Требуется проверка записи',done:'Записано в Google Таблицу'};
function pendingGeneration(){try{return JSON.parse(sessionStorage.getItem('generator-pending')||'null');}catch(_){return null;}}
function showGeneratorItem(j){
 return `<article class="mexc-job"><strong>${escapeHtml(generatorStates[j.state]||j.state)}</strong> · ${date(j.created)}<p>Кошельков: ${j.count} · Лист: ${escapeHtml(j.tab)}</p>${j.error?`<p class="negative">${escapeHtml(j.error)}</p>`:''}<details><summary>Адреса кошельков</summary>${j.addresses.map(a=>`<p class="mexc-address">${escapeHtml(a)}</p>`).join('')}</details>${j.state!=='done'?`<button type="button" class="secondary" data-generator-check="${j.id}">Проверить запись</button>`:''}</article>`;
}
async function refreshGenerator(){
 if(!loggedIn)return;
 try{
  const d=await api('generator');
  $('generator-target').textContent=d.enabled?'Лист назначения: '+d.tab:'Сначала включите Google Таблицы в настройках.';
  $('generator-items').innerHTML=d.items.map(showGeneratorItem).join('')||'<p class="muted">Пока нет созданных партий.</p>';
  const pending=pendingGeneration();
  if(pending&&d.items.some(j=>j.id===pending.id))sessionStorage.removeItem('generator-pending');
  const active=d.items.some(j=>j.state!=='done');
  $('generator-submit').disabled=generatorBusy||!d.enabled||active;
  $('generator-count').disabled=generatorBusy||active||!!pendingGeneration();
  if(pendingGeneration()){
   $('generator-count').value=pendingGeneration().count;
   $('generator-status').textContent='Предыдущий ответ не получен. Нажмите «Сгенерировать кошельки» ещё раз: будет использован тот же запрос без создания дубликата.';
  }
 }catch(e){toast(e.message,true);}
}
$('generator-form').onsubmit=async e=>{
 e.preventDefault();if(generatorBusy)return;
 generatorBusy=true;$('generator-submit').disabled=true;$('generator-count').disabled=true;
 $('generator-status').textContent='Создаём кошельки и проверяем запись в Google…';
 try{
  let pending=pendingGeneration();
  if(!pending){
   const count=Number($('generator-count').value);
   if(!Number.isInteger(count)||count<1||count>100)throw Error('Введите целое число от 1 до 100.');
   const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);
   pending={id:Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join(''),count};
   sessionStorage.setItem('generator-pending',JSON.stringify(pending));
  }
  const j=await api('generator','POST',pending);
  sessionStorage.removeItem('generator-pending');
  $('generator-status').textContent=j.state==='done'?`Записано ${j.count} кошельков. Запрошена синхронизация списка.`:j.error||'Нажмите «Проверить запись» в журнале.';
 }catch(err){if(err.status===400)sessionStorage.removeItem('generator-pending');$('generator-status').textContent=err.message;toast(err.message,true);}
 finally{generatorBusy=false;await refreshGenerator();}
};
$('generator-items').onclick=async e=>{
 const b=e.target.closest('[data-generator-check]');if(!b||generatorBusy)return;
 generatorBusy=true;b.disabled=true;
 try{
  const j=await api('generator/'+b.dataset.generatorCheck+'/check','POST',{});
  $('generator-status').textContent=j.state==='done'?`Запись ${j.count} кошельков подтверждена.`:j.error;
 }catch(err){toast(err.message,true);}
 finally{generatorBusy=false;await refreshGenerator();}
};
$('generator-refresh').onclick=refreshGenerator;

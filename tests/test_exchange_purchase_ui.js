'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
function harness(){
 const elements={};
 function el(id){return elements[id]||(elements[id]={id,hidden:false,disabled:false,textContent:'',innerHTML:'',open:false,listeners:{},setAttribute(){},addEventListener(k,f){this.listeners[k]=f;},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}});}
 const calls=[],queue=[];
 const c={document:{createElement:()=>el('dialog'),body:{append(){}}},$:el,appDialogBusy:false,usdtNames:{binance:'Binance',gate:'Gate',bybit:'Bybit'},binanceWithdrawStates:{6:'Завершён'},escapeHtml:s=>String(s).replaceAll('<','&lt;'),date:String,setInterval(){},api:async(path,method,body)=>{calls.push({path,method,body});const f=queue.shift();if(!f)throw Error('Unexpected API '+path);return f(path);}};
 vm.createContext(c);vm.runInContext(fs.readFileSync('static/exchange-purchase.js','utf8'),c);
 return {c,el,calls,queue,run:s=>vm.runInContext(s,c)};
}
function job(state){return {id:'job1',state,data:{amount:'50',total:'50.1',address:'0xabc',network:'BSC',bought:'0.07',withdraw_plan:{amount:'0.069',fee:'0.001',address:'0xabc',network:'BSC'}}};}
(async()=>{
 for(const v of ['binance','gate','bybit']){
  const h=harness(),prefix=v==='binance'?'binance':'exchanges/'+v;
  h.queue.push(()=>job('preview'));await h.c.startExchangePurchase(v,{});
  assert(h.el('dialog').open);assert(!h.el('purchase-next').hidden);
  h.queue.push(()=>job(v==='binance'?'buying':'ordering'));
  await Promise.all([h.el('purchase-next').onclick(),h.el('purchase-next').onclick()]);
  assert.equal(h.calls.filter(x=>/\/(buy|confirm)$/.test(x.path)).length,1);
  assert(h.el('dialog').open);assert(h.el('purchase-next').hidden);
  h.queue.push(()=>({items:[job('bought')]}),()=>job('bought'));await h.c.purchasePoll();
  assert.equal(h.el('purchase-next').textContent,'Подтвердить вывод BNB');
  assert.equal(h.calls.filter(x=>x.path.endsWith('/withdraw')).length,0);
  h.queue.push(()=>job('withdrawing'));await h.el('purchase-next').onclick();
  assert(h.el('dialog').open);
  h.queue.push(()=>({items:[job('done')]}));await h.c.purchasePoll();
  assert(h.el('purchase-content').innerHTML.includes('Можно запускать следующий перевод'));
  assert.equal(h.calls.filter(x=>x.path===prefix+'/job1/withdraw').length,1);
 }
 // A lost POST response does not restore confirmation or resend; recover by GET.
 const h=harness();h.queue.push(()=>job('preview'));await h.c.startExchangePurchase('binance',{});
 h.queue.push(()=>{throw Error('timeout');});await h.el('purchase-next').onclick();
 await h.el('purchase-next').onclick();assert(h.el('purchase-next').hidden);
 assert.equal(h.calls.filter(x=>x.path.endsWith('/buy')).length,1);
 h.queue.push(()=>({items:[job('buying')]}));await h.c.purchasePoll();
 assert.equal(h.calls.at(-1).method,undefined);
 h.el('purchase-close').onclick();assert(!h.el('dialog').open);
 // Reopening an existing operation does not submit an order or a withdrawal.
 h.queue.push(()=>({items:[job('bought')]}),()=>job('bought'));
 await h.c.openExchangePurchase('binance','job1');assert(h.el('dialog').open);
 assert.equal(h.calls.filter(x=>x.path.endsWith('/buy')).length,1);
 // Closing while preview is in flight must not reopen the dialog.
 const x=harness();let resolve;x.queue.push(()=>new Promise(r=>resolve=r));
 const pending=x.c.startExchangePurchase('gate',{});x.el('purchase-close').onclick();resolve(job('preview'));await pending;
 assert(!x.el('dialog').open);
 console.log('Exchange purchase UI: three venues, single dialog, explicit withdrawal, no double dispatch, lost response recovery and reopen OK');
})().catch(e=>{console.error(e);process.exitCode=1;});

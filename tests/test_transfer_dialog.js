const assert=require('assert'),fs=require('fs'),vm=require('vm');
const els={};
function el(id){return els[id]||=( {hidden:false,disabled:false,textContent:'',value:'',open:false,listeners:{},append(o){if(!this.value)this.value=o.value;},replaceChildren(){this.value='';},addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);},showModal(){this.open=true;},close(){this.open=false;const fs=this.listeners.close||[];this.listeners.close=[];fs.forEach(f=>f());}});}
let calls=[],failConfirm=false,state='pending1';
const plan={id:'job1',state:'preview',legs:2,source:'a',middle:'b',destination:'c',balance1:'1',balance2:'0',value1:'.9',value2:'.8',fee:'.1',exchange:'Binance'};
const ctx={$:el,document:{createElement:()=>({})},appDialogBusy:false,date:String,setTimeout:()=>1,clearTimeout:()=>{},refreshWithdrawals:()=>{},refreshSwaps:()=>{},api:async(path,method)=>{calls.push([path,method]);if(path==='exchange-deposits')return {items:[{exchange:'Binance',bnb:'c',usdt:'c'}]};if(path.endsWith('/preview'))return {...plan};if(path.endsWith('/confirm')){if(failConfirm)throw Error('timeout');return {...plan,state};}return {...plan,state};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/transfer-dialog.js','utf8'),ctx);
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 const pending=ctx.openWalletTransfer(1,'bnb');await tick();assert(el('transfer-dialog').open);
 await el('transfer-next').onclick();assert(el('transfer-details').textContent.includes('Получит по расчёту'));assert(el('transfer-dialog').open);
 failConfirm=true;await el('transfer-next').onclick();assert(el('transfer-next').hidden);assert(el('transfer-status').textContent.includes('1/2'));
 await el('transfer-next').onclick();assert.equal(calls.filter(c=>c[0].endsWith('/confirm')).length,1);
 el('transfer-close').onclick();await pending;assert(!ctx.appDialogBusy);
 el('transfer-reopen').onclick();await tick();assert(el('transfer-dialog').open);
 state='done';await vm.runInContext('pollTransfer(transferSession)',ctx);assert(el('transfer-status').textContent.includes('Можно запускать следующий'));
 assert.equal(calls.filter(c=>c[0].endsWith('/confirm')).length,1);el('transfer-close').onclick();
 const cancel=ctx.openWalletTransfer(2,'usdt');await tick();el('transfer-close').onclick();await cancel;
 assert.equal(calls.filter(c=>c[0].endsWith('/confirm')).length,1);
 assert(!ctx.transferProgress({state:'blocked'},'bnb').includes('Можно запускать'));
 console.log('Transfer dialog: one confirmation, timeout recovery, close/reopen, success and cancellation OK');
})().catch(e=>{console.error(e);process.exitCode=1});

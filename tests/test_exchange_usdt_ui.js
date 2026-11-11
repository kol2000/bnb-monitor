const assert=require('assert'),fs=require('fs'),vm=require('vm');
const els={},sections={};
function el(id){return els[id]||={hidden:false,disabled:false,textContent:'',innerHTML:'',open:false,listeners:{},click(){},addEventListener(e,fn){this.listeners[e]=fn;},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}};}
for(const v of ['binance','gate','bybit']){
 const submit={},form={elements:{amount:{value:'10'},maximum:{checked:false},address:{value:'0x'+'1'.repeat(40)},source:{value:v==='bybit'?'FUND':'UTA'}},querySelector:()=>submit};
 const box={};sections[v]={form,box,querySelector:s=>s==='form'?form:box,addEventListener(e,fn){this[e]=fn;}};
}
let job=null,calls=[],fail=false,refreshes=0;
const ctx={$:el,document:{getElementById:el,querySelector(s){const v=s.match(/venue="([a-z]+)"/)[1];return s.endsWith('.usdt-jobs')?sections[v].box:sections[v];}},MutationObserver:class{observe(){}},loggedIn:true,appDialogBusy:false,
 escapeHtml:s=>String(s).replaceAll('<','&lt;'),date:String,toast(){},setInterval(){},openVenueTab(){refreshes++;},uiPrompt:async()=>null,
 api:async(path,method,body)=>{calls.push({path,method,body});if(path.endsWith('/preview'))return {id:'job1',venue:'bybit',created:1,state:'preview',data:{address:body.address,source:body.source,amount:'10',fee:'.15',reserve:'10.15'}};
 if(path.endsWith('/confirm')){job={id:'job1',venue:'bybit',created:1,state:'sending',data:{address:'0x'+'1'.repeat(40),source:'FUND',amount:'10',fee:'.15',reserve:'10.15'}};if(fail)throw Error('timeout');return job;}
 return {items:job?[job]:[]};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/exchange-usdt.js','utf8'),ctx);
(async()=>{
 const f=sections.bybit.form;
 await f.onsubmit({preventDefault(){}});assert(el('usdt-dialog').open);assert(el('usdt-dialog-content').innerHTML.includes('Финансирование'));
 el('usdt-dialog-close').onclick();assert(!ctx.appDialogBusy);assert.equal(calls.filter(x=>x.path.endsWith('/confirm')).length,0);
 await f.onsubmit({preventDefault(){}});fail=true;
 await el('usdt-dialog-confirm').onclick();assert(el('usdt-dialog-confirm').hidden);
 await el('usdt-dialog-confirm').onclick();assert.equal(calls.filter(x=>x.path.endsWith('/confirm')).length,1);
 el('usdt-dialog-close').onclick();assert(!ctx.appDialogBusy);
 // Reopen existing operation, not a new withdrawal.
 await sections.bybit.click({target:{closest:()=>({dataset:{usdtView:'job1'}})}});assert(el('usdt-dialog').open);assert(el('usdt-dialog-confirm').hidden);
 job={...job,state:'done'};await ctx.usdtRefresh('bybit');assert(el('usdt-dialog-content').innerHTML.includes('Можно запускать следующий'));assert.equal(refreshes,1);
 assert.equal(calls.filter(x=>x.path.endsWith('/confirm')).length,1);
 const escaped=ctx.usdtSummary({...job,data:{...job.data,address:'<img>',withdrawal:{amount:'10',status:'<script>',txid:'javascript:x'}}});assert(!escaped.includes('<img>'));assert(!escaped.includes('<script>'));assert(!escaped.includes('href='));
 f.elements.maximum.checked=true;f.elements.maximum.onchange();assert(f.elements.amount.disabled);
 console.log('USDT UI: explicit confirmation, cancellation, timeout no resend, reopen, completion and escaping OK');
})().catch(e=>{console.error(e);process.exitCode=1;});

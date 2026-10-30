const fs=require('fs'),vm=require('vm'),assert=require('assert');
const els={};
function $(id){return els[id] ||= {value:'Binance',textContent:'',innerHTML:'',hidden:true,dataset:{},disabled:false,
 addEventListener(e,fn){if(e==='close')this.closeHandler=fn;else this[e]=fn;},querySelectorAll(){return [];},replaceChildren(){this.children=[];},append(o){this.children.push(o);if(this.children.length===1)this.value=o.value;},
 showModal(){},close(value){this.returnValue=value;this.closeHandler?.();}};}
let accepted=false,calls=[],choice=null;
const rows=[{exchange:'Binance',bnb:'0x'+'1'.repeat(40),usdt:''},{exchange:'Gate',bnb:'0x'+'2'.repeat(40),usdt:'0x'+'3'.repeat(40)}];
const ctx={$,appDialogBusy:false,loggedIn:true,setInterval(){},document:{createElement(){return {}; }},date:String,escapeHtml:s=>String(s).replaceAll('<','&lt;'),toast(){},
 binanceCanSellBnb:v=>Number(v)>=0.0001,uiConfirm:async()=>accepted,uiPrompt:async()=>null,confirmBinanceWithdrawal:async()=>accepted,
 api:async(path,method,body)=>{calls.push({path,method,body});if(path==='exchange-deposits')return {items:rows};if(path.endsWith('/jobs'))return {items:[]};
 if(path.endsWith('/refresh'))return {balances:{BNB:'0',USDT:'2'},funding_balances:{BNB:'0.28',USDT:'170'},updated:1,warnings:[],history:[]};
 if(path.endsWith('/preview'))return {id:'abc',data:{action:'buy',amount:'50',total:'50.1',market:{fee:'0.001'},network:{fee:'0.00001'},address:rows[0].bnb}};return {ok:true};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/exchanges.js','utf8'),ctx);
(async()=>{
 await ctx.loadExchangeDeposits();assert.equal($('withdraw-binance-destination').value,rows[0].bnb);
 $('deposit-exchange').value='Gate';$('deposit-exchange').onchange();assert.equal($('withdraw-binance-destination').value,rows[1].bnb);
 calls=[];await $('withdrawal-form').onsubmit({preventDefault(){}});assert(!calls.some(x=>x.method==='POST'));
 accepted=true;await $('withdrawal-form').onsubmit({preventDefault(){}});let saved=calls.find(x=>x.method==='POST');assert.equal(saved.body.exchange,'Gate');assert.equal(saved.body.bnb,rows[1].bnb);
 const pending=ctx.chooseDepositExchange('usdt');await new Promise(setImmediate);
 $('exchange-choice-confirm').onclick();const selected=await pending;assert.equal(selected.exchange,'Gate');assert.equal(selected.usdt,rows[1].usdt);assert.equal(ctx.appDialogBusy,false);
 const cancelled=ctx.chooseDepositExchange('bnb');await new Promise(setImmediate);$('exchange-choice-cancel').onclick();assert.equal(await cancelled,null);
 calls=[];accepted=false;await ctx.venueTrade('bybit',{action:'buy',amount:'50'});assert(!calls.some(x=>x.path.endsWith('/confirm')));
 calls=[];accepted=true;await ctx.venueTrade('gate',{action:'buy',amount:'50'});assert.equal(calls.filter(x=>x.path.endsWith('/confirm')).length,1);
 calls=[];await ctx.openVenueTab('bybit');
 assert.equal(calls.filter(x=>x.path==='exchanges/bybit/refresh').length,1);
 assert($('bybit-funding').textContent.includes('170'));assert.equal($('bybit-sell').dataset.unavailable,'false');
 assert(calls.some(x=>x.path==='exchanges/bybit/jobs'));
 calls=[];await Promise.all([ctx.openVenueTab('gate'),ctx.openVenueTab('gate')]);
 assert.equal(calls.filter(x=>x.path==='exchanges/gate/refresh').length,1);
 const originalApi=ctx.api;ctx.api=async(path,...args)=>{if(path.endsWith('/refresh'))throw new Error('Недоступен API');return originalApi(path,...args);};
 await ctx.openVenueTab('bybit');assert($('bybit-warnings').textContent.includes('Недоступен API'));assert($('bybit-warnings').textContent.includes('устаревшими'));
 ctx.api=originalApi;await ctx.openVenueTab('bybit');assert.equal($('bybit-warnings').textContent,'');
 assert(fs.readFileSync('static/app.js','utf8').includes('openVenueTab(b.dataset.tab)'));
 const page=fs.readFileSync('templates/index.html','utf8');for(const id of Object.keys(els))assert(page.includes(`id="${id}"`),id);
 assert(page.includes('data-tab="gate"'));assert(page.includes('data-tab="bybit"'));
 assert(!fs.readFileSync('static/app.js','utf8').includes("'/withdraw/preview','POST',{}"));
 console.log('Exchange UI: venue-specific deposits, explicit approval, cancellation and DOM wiring OK');
})().catch(e=>{console.error(e);process.exitCode=1;});

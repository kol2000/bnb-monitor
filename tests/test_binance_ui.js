// Run with node. Exercise actual exchange UI functions using a minimal DOM adapter.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={};
function $(id){return elements[id] ||= {innerHTML:'',textContent:'',disabled:false,hidden:false,value:'',classList:{remove(){},toggle(){}},addEventListener(){},querySelectorAll(){return []}};}
const context={$ , loggedIn:true,document:{hidden:false},setInterval(){},Date,Number,console,escapeHtml:s=>String(s),date:n=>new Date(n*1000).toISOString(),toast(){},api:async()=>({items:[],cache:{}})};
vm.createContext(context);
vm.runInContext(fs.readFileSync('static/binance.js','utf8'),context);
assert(context.binanceCanSellBnb('0.0001'));assert(!context.binanceCanSellBnb('0.00009999'));assert(!context.binanceCanSellBnb('NaN'));
assert.equal(context.binanceHistoryDate('2026-10-21 10:00:00'),'2026-10-21T10:00:00.000Z');
assert.equal(context.binanceHistoryDate('invalid'),'—');
context.renderBinanceBalances({updated:1,balances:[{asset:'BNB',free:'0.00001',locked:'0'}]});
assert($('binance-balances').innerHTML.includes('disabled title='));
assert($('binance-balances').innerHTML.includes('data-sell-unavailable="true"'));
context.renderBinanceBalances({updated:2,balances:[{asset:'BNB',free:'0.1',locked:'0'}]});
assert(!$('binance-balances').innerHTML.includes('disabled title='));
const html=fs.readFileSync('templates/index.html','utf8');
assert(html.includes('data-tab="binance"'));assert(!html.includes('data-tab="mexc"'));assert(!html.includes('/static/mexc.js'));
assert(html.includes('<details class="card audit-details"><summary>'));
assert(html.includes('id="withdraw-binance-destination"'));
console.log('Binance UI: thresholds, balance rendering, UTC history, tab and collapsed audit OK');

// Confirmation is asynchronous: cancel/Escape must not authorize withdrawal.
(async()=>{
 const dialog=$('binance-withdraw-dialog');let closeListener;
 dialog.addEventListener=(type,fn)=>{if(type==='close')closeListener=fn;};
 dialog.showModal=()=>{};
 dialog.close=value=>{dialog.returnValue=value;closeListener();};
 let decision=context.confirmBinanceWithdrawal('amount, address, network');
 $('binance-withdraw-cancel').onclick();assert.equal(await decision,false);
 decision=context.confirmBinanceWithdrawal('new calculation');
 closeListener();assert.equal(await decision,false);
 decision=context.confirmBinanceWithdrawal('final calculation');
 $('binance-withdraw-confirm').onclick();assert.equal(await decision,true);
 assert.equal($('binance-withdraw-description').textContent,'final calculation');
 assert(html.includes('id="binance-withdraw-confirm" type="button" class="withdraw-confirm"'));
 console.log('Withdrawal confirmation: cancel, Escape and explicit approval OK');
})().catch(error=>{console.error(error);process.exitCode=1;});

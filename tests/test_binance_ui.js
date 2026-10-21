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
assert(html.indexOf('data-tab="binance"')>html.indexOf('data-tab="mexc"'));
assert(html.includes('<details class="card audit-details"><summary>'));
assert(html.includes('id="withdraw-binance-destination"'));
console.log('Binance UI: thresholds, balance rendering, UTC history, tab and collapsed audit OK');

// Run with node tests/test_wallet_ui.js; test actual functions without browser/network.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/app.js','utf8');
const elements={'wallet-search':{value:''},'wallet-filter':{value:'all'}};
const context={$,usdtEnabled:true,staleAfter:180,Date,Math,BigInt,escapeHtml:s=>String(s),date:s=>String(s)};
function $(id){return elements[id];}
vm.createContext(context);
const filters=source.slice(source.indexOf('function matchesWalletSearch'),source.indexOf("$('wallet-search').oninput"));
const fresh=source.slice(source.indexOf('function checkLine'),source.indexOf('function renderWallets'));
vm.runInContext(filters+'\n'+fresh,context);
const w={name:'User@example.com',address:'0x1234567890',balance:'1000000000000000001',usdt_balance:'0'};
elements['wallet-search'].value='EXAMPLE';assert(context.matchesWalletSearch(w));
elements['wallet-search'].value='absent';assert(!context.matchesWalletSearch(w));
elements['wallet-search'].value='';elements['wallet-filter'].value='bnb';assert(context.matchesWalletSearch(w));
elements['wallet-filter'].value='usdt';assert(!context.matchesWalletSearch(w));
elements['wallet-filter'].value='unnamed';assert(context.matchesWalletSearch({...w,name:w.address.slice(0,8)}));
elements['wallet-filter'].value='error';assert(context.matchesWalletSearch({...w,error:'timeout'}));
assert(context.checkLine('BNB',Date.now()/1000-600,'').includes('устарел'));
assert(context.checkLine('BNB',Date.now()/1000,'timeout').includes('Последний успешный баланс сохранён'));
assert(context.checkLine('USDT',0,'',false).includes('Выключен'));
console.log('Wallet search, filters and freshness: OK');
const archiveSource=fs.readFileSync('static/archive.js','utf8');
vm.runInContext(archiveSource.slice(archiveSource.indexOf('function archiveZero'),archiveSource.indexOf('function renderArchive')),context);
assert(context.archiveZero({bnb:'0',usdt:'0.000000'}));
assert(!context.archiveZero({bnb:'0.000000000000000001',usdt:'0'}));
assert(!context.archiveZero({bnb:null,usdt:'0'}));
assert(!context.archiveZero({bnb:'0',usdt:null}));
console.log('Archive zero-balance selection: OK');

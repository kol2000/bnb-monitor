const fs=require('fs'),vm=require('vm'),assert=require('assert');
const els={};function $(id){return els[id] ||= {value:'',innerHTML:'',hidden:true,addEventListener(event,fn){this[event]=fn;}};}
let calls=[],accepted=false;
const preview={id:'job',source:'0x'+'1'.repeat(40),middle:'0x'+'2'.repeat(40),destination:'0x'+'1'.repeat(40),amount:'0.1',expected:'60',minimum:'59.7',fee:'0.00001',reserve:'0.00004',retained:'0.00004',middle_reserve:'0.00002'};
const ctx={$,loggedIn:true,setInterval(){},renderWallets(){},date:String,short:String,escapeHtml:s=>String(s).replaceAll('<','&lt;'),toast(){},uiPrompt:async()=>null,uiConfirm:async()=>accepted,
 api:async(path,method,body)=>{calls.push({path,method,body});if(path==='swaps')return {items:[]};return preview;}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('static/swaps.js','utf8'),ctx);
(async()=>{
 assert.equal(ctx.walletSwapControls({account_number:2}), '');
 const w={id:1,address:preview.source,balance:'100',usdt_balance:'200'};
 let html=ctx.walletSwapControls(w);assert(html.includes('data-swap="1"'));assert(html.includes('data-usdt-transfer="1"'));
 const button={dataset:{swap:'1'},hasAttribute(){return false;}};
 await $('wallet-list').click({target:{closest:()=>button}});
 assert(!calls.some(x=>x.path.endsWith('/confirm')),'cancel must not submit transaction');
 calls=[];accepted=true;await $('wallet-list').click({target:{closest:()=>button}});
 assert.equal(calls.filter(x=>x.path==='swaps/job/confirm').length,1);
 calls=[];await $('wallet-list').click({target:{closest:()=>({dataset:{usdtTransfer:'1'},hasAttribute(){return true;}})}});
 assert.equal(calls.length,0,'missing destination / cancelled prompt must not preview or confirm');
 const page=fs.readFileSync('templates/index.html','utf8');
 for(const id of Object.keys(els))assert(page.includes(`id="${id}"`),id);
 console.log('Swaps UI: account visibility, explicit approval, cancellation, destination and DOM checks OK');
})().catch(e=>{console.error(e);process.exitCode=1;});

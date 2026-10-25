const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={};
function element(id){return elements[id] ||= {value:'',textContent:'',hidden:false,focus(){},select(){}};}
const dialog=element('app-dialog');let listener;
dialog.addEventListener=(event,fn)=>{if(event==='close')listener=fn;};
dialog.removeEventListener=()=>{listener=null;};
dialog.showModal=()=>{dialog.open=true;};
dialog.close=value=>{dialog.returnValue=value;dialog.open=false;const fn=listener;listener=null;fn();};
const ctx={document:{getElementById:element}};vm.createContext(ctx);
vm.runInContext(fs.readFileSync('static/dialogs.js','utf8'),ctx);
(async()=>{
 let p=ctx.uiConfirm('<b>Exact amount/address</b>');
 assert.equal(element('app-dialog-description').textContent,'<b>Exact amount/address</b>');
 assert.equal(await ctx.uiConfirm('second action'),false);
 element('app-dialog-cancel').onclick();assert.equal(await p,false);
 p=ctx.uiConfirm('buy');dialog.oncancel();dialog.close('');assert.equal(await p,false);
 p=ctx.uiConfirm('withdraw');element('app-dialog-confirm').onclick();assert.equal(await p,true);
 p=ctx.uiPrompt('Name','original');element('app-dialog-input').value='edited';element('app-dialog-cancel').onclick();assert.equal(await p,null);
 p=ctx.uiPrompt('Name','original');element('app-dialog-input').value='edited';element('app-dialog-input').onkeydown({key:'Enter',preventDefault(){}});assert.equal(await p,'edited');
 assert.equal(element('app-dialog-input').value,'');
 dialog.showModal=()=>{throw Error('unavailable')};
 await assert.rejects(ctx.uiConfirm('fail closed'));
 dialog.showModal=()=>{};p=ctx.uiConfirm('retry');element('app-dialog-cancel').onclick();assert.equal(await p,false);
 const html=fs.readFileSync('templates/index.html','utf8');
 const scripts=[...html.matchAll(/src="\/static\/([^"]+\.js)"/g)].map(m=>m[1]);
 for(const script of scripts){const code=fs.readFileSync('static/'+script,'utf8');assert(!/\b(?:confirm|prompt|alert)\(/.test(code),script+' has native dialog');}
 for(const id of Object.keys(elements))assert(html.includes('id="'+id+'"'),id);
 console.log('Unified dialogs: explicit approval, cancel, Escape, text input, concurrency and safe failure OK');
})().catch(e=>{console.error(e);process.exitCode=1;});

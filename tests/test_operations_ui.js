const fs=require('fs'),vm=require('vm'),assert=require('assert');
const elements={};
function $(id){return elements[id] ||= {innerHTML:'',textContent:'',hidden:false,disabled:false};}
const context={$,loggedIn:false,document:{hidden:false},setInterval(){},date:x=>x?String(x):'—',escapeHtml:s=>String(s).replaceAll('<','&lt;'),api(){throw Error('unexpected network')},toast(){}};
vm.createContext(context);vm.runInContext(fs.readFileSync('static/operations.js','utf8'),context);
context.renderOperations({connections:{rpc:{ok:false,error:'HTTP 429',checked:10,success:8}},ip:{changed:true,current:'91.135.222.58',expected:'77.221.143.151',checked:10},backup:{name:'test',created:1,size:1048576,checksum:true,verified:2},priority:[],notices:[{state:'unknown',created:1,text:'<unsafe>',error:'No retry'}]});
assert(!$('ip-warning').hidden);assert(!$('ip-acknowledge').hidden);
assert($('connection-status').innerHTML.includes('HTTP 429'));
assert($('backup-status').innerHTML.includes('SQLite OK'));
assert($('operation-notices').innerHTML.includes('&lt;unsafe>'));
context.renderOperations({connections:{},ip:{changed:false},backup:{},priority:[],notices:[]});
assert($('ip-warning').hidden);assert($('ip-acknowledge').hidden);
const html=fs.readFileSync('templates/index.html','utf8');
for(const id of Object.keys(elements))assert(html.includes(`id="${id}"`),id);
console.log('Operations UI: diagnostics, IP warning, backup and safe notice rendering OK');

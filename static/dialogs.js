'use strict';
// Styled confirmations are asynchronous. Only an explicit approval returns true.
let appDialogBusy=false;
function showAppDialog(message, options={}){
 const isPrompt=options.input===true;
 if(appDialogBusy)return Promise.resolve(isPrompt?null:false);
 const get=id=>document.getElementById(id);
 const dialog=get('app-dialog'),field=get('app-dialog-input');
 appDialogBusy=true;
 get('app-dialog-title').textContent=options.title||(isPrompt?'Название кошелька':'Подтверждение действия');
 get('app-dialog-description').textContent=String(message);
 get('app-dialog-input-label').hidden=!isPrompt;
 field.value=isPrompt?String(options.value??''):'';
 get('app-dialog-confirm').textContent=options.confirmLabel||(isPrompt?'Сохранить':'Подтвердить');
 dialog.returnValue='';
 return new Promise((resolve,reject)=>{
  const close=()=>{
   const result=dialog.returnValue==='confirm'?(isPrompt?field.value:true):(isPrompt?null:false);
   field.value='';appDialogBusy=false;resolve(result);
  };
  dialog.addEventListener('close',close,{once:true});
  get('app-dialog-cancel').onclick=()=>dialog.close('cancel');
  get('app-dialog-confirm').onclick=()=>dialog.close('confirm');
  // Escape cancels; Enter in a text field explicitly saves the edited name.
  dialog.oncancel=()=>{dialog.returnValue='';};
  field.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();dialog.close('confirm');}};
  try{dialog.showModal();if(isPrompt){field.focus();field.select();}}
  catch(e){dialog.removeEventListener('close',close);appDialogBusy=false;field.value='';reject(e);}
 });
}
function uiConfirm(message,options={}){return showAppDialog(message,options);}
function uiPrompt(message,value){return showAppDialog(message,{input:true,value});}

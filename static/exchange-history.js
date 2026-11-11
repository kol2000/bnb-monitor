'use strict';
// Measure the forms, not the growing journal: their heights stay independent.
for(const section of document.querySelectorAll('[data-usdt-venue]')){
 const forms=section.querySelector('.exchange-column');
 const panel=section.querySelector('.exchange-history-panel');
 const button=section.querySelector('.history-expand');
 const fit=()=>{const height=forms.getBoundingClientRect().height;if(height>0)panel.style.setProperty('--forms-height',Math.ceil(height)+'px');};
 new ResizeObserver(fit).observe(forms);
 button.onclick=()=>{
  const expanded=panel.classList.toggle('expanded');
  button.setAttribute('aria-expanded',String(expanded));
  button.textContent=expanded?'Свернуть историю':'Развернуть историю';
  fit();
  if(!expanded)panel.scrollIntoView({block:'nearest'});
 };
 fit();
}

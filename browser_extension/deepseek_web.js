window.wechatDeepseekDraft=async function(prompt,key){
 const root=document.documentElement;
 if(location.origin!=='https://chat.deepseek.com'||location.pathname!=='/')throw Error('deepseek_not_fresh_home');
 if(document.querySelector('.ds-assistant-message-main-content'))throw Error('deepseek_existing_conversation');
 const editor=document.querySelector('textarea[placeholder="给 DeepSeek 发送消息 "]');
 if(!editor||editor.value.trim())throw Error('deepseek_user_editing_or_not_logged_in');
 for(const label of ['深度思考','智能搜索']){
  const control=Array.from(document.querySelectorAll('.ds-toggle-button')).find(e=>e.textContent.trim()===label);
  if(!control)throw Error('deepseek_mode_control_missing');
  if(control.getAttribute('aria-pressed')==='true'){control.click();await new Promise(resolve=>setTimeout(resolve,300));}
  if(control.getAttribute('aria-pressed')!=='false')throw Error('deepseek_fast_mode_not_verified');
 }
 root.dataset.wechatDeepseekOwner=key;
 const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
 setter.call(editor,prompt);editor.dispatchEvent(new Event('input',{bubbles:true}));editor.focus();
 await new Promise(resolve=>setTimeout(resolve,400));
 if(editor.value!==prompt)throw Error('deepseek_prompt_not_inserted');
 editor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
 const deadline=Date.now()+180000;
 while(Date.now()<deadline){
  if(root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_ownership_lost');
  const answer=document.querySelector('.ds-assistant-message-main-content');
  if(answer){
   let text=(answer.innerText||answer.textContent||'').trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
   const first=text.indexOf('{'),last=text.lastIndexOf('}');if(first>=0&&last>first)text=text.slice(first,last+1);
   try{let result=JSON.parse(text);if(typeof result.reply==='string'){return JSON.stringify(result);}}catch{}
  }
  await new Promise(resolve=>setTimeout(resolve,400));
 }
 throw Error('deepseek_reply_timeout');
};
void 0;

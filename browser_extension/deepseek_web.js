window.wechatDeepseekDraft=async function(prompt,key,reuse=false,expectedUrl,turnId,contactName=''){
 const root=document.documentElement;
 const home='https://chat.deepseek.com/';
 const chatUrl=/^https:\/\/chat\.deepseek\.com\/a\/chat\/s\/[^/?#]+$/;
 const marker='[wechat-turn:'+turnId+']';
 const blocks=()=>Array.from(document.querySelector('.ds-virtual-list-visible-items')?.children||[]);
 const users=()=>blocks().filter(node=>!node.querySelector('.ds-assistant-message-main-content')&&(node.textContent||'').trim());
 let activeUrl=expectedUrl,persistedUrl='';
 const assertUrl=()=>{
  const current=location.href;
  if(current===activeUrl)return;
  if(activeUrl===home&&chatUrl.test(current)){activeUrl=current;return;}
  throw Error('deepseek_conversation_changed');
 };
 try{
  if(location.href!==expectedUrl||!(expectedUrl===home||chatUrl.test(expectedUrl)))throw Error('deepseek_wrong_conversation');
  delete root.dataset.wechatDeepseekFailed;
  delete root.dataset.wechatDeepseekFailedOwner;
  if((reuse&&!chatUrl.test(activeUrl))||(!reuse&&activeUrl!==home))throw Error('deepseek_not_fresh_home');
  if(!reuse&&(document.querySelector('.ds-assistant-message-main-content')||users().length))throw Error('deepseek_existing_conversation');
  if(root.dataset.wechatDeepseekOwner&&root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_wrong_contact');
  const editor=document.querySelector('textarea[placeholder="给 DeepSeek 发送消息 "]');
  if(!editor||editor.value.trim())throw Error('deepseek_user_editing_or_not_logged_in');
  for(const label of ['深度思考','智能搜索']){
   assertUrl();
   const control=Array.from(document.querySelectorAll('.ds-toggle-button')).find(e=>e.textContent.trim()===label);
   if(!control)throw Error('deepseek_mode_control_missing');
   if(control.getAttribute('aria-pressed')==='true'){control.click();await new Promise(resolve=>setTimeout(resolve,300));}
   if(control.getAttribute('aria-pressed')!=='false')throw Error('deepseek_fast_mode_not_verified');
  }
  const oldAnswers=new Set(document.querySelectorAll('.ds-assistant-message-main-content'));
  if(users().some(node=>node.textContent.includes(marker)))throw Error('deepseek_turn_already_present');
  root.dataset.wechatDeepseekOwner=key;
  const submitted=marker+'\n此标记只用于核对本轮网页消息，回复中不要复述。\n\n'+prompt;
  const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
  setter.call(editor,submitted);editor.dispatchEvent(new Event('input',{bubbles:true}));editor.focus();
  await new Promise(resolve=>setTimeout(resolve,400));
  assertUrl();
  if(editor.value!==submitted||root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_prompt_not_inserted');
  editor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
  const deadline=Date.now()+45000;
  let settledText='',settledSince=0;
  while(Date.now()<deadline){
   assertUrl();
   if(root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_ownership_lost');
   const currentBlocks=blocks(),currentUsers=currentBlocks.filter(node=>!node.querySelector('.ds-assistant-message-main-content')&&(node.textContent||'').trim());
   const user=currentUsers.find(node=>node.textContent.includes(marker));
   if(user){
    if(currentUsers.some(node=>node!==user&&(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)))throw Error('deepseek_newer_user_turn');
    if(chatUrl.test(activeUrl)&&persistedUrl!==activeUrl){
     const saved=await chrome.storage.local.get('deepseekConversations'),conversations=saved.deepseekConversations||{};
     if(reuse&&conversations[key]?.url&&conversations[key].url!==activeUrl)throw Error('deepseek_conversation_changed');
     conversations[key]={url:activeUrl,name:contactName||conversations[key]?.name||'',used:Date.now(),lastTurnId:turnId};
     await chrome.storage.local.set({deepseekConversations:conversations,deepseekOwnedUrl:activeUrl});
     persistedUrl=activeUrl;assertUrl();
    }
    const responseBlock=currentBlocks.find(node=>(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)&&node.querySelector('.ds-assistant-message-main-content')&&!oldAnswers.has(node.querySelector('.ds-assistant-message-main-content')));
    const answer=responseBlock?.querySelector('.ds-assistant-message-main-content');
    if(answer&&chatUrl.test(activeUrl)){
     let replyText=(answer.innerText||answer.textContent||'').trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
     const first=replyText.indexOf('{'),last=replyText.lastIndexOf('}');if(first>=0&&last>first)replyText=replyText.slice(first,last+1);
     try{
      const result=JSON.parse(replyText);
      if(typeof result.reply==='string'){
       if(replyText===settledText&&Date.now()-settledSince>=600){assertUrl();return {reply:JSON.stringify(result),url:activeUrl,reused:reuse,reset:false};}
       if(replyText!==settledText){settledText=replyText;settledSince=Date.now();}
      }
     }catch{}
    }
   }
   await new Promise(resolve=>setTimeout(resolve,400));
  }
  throw Error('deepseek_reply_timeout');
 }catch(error){root.dataset.wechatDeepseekFailed='true';root.dataset.wechatDeepseekFailedOwner=key;throw error;}
};
void 0;

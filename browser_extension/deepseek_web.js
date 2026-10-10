window.wechatDeepseekDraft=async function(prompt,key,reuse=false,expectedUrl,turnId,contactName=''){
 const root=document.documentElement;
 const home='https://chat.deepseek.com/';
 const chatUrl=/^https:\/\/chat\.deepseek\.com\/a\/chat\/s\/[^/?#]+$/;
 const editorSelector='textarea[placeholder="给 DeepSeek 发送消息 "]';
 const marker='[wechat-turn:'+turnId+']';
 const blocks=()=>Array.from(document.querySelector('.ds-virtual-list-visible-items')?.children||[]);
 const users=()=>blocks().filter(node=>!node.querySelector('.ds-assistant-message-main-content')&&(node.textContent||'').trim());
 const historySignature=()=>JSON.stringify([blocks().map(node=>node.textContent||''),Array.from(document.querySelectorAll('.ds-assistant-message-main-content')).map(node=>node.textContent||'')]);
 const sendControl=editor=>{
  const scope=editor?.parentElement?.parentElement;
  if(!scope||scope.querySelector(editorSelector)!==editor)return null;
  // This primary/circle control and arrow path were verified in the live
  // composer. Attachment and unrelated page controls cannot match this shape.
  const candidates=Array.from(scope.querySelectorAll('[role="button"].ds-button--primary.ds-button--filled.ds-button--circle'));
  if(!candidates.length)return null;
  // A changed icon may be Stop, and duplicate candidates are ambiguous. Neither
  // case authorizes Enter as a way around the composer's own readiness state.
  if(candidates.length!==1)return {button:null,disabled:true};
  const button=candidates[0],arrow=Array.from(button.querySelectorAll('svg[width="16"][height="16"] path')).some(path=>(path.getAttribute('d')||'').includes('L9 3.95579V15.0417H7V3.95579'));
  const disabled=!arrow||button.isConnected===false||button.disabled===true||button.getAttribute('aria-disabled')==='true'||/(?:^|\s)ds-button--disabled(?:\s|$)/.test(button.getAttribute('class')||'');
  return {button,disabled};
 };
 let activeUrl=expectedUrl,persistedUrl='',stage='preflight',mayNavigateToChat=false,dispatched=false,seenTurn=false,observeReceipt=null;
 const markStage=value=>{stage=value;root.dataset.wechatDeepseekStage=value;};
 const assertUrl=()=>{
  const current=location.href;
  if(current===activeUrl)return;
  if(mayNavigateToChat&&activeUrl===home&&chatUrl.test(current)){activeUrl=current;return;}
  throw Error('deepseek_conversation_changed');
 };
 try{
  markStage('preflight');
  // Re-injecting this script must not turn an uncertain receipt into a retry.
  // Keep the last receipt and any manual composer draft for this exact turn.
  if(/^[a-zA-Z0-9_-]{1,128}$/.test(String(turnId||''))&&root.dataset.wechatDeepseekSubmitTurnId===String(turnId)&&root.dataset.wechatDeepseekSubmitAttempts==='1')throw Error(['marker_seen','reply_received'].includes(root.dataset.wechatDeepseekSubmitStatus)?'deepseek_turn_already_present':'deepseek_turn_not_visible');
  root.dataset.wechatDeepseekSubmitAttempts='0';root.dataset.wechatDeepseekSubmitStatus='not_sent';
  root.dataset.wechatDeepseekSubmitTurnId=/^[a-zA-Z0-9_-]{1,128}$/.test(String(turnId||''))?String(turnId):'unknown';
  root.dataset.wechatDeepseekSubmitEvidence='none';
  delete root.dataset.wechatDeepseekSubmitMethod;
  if(location.href!==expectedUrl||!(expectedUrl===home||chatUrl.test(expectedUrl)))throw Error('deepseek_wrong_conversation');
  delete root.dataset.wechatDeepseekFailed;
  delete root.dataset.wechatDeepseekFailedOwner;
  delete root.dataset.wechatDeepseekFailure;
  delete root.dataset.wechatDeepseekFailureStage;
  if((reuse&&!chatUrl.test(activeUrl))||(!reuse&&activeUrl!==home))throw Error('deepseek_not_fresh_home');
  if(!reuse&&(document.querySelector('.ds-assistant-message-main-content')||users().length))throw Error('deepseek_existing_conversation');
  if(root.dataset.wechatDeepseekOwner&&root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_wrong_contact');
  let editor=document.querySelector(editorSelector);
  if(!editor||editor.value!=='')throw Error('deepseek_user_editing_or_not_logged_in');
  let sawControl=!!sendControl(editor);
  const initialUsers=JSON.stringify(users().map(node=>node.textContent||''));
  const assertNoNewUser=()=>{
   const visible=users();
   if(visible.some(node=>node.textContent.includes(marker)))throw Error('deepseek_turn_already_present');
   if(JSON.stringify(visible.map(node=>node.textContent||''))!==initialUsers)throw Error('deepseek_newer_user_turn');
  };
  markStage('mode');for(const label of ['深度思考','智能搜索']){
   assertUrl();
   const control=Array.from(document.querySelectorAll('.ds-toggle-button')).find(e=>e.textContent.trim()===label);
   if(!control)throw Error('deepseek_mode_control_missing');
   if(control.getAttribute('aria-pressed')==='true'){control.click();await new Promise(resolve=>setTimeout(resolve,300));}
   if(control.getAttribute('aria-pressed')!=='false')throw Error('deepseek_fast_mode_not_verified');
  }
  markStage('prompt');const oldAnswers=new Set(document.querySelectorAll('.ds-assistant-message-main-content'));
  assertUrl();assertNoNewUser();
  if(root.dataset.wechatDeepseekOwner&&root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_wrong_contact');
  editor=document.querySelector(editorSelector);
  if(!editor||editor.isConnected===false||editor.value!=='')throw Error('deepseek_user_editing_or_not_logged_in');
  sawControl=!!sendControl(editor)||sawControl;
  if(!reuse&&(document.querySelector('.ds-assistant-message-main-content')||users().length))throw Error('deepseek_existing_conversation');
  root.dataset.wechatDeepseekOwner=key;
  const submitted=marker+'\n此标记只用于核对本轮网页消息，回复中不要复述。\n\n'+prompt;
  const setter=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
  setter.call(editor,submitted);editor.dispatchEvent(new Event('input',{bubbles:true}));editor.focus();
  await new Promise(resolve=>setTimeout(resolve,400));
  assertUrl();assertNoNewUser();
  // React may replace the textarea after input. A detached node can keep the
  // correct value while its keyboard event never reaches the current composer.
  editor=document.querySelector(editorSelector);
  if(!editor||editor.isConnected===false||editor.value!==submitted||root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_prompt_not_inserted');
  editor.focus();
  assertUrl();assertNoNewUser();
  if(document.querySelector(editorSelector)!==editor||editor.isConnected===false||editor.value!==submitted||root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_prompt_not_inserted');
  const submitUrl=location.href,submitHistory=historySignature();
  const evidence=new Set();
  observeReceipt=()=>{
   const currentEditor=document.querySelector(editorSelector);
   if(!currentEditor||currentEditor.isConnected===false)evidence.add('editor_missing');
   else if(currentEditor.value==='')evidence.add('editor_cleared');
   else if(currentEditor.value!==submitted)evidence.add('editor_changed');
   if(location.href!==submitUrl)evidence.add('url_changed');
   if(historySignature()!==submitHistory)evidence.add('history_changed');
   if(sendControl(currentEditor)?.disabled)evidence.add('composer_busy');
   root.dataset.wechatDeepseekSubmitEvidence=Array.from(evidence).sort().join(',')||'none';
   if(evidence.size&&root.dataset.wechatDeepseekSubmitStatus==='pending')root.dataset.wechatDeepseekSubmitStatus='ambiguous';
  };
  markStage('send');
  let control=sendControl(editor);sawControl=!!control||sawControl;const readyDeadline=Date.now()+2000;
  while((control?.disabled||sawControl&&!control)&&Date.now()<readyDeadline){
   await new Promise(resolve=>setTimeout(resolve,200));assertUrl();assertNoNewUser();
   editor=document.querySelector(editorSelector);
   if(!editor||editor.isConnected===false||editor.value!==submitted||root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_prompt_not_inserted');
   if(historySignature()!==submitHistory)throw Error('deepseek_prompt_not_inserted');
   control=sendControl(editor);
   sawControl=!!control||sawControl;
  }
  if(control?.disabled||sawControl&&!control){root.dataset.wechatDeepseekSubmitStatus='not_ready';throw Error('deepseek_prompt_not_inserted');}
  // Choose a single route before dispatch; no second dispatch on missing receipt.
  root.dataset.wechatDeepseekSubmitAttempts='1';root.dataset.wechatDeepseekSubmitStatus='pending';root.dataset.wechatDeepseekSubmitMethod=control?'button':'enter';mayNavigateToChat=true;dispatched=true;
  if(control)control.button.click();
  else editor.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true,cancelable:true}));
  const deadline=Date.now()+45000;
  let settledText='',settledSince=0;markStage('reply_marker');
  while(Date.now()<deadline){
   // These fixed tokens are hints, never ownership or answer proof. Latch them
   // before navigation checks, even if the composer later restores its draft.
   observeReceipt();
   assertUrl();
   if(root.dataset.wechatDeepseekOwner!==key)throw Error('deepseek_ownership_lost');
   const currentBlocks=blocks(),currentUsers=currentBlocks.filter(node=>!node.querySelector('.ds-assistant-message-main-content')&&(node.textContent||'').trim());
   const user=currentUsers.find(node=>node.textContent.includes(marker));
   // A virtual list can hide a submitted turn. Missing marker alone must never
   // trigger another send or make an unrelated answer eligible.
   if(user){
    seenTurn=true;root.dataset.wechatDeepseekSubmitStatus='marker_seen';markStage('reply');
    if(currentUsers.some(node=>node!==user&&(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)))throw Error('deepseek_newer_user_turn');
    if(chatUrl.test(activeUrl)&&persistedUrl!==activeUrl){
     markStage('persist');
     const saved=await chrome.storage.local.get('deepseekConversations'),conversations=saved.deepseekConversations||{};
     if(reuse&&conversations[key]?.url&&conversations[key].url!==activeUrl)throw Error('deepseek_conversation_changed');
     conversations[key]={url:activeUrl,name:contactName||conversations[key]?.name||'',used:Date.now(),lastTurnId:turnId};
     await chrome.storage.local.set({deepseekConversations:conversations,deepseekOwnedUrl:activeUrl});
     persistedUrl=activeUrl;assertUrl();markStage('reply');
    }
    const responseBlock=currentBlocks.find(node=>(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)&&node.querySelector('.ds-assistant-message-main-content')&&!oldAnswers.has(node.querySelector('.ds-assistant-message-main-content')));
    const answer=responseBlock?.querySelector('.ds-assistant-message-main-content');
    if(answer&&chatUrl.test(activeUrl)){
     let replyText=(answer.innerText||answer.textContent||'').trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
     const first=replyText.indexOf('{'),last=replyText.lastIndexOf('}');if(first>=0&&last>first)replyText=replyText.slice(first,last+1);
     try{
      const result=JSON.parse(replyText);
      if(typeof result.reply==='string'){
       if(replyText===settledText&&Date.now()-settledSince>=600){assertUrl();root.dataset.wechatDeepseekSubmitStatus='reply_received';return {reply:JSON.stringify(result),url:activeUrl,reused:reuse,reset:false};}
       if(replyText!==settledText){settledText=replyText;settledSince=Date.now();}
      }
     }catch{}
    }
   }
   await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(!seenTurn)root.dataset.wechatDeepseekSubmitStatus='unconfirmed';
  throw Error(seenTurn?'deepseek_reply_timeout':'deepseek_turn_not_visible');
 }catch(error){
  if(dispatched){try{observeReceipt();}catch{}if(!seenTurn)root.dataset.wechatDeepseekSubmitStatus='unconfirmed';}
  const allowed=new Set(['deepseek_conversation_changed','deepseek_wrong_conversation','deepseek_not_fresh_home','deepseek_existing_conversation','deepseek_wrong_contact','deepseek_user_editing_or_not_logged_in','deepseek_mode_control_missing','deepseek_fast_mode_not_verified','deepseek_turn_already_present','deepseek_prompt_not_inserted','deepseek_ownership_lost','deepseek_newer_user_turn','deepseek_turn_not_visible','deepseek_reply_timeout']);
  const code=allowed.has(error?.message)?error.message:'deepseek_unexpected_error';
  root.dataset.wechatDeepseekFailed='true';root.dataset.wechatDeepseekFailedOwner=key;root.dataset.wechatDeepseekFailure=code;root.dataset.wechatDeepseekFailureStage=stage;
  const failure=Error(code);failure.deepseekCode=code;failure.deepseekStage=stage;throw failure;
 }
};
void 0;

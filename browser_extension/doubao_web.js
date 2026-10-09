// The observer wakes on visible DOM changes even when Chrome throttles hidden-tab timers.
window.wechatDoubaoWait= function(check,timeoutMs,errorCode){
 return new Promise((resolve,reject)=>{
  let done=false,timer,observer;
  const onImageEvent=event=>{if(event.target?.tagName==='IMG')evaluate();};
  const finish=(error,value)=>{
   if(done)return;done=true;observer?.disconnect();clearTimeout(timer);
   document.removeEventListener?.('load',onImageEvent,true);
   document.removeEventListener?.('error',onImageEvent,true);
   if(error)reject(error);else resolve(value);
  };
  const evaluate=()=>{if(done)return;try{const value=check();if(value)finish(null,value);}catch(error){finish(error);}};
  observer=new MutationObserver(evaluate);
  observer.observe(document.documentElement,{
   subtree:true,childList:true,characterData:true,
   attributes:true,
   attributeFilter:['data-streaming','data-message-id','aria-disabled','disabled','data-state','aria-selected','data-conversation-id','data-wechat-doubao-owner','src','class']
  });
  document.addEventListener?.('load',onImageEvent,true);
  document.addEventListener?.('error',onImageEvent,true);
  timer=setTimeout(()=>finish(Error(errorCode)),timeoutMs);
  evaluate();
 });
};

window.wechatDoubaoReply=async function(prompt,key,reused,images,expectedUrl,turnId,contactName=''){
 let pendingPersist=Promise.resolve();
 let diagnosticPhase='validate',diagnosticFailure='',diagnosticComplete=false,captureDiagnostic=()=>({});
 window.wechatDoubaoActiveTurn=turnId;
 try{
 const root=document.documentElement,previousOwner=root.dataset.wechatDoubaoOwner;
 const home='https://www.doubao.com/chat/',chatUrl=/^https:\/\/www\.doubao\.com\/chat\/\d+$/,localUrl=/^https:\/\/www\.doubao\.com\/chat\/local_\d+$/;
 const isHomeUrl=url=>url===home||url==='https://www.doubao.com/chat';
 const marker='[wechat-turn:'+turnId+']';let activeUrl=expectedUrl,submittedTurn=false,visualReady=false,finalUrlLocked=false;
 let persistedUrl='';
 const messageId=node=>node?.querySelector?.('[data-message-id]')?.getAttribute?.('data-message-id')||node?.getAttribute?.('data-message-id')||'';
 const persistTurn=()=>{
  if(!(chatUrl.test(activeUrl)||localUrl.test(activeUrl))||persistedUrl===activeUrl)return;
  const target=activeUrl;persistedUrl=target;
  pendingPersist=pendingPersist.then(async()=>{
   const saved=await chrome.storage.local.get('doubaoConversations'),conversations=saved.doubaoConversations||{};
   if(reused&&conversations[key]?.url&&conversations[key].url!==target&&!(localUrl.test(conversations[key].url)&&chatUrl.test(target)))throw Error('doubao_conversation_changed');
   conversations[key]={url:target,name:contactName||conversations[key]?.name||'',lastTurnId:turnId};
   await chrome.storage.local.set({doubaoConversations:conversations,doubaoOwnedUrl:target});
  });
 };
 const visibleUsers=()=>Array.from(document.querySelectorAll('[data-testid="send_message"]'));
 const imageMessage=node=>!!node.querySelector('img')&&!node.textContent.includes('[wechat-turn:');
 // Diagnostics contain only structural attributes, IDs and character counts.
 // Never copy text, input contents, labels, HTML, URLs or uploaded image data.
 const safeAttrs=node=>{
  if(!node)return null;
  const values={tag:String(node.tagName||'').toLowerCase()};
  for(const attr of ['data-testid','data-message-role','role']){
   const value=node.getAttribute?.(attr);
   if(value&&/^[a-zA-Z0-9_-]{1,80}$/.test(value))values[attr]=value;
  }
  for(const attr of ['data-message-id','data-local-message-id','data-msg-id']){
   const value=node.getAttribute?.(attr);
   if(value!==null&&value!==undefined)values[attr]=/^\d{1,32}$|^[0-9a-fA-F-]{8,80}$/.test(value)?value:'present';
  }
  for(const attr of ['data-streaming','aria-busy','aria-disabled']){
   const value=node.getAttribute?.(attr);
   if(value!==null&&value!==undefined)values[attr]=['true','false'].includes(value)?value:'present';
  }
  const state=node.getAttribute?.('data-state');
  if(state!==null&&state!==undefined)values['data-state']=['loading','streaming','finished','done','complete','idle','open','closed','active','inactive','busy','error','success','pending'].includes(state)?state:'present';
  return values;
 };
 const count=(node,selector)=>Array.from(node?.querySelectorAll?.(selector)||[]).length;
 const follows=(first,second)=>!!((first?.compareDocumentPosition?.(second)||0)&4);
 const idPaths=(node,user)=>{
  const child=node?.querySelector?.('[data-message-id]'),ancestor=node?.parentElement?.closest?.('[data-message-id]');
  const union=node?.closest?.('[data-testid="union_message"]');
  const userAncestor=user?.parentElement?.closest?.('[data-message-id]');
  return {selected:child?.getAttribute?.('data-message-id')?'descendant':node?.getAttribute?.('data-message-id')?'self':'missing',
   self:safeAttrs(node),descendant:safeAttrs(child),ancestor:safeAttrs(ancestor),union:safeAttrs(union),
   ancestorSharedWithUser:!!ancestor&&ancestor===userAncestor,
   ancestorUserCount:count(ancestor,'[data-testid="send_message"]'),ancestorAnswerCount:count(ancestor,'[data-testid="receive_message"]')};
 };
 captureDiagnostic=()=>{
  const users=visibleUsers(),user=users.find(node=>node.textContent.includes(marker));
  const answers=Array.from(document.querySelectorAll('[data-testid="receive_message"]'));
  const after=user?answers.filter(node=>follows(user,node)):[];
  const current=location.href,urlKind=isHomeUrl(current)?'home':localUrl.test(current)?'local':chatUrl.test(current)?'chat':'other';
  return {urlKind,sameOwnedUrl:current===activeUrl,userFound:!!user,user:idPaths(user,user),
   selectors:{send:users.length,receive:answers.length,receiveVariant:count(document,'[data-testid*="receive_message"]'),assistantRole:count(document,'[data-message-role="assistant"],[data-role="assistant"]'),union:count(document,'[data-testid="union_message"]'),messageBlock:count(document,'[data-testid="message-block-container"]'),messageId:count(document,'[data-message-id]'),streaming:count(document,'[data-streaming]')},
   afterCount:after.length,after:after.slice(-3).map(node=>{
    const body=node.querySelector?.('[data-testid="message_text_content"]'),ancestors=[];
    for(let parent=node.parentElement;parent&&ancestors.length<4;parent=parent.parentElement)ancestors.push(safeAttrs(parent));
    return {id:idPaths(node,user),ancestors,body:safeAttrs(body),bodyCount:count(node,'[data-testid="message_text_content"]'),textLength:String(node.textContent||'').length,bodyTextLength:String(body?.innerText||body?.textContent||'').length,
     streamingNodes:Array.from(node.querySelectorAll?.('[data-streaming],[aria-busy]')||[]).slice(0,4).map(safeAttrs),
     alternateIds:Array.from(node.querySelectorAll?.('[data-local-message-id],[data-msg-id]')||[]).slice(0,3).map(safeAttrs)};
   }),sendButton:safeAttrs(document.querySelector('[data-testid="chat_input_send_button"]'))};
 };
 const assertUrl=(phase='setup')=>{
  const current=location.href;
  if(current===activeUrl)return true;
  if(!reused&&isHomeUrl(activeUrl)&&(isHomeUrl(current)||chatUrl.test(current)||localUrl.test(current))){activeUrl=current;return true;}
  if(phase==='reply'&&submittedTurn&&!finalUrlLocked&&chatUrl.test(current)&&
     ((!reused&&localUrl.test(activeUrl))||(reused&&localUrl.test(activeUrl))||(!reused&&visualReady&&chatUrl.test(activeUrl)))){
   const users=visibleUsers(),mine=users.find(node=>node.textContent.includes(marker));
   if(!mine)return false;
   const before=users.filter(node=>node!==mine&&(node.compareDocumentPosition(mine)&Node.DOCUMENT_POSITION_FOLLOWING));
   const after=users.filter(node=>node!==mine&&(mine.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING));
   if(after.length||(!reused&&(before.length>images.length||before.some(node=>!imageMessage(node)||!messageId(node))||
      Array.from(document.querySelectorAll('[data-testid="receive_message"]')).some(node=>node.compareDocumentPosition(mine)&Node.DOCUMENT_POSITION_FOLLOWING))))throw Error('doubao_unowned_conversation');
   activeUrl=current;finalUrlLocked=true;return true;
  }
  throw Error('doubao_conversation_changed');
 };
 if((location.href!==expectedUrl&&!(!reused&&isHomeUrl(location.href)&&isHomeUrl(expectedUrl)))||!(isHomeUrl(expectedUrl)||chatUrl.test(expectedUrl)||localUrl.test(expectedUrl))||(reused&&isHomeUrl(expectedUrl)))throw Error('doubao_wrong_conversation');
 if(!reused&&isHomeUrl(expectedUrl))activeUrl=location.href;
 if(previousOwner&&previousOwner!==key)throw Error('doubao_wrong_contact');
 const editor=document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"]');
 if(!editor||editor.textContent.trim())throw Error('doubao_user_editing');
 if(!reused&&(document.querySelector('[data-testid="receive_message"]')||document.querySelector('[data-testid="send_message"]')))throw Error('doubao_unowned_conversation');
 const oldAnswerIds=new Set(Array.from(document.querySelectorAll('[data-testid="receive_message"]')).map(messageId).filter(Boolean));
 if(visibleUsers().some(node=>node.textContent.includes(marker)))throw Error('doubao_turn_already_present');
 const existing=Array.from(document.querySelectorAll('[data-testid="attachment-image-card"]'));
 if(existing.length){
  diagnosticPhase='remove_attachments';
  if(previousOwner!==key||existing.some(card=>!images.some(image=>card.getAttribute('aria-label')===image.name)))throw Error('doubao_user_attachments');
  for(const card of existing){card.querySelector('[data-testid="attachment-delete-btn"]')?.dispatchEvent(new MouseEvent('click',{bubbles:true}));}
  await window.wechatDoubaoWait(()=>!document.querySelector('[data-testid="chat_input"] [data-testid="attachment-image-card"]'),3000,'doubao_user_attachments');
 }
 root.dataset.wechatDoubaoOwner=key;
 if(images.length){
  diagnosticPhase='upload';
  assertUrl();
  const input=document.querySelector('input[data-testid="upload-file-input"]')||document.querySelector('input[type="file"]');if(!input)throw Error('doubao_upload_not_available');
  const transfer=new DataTransfer();
  for(const image of images){const binary=atob(image.data),bytes=Uint8Array.from(binary,c=>c.charCodeAt(0));transfer.items.add(new File([bytes],image.name,{type:image.mime}));}
  input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
  // Wait for the visible upload to finish; a failed upload must never become an invented visual answer.
  await window.wechatDoubaoWait(()=>{
   assertUrl();if(root.dataset.wechatDoubaoOwner!==key)throw Error('doubao_ownership_lost');
   const area=document.querySelector('[data-testid="chat_input"]');
   const previews=Array.from(area?.querySelectorAll('[data-testid="attachment-image-card"]')||[]).filter(card=>images.some(image=>card.getAttribute('aria-label')===image.name)&&card.querySelector('img')?.naturalWidth>0);const uploading=area?.querySelector('[role="progressbar"],.animate-spin');
   return previews.length>=images.length&&!uploading;
  },30000,'doubao_image_upload_not_verified');
  visualReady=true;
 }
 if(!reused){
  const before=visibleUsers(),priorReplies=document.querySelectorAll('[data-testid="receive_message"]');
  if(priorReplies.length||before.length>images.length||before.some(node=>!imageMessage(node)||!messageId(node)))throw Error('doubao_unowned_conversation');
 }
 const submitted=marker+'\n此标记只用于核对本轮网页消息，回复中不要复述。\n\n'+prompt;
 diagnosticPhase='send_ready';
 editor.focus();document.execCommand('insertText',false,submitted);
 editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:submitted}));
 const send=await window.wechatDoubaoWait(()=>{
  assertUrl();if(root.dataset.wechatDoubaoOwner!==key)throw Error('doubao_ownership_lost');
  if(editor.textContent.replace(/\s/g,'')!==submitted.replace(/\s/g,''))return null;
  const button=document.querySelector('[data-testid="chat_input_send_button"]');
  return button&&!button.disabled&&button.getAttribute('aria-disabled')!=='true'?button:null;
 },3000,'doubao_prompt_or_send_not_ready');
 submittedTurn=true;send.click();
 diagnosticPhase='wait_user';
 const replyDeadline=Date.now()+75000;
 await window.wechatDoubaoWait(()=>{
  if(!assertUrl('reply'))return null;
  if(root.dataset.wechatDoubaoOwner!==key)throw Error('doubao_ownership_lost');
  const users=visibleUsers(),user=users.find(node=>node.textContent.includes(marker));
  if(!user)return null;
  if(users.some(node=>node!==user&&(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)))throw Error('doubao_newer_user_turn');
  if(!reused){
   const before=users.filter(node=>node!==user&&(node.compareDocumentPosition(user)&Node.DOCUMENT_POSITION_FOLLOWING));
   if(before.length>images.length||before.some(node=>!imageMessage(node)||!messageId(node))||
      Array.from(document.querySelectorAll('[data-testid="receive_message"]')).some(node=>node.compareDocumentPosition(user)&Node.DOCUMENT_POSITION_FOLLOWING))throw Error('doubao_unowned_conversation');
  }
  return chatUrl.test(activeUrl)||localUrl.test(activeUrl)?user:null;
 },Math.max(1,replyDeadline-Date.now()),'doubao_reply_timeout');
 diagnosticPhase='persist_user';
 persistTurn();await pendingPersist;
 diagnosticPhase='wait_answer';
 const decision=await window.wechatDoubaoWait(()=>{
  if(!assertUrl('reply'))return null;
  if(root.dataset.wechatDoubaoOwner!==key)throw Error('doubao_ownership_lost');
  const users=visibleUsers();
  const user=users.find(node=>node.textContent.includes(marker));
  if(user&&users.some(node=>node!==user&&(user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING)))throw Error('doubao_newer_user_turn');
  if(user)persistTurn();
  const userId=messageId(user);
  const after=user?Array.from(document.querySelectorAll('[data-testid="receive_message"]')).filter(node=>user.compareDocumentPosition(node)&Node.DOCUMENT_POSITION_FOLLOWING):[];
  const answer=after.find(node=>{const id=messageId(node);return id&&id!==userId&&!oldAnswerIds.has(id);});
  const diagnostic=JSON.stringify({user:!!user,userId,after:after.length,afterIds:after.map(messageId).filter(Boolean).slice(-3),candidate:!!answer,knownIds:oldAnswerIds.size,urlOk:chatUrl.test(activeUrl)});
  if(root.dataset.wechatDoubaoGuard!==diagnostic)root.dataset.wechatDoubaoGuard=diagnostic;
  if(answer&&chatUrl.test(activeUrl)){
   const body=answer.querySelector('[data-testid="message_text_content"]');
   if(body&&body.getAttribute('data-streaming')==='false'){
    let text=body.innerText.trim();const first=text.indexOf('{'),last=text.lastIndexOf('}');if(first>=0&&last>first)text=text.slice(first,last+1);
    let decision;try{decision=JSON.parse(text);}catch{}
    if(typeof decision?.reply==='string')return decision;
   }
  }
  return null;
 },Math.max(1,replyDeadline-Date.now()),'doubao_reply_timeout');
 diagnosticPhase='persist_answer';
 await pendingPersist;
 const payloadMarker='以下是本轮微信数据：';let name='';
 try{const raw=prompt.split(payloadMarker)[1].split('只输出符合')[0].trim();name=JSON.parse(raw).contact_profile?.name||'';}catch{}
 diagnosticPhase='rename';
 if(name)await window.wechatDoubaoRename(name);else root.dataset.wechatDoubaoRenameStage='no_payload_name';
 if(!assertUrl('reply')||root.dataset.wechatDoubaoOwner!==key)throw Error('doubao_ownership_lost');
 diagnosticPhase='complete';diagnosticComplete=true;
 return {reply:JSON.stringify(decision),url:activeUrl};
 }catch(error){diagnosticFailure=String(error.message).match(/doubao_[a-z_]+/)?.[0]||'doubao_unexpected_error';await pendingPersist.catch(()=>{});document.documentElement.dataset.wechatDoubaoFailure=String(error.message).match(/doubao_[a-z_]+/)?.[0]||error.name;throw error;}
 finally{
  if(window.wechatDoubaoActiveTurn===turnId)delete window.wechatDoubaoActiveTurn;
  try{
   let structure;try{structure=captureDiagnostic();}catch{structure={captureFailed:true};}
   const diagnostic={schema:1,at:Date.now(),phase:diagnosticPhase,complete:diagnosticComplete,failure:diagnosticFailure,
    turn_id:/^[a-zA-Z0-9_-]{1,128}$/.test(turnId||'')?turnId:'invalid',...structure};
   document.documentElement.dataset.wechatDoubaoDiagnostic=JSON.stringify(diagnostic);
   if(diagnosticFailure&&typeof chrome==='object'&&chrome.storage?.local?.set)await chrome.storage.local.set({doubaoLastDomFailure:diagnostic});
  }catch{}
 }
};

window.wechatDoubaoRename=async function(name){
 const root=document.documentElement;const stage=value=>root.dataset.wechatDoubaoRenameStage=value;
 const wait=async query=>window.wechatDoubaoWait(query,2500,'doubao_rename_timeout').catch(()=>null);
 stage('entered');const id=location.pathname.split('/').filter(Boolean).pop();if(!/^\d+$/.test(id)){stage('no_id');return false;}
 const selector='[data-testid="conversation-list-v2-item"][data-conversation-id="'+id+'"]';
 const row=await wait(()=>document.querySelector(selector));if(!row){stage('no_row');return false;}
 name=name.slice(0,20);if(row.textContent.includes(name)){stage('already_named');return true;}
 const button=row.querySelector('[data-testid="chat_list_item_more_button"]');if(!button){stage('no_button');return false;}button.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true,button:0,pointerId:1,pointerType:'mouse',isPrimary:true}));button.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,button:0,pointerId:1,pointerType:'mouse',isPrimary:true}));
 const rename=await wait(()=>Array.from(document.querySelectorAll('[role="menuitem"]')).find(e=>e.textContent.includes('重命名')));if(!rename){stage('no_menu');return false;}rename.click();
 const input=await wait(()=>document.querySelector('[data-testid="edit_conversation_dialog_name_input"]'));if(!input){stage('no_input');return false;}
 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,name);
 input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertReplacementText',data:name}));input.dispatchEvent(new Event('change',{bubbles:true}));await Promise.resolve();
 const dialog=input.closest('[role="dialog"],.semi-modal')||document;const save=Array.from(dialog.querySelectorAll('button')).find(e=>e.textContent.trim()==='确定');
 if(!save){stage('no_save');return false;}save.click();
 const updated=await wait(()=>document.querySelector(selector)?.textContent.includes(name));stage(updated?'saved':'not_reflected');return !!updated;
};

// One owned Doubao page; no access to pre-existing personal conversations.
let doubaoBusy=false,doubaoSocket=null;
function doubaoErrorCode(error){
 const allowed=new Set(['doubao_recovery_unproven','doubao_recovery_tab_unavailable','doubao_seed_tab_unavailable','doubao_owned_tab_unavailable','doubao_owned_tab_unproven','doubao_user_editing','doubao_known_conversation_missing','doubao_pending_unverified','doubao_login_or_load','doubao_empty_or_wrong_conversation','doubao_result_url_changed','doubao_result_rejected','doubao_conversation_changed','doubao_unowned_conversation','doubao_wrong_conversation','doubao_wrong_contact','doubao_turn_already_present','doubao_user_attachments','doubao_upload_not_available','doubao_ownership_lost','doubao_image_upload_not_verified','doubao_prompt_or_send_not_ready','doubao_newer_user_turn','doubao_reply_timeout','doubao_rename_timeout']);
 const code=String(error?.message||'').match(/\bdoubao_[a-z_]+\b/)?.[0];
 return allowed.has(code)?code:'doubao_unexpected_error';
}
async function doubaoPump(){
 if(doubaoBusy)return;const {token}=await chrome.storage.local.get('token');if(!token||doubaoBusy)return;
 const base='http://127.0.0.1:18769/browser-bridge';const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':'doubao'};
 let job=null,stage='bootstrap',ownedTabId=null;const pumpStarted=Date.now();doubaoBusy=true;
 try{
  const home='https://www.doubao.com/chat/';
  const isHomeUrl=url=>url===home||url==='https://www.doubao.com/chat';
  const chatUrl=/^https:\/\/www\.doubao\.com\/chat\/\d+$/;
  const localUrl=/^https:\/\/www\.doubao\.com\/chat\/local_\d+$/;
  const saved=await chrome.storage.local.get(['doubaoOwnedTab','doubaoOwnedUrl','doubaoConversations','doubaoSeedApplied','doubaoRecoveryApplied']);const conversations=saved.doubaoConversations||{};let tab,bootstrap={};
  try{bootstrap=await (await fetch(chrome.runtime.getURL('doubao-bootstrap.local.json'))).json();}catch{}
  const recoveryId=bootstrap.recovery_id;
  if(recoveryId&&saved.doubaoRecoveryApplied!==recoveryId){
   const ownedUrl=bootstrap.owned_url;
   const known=saved.doubaoOwnedUrl===ownedUrl||Object.values(conversations).some(value=>value?.url===ownedUrl);
   if(typeof recoveryId!=='string'||!/^[a-zA-Z0-9_-]{16,128}$/.test(recoveryId)||!chatUrl.test(ownedUrl||'')||!known)throw Error('doubao_recovery_unproven');
   const matches=(await chrome.tabs.query({url:'https://www.doubao.com/*'})).filter(candidate=>candidate.url===ownedUrl);
   if(matches.length!==1)throw Error('doubao_recovery_tab_unavailable');
   tab=matches[0];
   await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:ownedUrl,doubaoRecoveryApplied:recoveryId});
  }
  stage='queue';const next=await fetch(base+'/next',{headers});if(!next.ok)return;job=(await next.json()).job;if(!job)return;
  const started=Date.now(),key=job.conversation_key;
  stage='owned_tab';
  const seed=bootstrap.seed;
  const validSeed=seed&&/^[0-9a-f]{64}$/.test(seed.conversation_key||'')&&chatUrl.test(seed.url||'');
  if(!recoveryId&&validSeed&&!conversations[seed.conversation_key]){
   conversations[seed.conversation_key]={url:seed.url,name:String(seed.name||''),lastTurnId:''};
   await chrome.storage.local.set({doubaoConversations:conversations});
  }
  if(!tab&&!recoveryId&&validSeed&&saved.doubaoSeedApplied!==seed.url){
   const open=await chrome.tabs.query({url:'https://www.doubao.com/*'});
   tab=open.find(candidate=>candidate.url===seed.url);
   if(!tab)throw Error('doubao_seed_tab_unavailable');
   await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:tab.url,doubaoSeedApplied:seed.url});
  }
  if(!tab&&Number.isInteger(saved.doubaoOwnedTab)){
   try{tab=await chrome.tabs.get(saved.doubaoOwnedTab);}catch{}
   if(!tab||!tab.url||new URL(tab.url).origin!=='https://www.doubao.com')throw Error('doubao_owned_tab_unavailable');
  }else if(!tab&&!recoveryId&&(isHomeUrl(bootstrap.owned_url)||chatUrl.test(bootstrap.owned_url||'')||localUrl.test(bootstrap.owned_url||''))){
   const open=await chrome.tabs.query({url:'https://www.doubao.com/*'});
   tab=open.find(candidate=>candidate.url===bootstrap.owned_url);
   if(!tab)throw Error('doubao_owned_tab_unproven');
  }else if(!tab&&(saved.doubaoOwnedUrl||Object.keys(conversations).length)){
   throw Error('doubao_owned_tab_unproven');
  }else if(!tab){
   tab=await chrome.tabs.create({url:home,active:false});
  }
  ownedTabId=tab.id;await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:tab.url||home});
  const known=conversations[key];let reused=chatUrl.test(known?.url||'')||localUrl.test(known?.url||''),expectedUrl=reused?known.url:home;
  stage='navigate';
  if(tab.url!==expectedUrl){
   const editing=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>!!(document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"]')?.textContent.trim()||document.querySelector('[data-testid="chat_input"] [data-testid="attachment-image-card"]'))});
   if(editing[0]?.result)throw Error('doubao_user_editing');
  }
  let freshFallback=false;
  if(reused){
   if(tab.url!==expectedUrl)await chrome.tabs.update(tab.id,{url:expectedUrl});
  }else{
   const changed=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{
    if(location.origin!=='https://www.doubao.com')return false;
    if(document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"]')?.textContent.trim()||document.querySelector('[data-testid="chat_input"] [data-testid="attachment-image-card"]'))throw Error('doubao_user_editing');
    if(location.href==='https://www.doubao.com/chat/'||location.href==='https://www.doubao.com/chat')return true;
    const button=Array.from(document.querySelectorAll('button,[role="button"]')).find(e=>e.textContent.trim().startsWith('新对话'));
    if(button){button.click();delete document.documentElement.dataset.wechatDoubaoOwner;return true;}
    return false;
   }});
   if(!changed[0]?.result){await chrome.tabs.update(tab.id,{url:home});freshFallback=true;}
  }
  stage='ready';let ready=false,stableSince=0,lastSignature='';const freshStarted=Date.now(),deadline=freshStarted+30000;
  while(Date.now()<deadline){
   try{const state=await chrome.scripting.executeScript({target:{tabId:tab.id},func:(lastTurnId)=>{const replies=document.querySelectorAll('[data-testid="receive_message"]');const editor=document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"]');return {url:location.href,editor:!!editor,draft:!!editor?.textContent.trim(),attachments:!!document.querySelector('[data-testid="chat_input"] [data-testid="attachment-image-card"]'),answers:replies.length,users:document.querySelectorAll('[data-testid="send_message"]').length,lastAnswerLength:replies.length?(replies[replies.length-1].textContent||'').length:0,hasKnownTurn:!lastTurnId||Array.from(document.querySelectorAll('[data-testid="send_message"]')).some(node=>node.textContent.includes('[wechat-turn:'+lastTurnId+']'))};},args:[reused?known?.lastTurnId||'':'']});
    const value=state[0]?.result;
    if(reused&&isHomeUrl(value?.url)&&value.editor)throw Error('doubao_known_conversation_missing');
   if(reused&&localUrl.test(expectedUrl)&&chatUrl.test(value?.url||'')&&known?.lastTurnId&&value.hasKnownTurn){
    expectedUrl=value.url;conversations[key]={...known,url:expectedUrl,pending:false};
    await chrome.storage.local.set({doubaoConversations:conversations,doubaoOwnedUrl:expectedUrl});
   }
   const emptyFresh=(isHomeUrl(value?.url)||localUrl.test(value?.url||''))&&value.editor&&!value.answers&&!value.users&&!value.draft&&!value.attachments;
   if(!reused&&emptyFresh)expectedUrl=value.url;
   if(!reused&&!freshFallback&&Date.now()-freshStarted>=1500&&!emptyFresh&&(isHomeUrl(value?.url)||value?.url?.startsWith(home))){
     if(value.draft||value.attachments)throw Error('doubao_user_editing');
     await chrome.tabs.update(tab.id,{url:home});freshFallback=true;lastSignature='';stableSince=0;continue;
    }
   if(reused&&known?.pending&&(value?.users||value?.answers))throw Error('doubao_pending_unverified');
   const eligible=value?.url===expectedUrl&&value.editor&&value.hasKnownTurn&&(reused||emptyFresh);
    const signature=eligible?JSON.stringify([value.url,value.users,value.answers,value.lastAnswerLength]):'';
    if(signature&&signature===lastSignature&&Date.now()-stableSince>=900){ready=true;break;}
    if(signature!==lastSignature){lastSignature=signature;stableSince=Date.now();}
  }catch(error){if(['doubao_user_editing','doubao_known_conversation_missing','doubao_pending_unverified'].includes(error?.message))throw error;}await new Promise(r=>setTimeout(r,300));
  }
  if(!ready)throw Error('doubao_login_or_load');
  if(!reused&&localUrl.test(expectedUrl)){
   conversations[key]={url:expectedUrl,name:job.contact_name||'',lastTurnId:'',pending:true};
   await chrome.storage.local.set({doubaoConversations:conversations});
  }
  stage='setup';await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:expectedUrl});
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['doubao_web.js']});
  stage='reply';
  const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(prompt,key,reused,images,expectedUrl,turnId,contactName)=>await window.wechatDoubaoReply(prompt,key,reused,images,expectedUrl,turnId,contactName),args:[job.prompt,key,reused,job.images||[],expectedUrl,job.id,job.contact_name||'']});
  const outcome=result[0]?.result;if(!outcome?.reply||!chatUrl.test(outcome.url)||(reused&&outcome.url!==expectedUrl))throw Error('doubao_empty_or_wrong_conversation');
  stage='complete';const finished=await chrome.tabs.get(tab.id);if(finished.url!==outcome.url)throw Error('doubao_result_url_changed');
  conversations[key]={url:finished.url,name:job.contact_name||'',lastTurnId:job.id};await chrome.storage.local.set({doubaoConversations:conversations,doubaoOwnedUrl:finished.url});
  const accepted=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:outcome.reply,browser_meta:{tab_id:tab.id,reused,managed_tabs:1,bridge_total_ms:Date.now()-started}})});
  if(!accepted.ok)throw Error('doubao_result_rejected');
 }catch(error){
  const code=doubaoErrorCode(error),failure={code,stage,job_id:job?.id||'',at:Date.now(),elapsed_ms:Date.now()-pumpStarted};
  if(Number.isInteger(ownedTabId))failure.tab_id=ownedTabId;
  await chrome.storage.local.set({doubaoLastFailure:failure}).catch(()=>{});
  if(job)await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:code,stage})}).catch(()=>{});
 }
 finally{doubaoBusy=false;}
}
async function connectDoubao(){
 if(doubaoSocket)return;const {token}=await chrome.storage.local.get('token');if(!token)return;
 const socket=new WebSocket('ws://127.0.0.1:18769/browser-bridge/events');doubaoSocket=socket;
 socket.onopen=()=>{socket.send(JSON.stringify({token,provider:'doubao'}));doubaoPump();};socket.onmessage=()=>doubaoPump();
 socket.onclose=()=>{doubaoSocket=null;setTimeout(connectDoubao,2000);};socket.onerror=()=>{try{socket.close();}catch{}};
}
chrome.alarms.onAlarm.addListener(()=>{connectDoubao();doubaoPump();});connectDoubao();

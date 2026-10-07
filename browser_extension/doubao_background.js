// One owned Doubao page; no access to pre-existing personal conversations.
let doubaoBusy=false,doubaoSocket=null;
async function doubaoPump(){
 if(doubaoBusy)return;const {token}=await chrome.storage.local.get('token');if(!token)return;
 const base='http://127.0.0.1:18769/browser-bridge';const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':'doubao'};
 let job=null;doubaoBusy=true;
 try{
  const next=await fetch(base+'/next',{headers});if(!next.ok)return;job=(await next.json()).job;if(!job)return;
  const started=Date.now(),key=job.conversation_key;
  const home='https://www.doubao.com/chat/';
  const chatUrl=/^https:\/\/www\.doubao\.com\/chat\/\d+$/;
  const localUrl=/^https:\/\/www\.doubao\.com\/chat\/local_\d+$/;
  const saved=await chrome.storage.local.get(['doubaoOwnedTab','doubaoOwnedUrl','doubaoConversations','doubaoSeedApplied']);const conversations=saved.doubaoConversations||{};let tab,bootstrap={};
  try{bootstrap=await (await fetch(chrome.runtime.getURL('doubao-bootstrap.local.json'))).json();}catch{}
  const seed=bootstrap.seed;
  const validSeed=seed&&/^[0-9a-f]{64}$/.test(seed.conversation_key||'')&&chatUrl.test(seed.url||'');
  if(validSeed&&!conversations[seed.conversation_key]){
   conversations[seed.conversation_key]={url:seed.url,name:String(seed.name||''),lastTurnId:''};
   await chrome.storage.local.set({doubaoConversations:conversations});
  }
  if(validSeed&&saved.doubaoSeedApplied!==seed.url){
   const open=await chrome.tabs.query({url:'https://www.doubao.com/*'});
   tab=open.find(candidate=>candidate.url===seed.url);
   if(!tab)throw Error('doubao_seed_tab_unavailable');
   await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:tab.url,doubaoSeedApplied:seed.url});
  }
  if(!tab&&Number.isInteger(saved.doubaoOwnedTab)){
   try{tab=await chrome.tabs.get(saved.doubaoOwnedTab);}catch{}
   if(!tab||!tab.url||new URL(tab.url).origin!=='https://www.doubao.com')throw Error('doubao_owned_tab_unavailable');
  }else if(!tab&&(bootstrap.owned_url===home||chatUrl.test(bootstrap.owned_url||'')||localUrl.test(bootstrap.owned_url||''))){
   const open=await chrome.tabs.query({url:'https://www.doubao.com/*'});
   tab=open.find(candidate=>candidate.url===bootstrap.owned_url);
   if(!tab)throw Error('doubao_owned_tab_unproven');
  }else if(!tab&&(saved.doubaoOwnedUrl||Object.keys(conversations).length)){
   throw Error('doubao_owned_tab_unproven');
  }else if(!tab){
   tab=await chrome.tabs.create({url:home,active:false});
  }
  await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:tab.url||home});
  const known=conversations[key];let reused=chatUrl.test(known?.url||'')||localUrl.test(known?.url||''),expectedUrl=reused?known.url:home;
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
    if(location.href==='https://www.doubao.com/chat/')return true;
    const button=Array.from(document.querySelectorAll('button,[role="button"]')).find(e=>e.textContent.trim().startsWith('新对话'));
    if(button){button.click();delete document.documentElement.dataset.wechatDoubaoOwner;return true;}
    return false;
   }});
   if(!changed[0]?.result){await chrome.tabs.update(tab.id,{url:home});freshFallback=true;}
  }
  let ready=false,stableSince=0,lastSignature='';const freshStarted=Date.now(),deadline=freshStarted+30000;
  while(Date.now()<deadline){
   try{const state=await chrome.scripting.executeScript({target:{tabId:tab.id},func:(lastTurnId)=>{const replies=document.querySelectorAll('[data-testid="receive_message"]');const editor=document.querySelector('[data-testid="chat_input_input"] [contenteditable="true"]');return {url:location.href,editor:!!editor,draft:!!editor?.textContent.trim(),attachments:!!document.querySelector('[data-testid="chat_input"] [data-testid="attachment-image-card"]'),answers:replies.length,users:document.querySelectorAll('[data-testid="send_message"]').length,lastAnswerLength:replies.length?(replies[replies.length-1].textContent||'').length:0,hasKnownTurn:!lastTurnId||Array.from(document.querySelectorAll('[data-testid="send_message"]')).some(node=>node.textContent.includes('[wechat-turn:'+lastTurnId+']'))};},args:[reused?known?.lastTurnId||'':'']});
    const value=state[0]?.result;
    if(reused&&value?.url===home&&value.editor)throw Error('doubao_known_conversation_missing');
   if(reused&&localUrl.test(expectedUrl)&&chatUrl.test(value?.url||'')&&known?.lastTurnId&&value.hasKnownTurn){
    expectedUrl=value.url;conversations[key]={...known,url:expectedUrl,pending:false};
    await chrome.storage.local.set({doubaoConversations:conversations,doubaoOwnedUrl:expectedUrl});
   }
   const emptyFresh=(value?.url===home||localUrl.test(value?.url||''))&&value.editor&&!value.answers&&!value.users&&!value.draft&&!value.attachments;
   if(!reused&&emptyFresh&&localUrl.test(value.url))expectedUrl=value.url;
   if(!reused&&!freshFallback&&Date.now()-freshStarted>=1500&&!emptyFresh&&value?.url?.startsWith(home)){
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
  await chrome.storage.local.set({doubaoOwnedTab:tab.id,doubaoOwnedUrl:expectedUrl});
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['doubao_web.js']});
  const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(prompt,key,reused,images,expectedUrl,turnId,contactName)=>await window.wechatDoubaoReply(prompt,key,reused,images,expectedUrl,turnId,contactName),args:[job.prompt,key,reused,job.images||[],expectedUrl,job.id,job.contact_name||'']});
  const outcome=result[0]?.result;if(!outcome?.reply||!chatUrl.test(outcome.url)||(reused&&outcome.url!==expectedUrl))throw Error('doubao_empty_or_wrong_conversation');
  const finished=await chrome.tabs.get(tab.id);if(finished.url!==outcome.url)throw Error('doubao_result_url_changed');
  conversations[key]={url:finished.url,name:job.contact_name||'',lastTurnId:job.id};await chrome.storage.local.set({doubaoConversations:conversations,doubaoOwnedUrl:finished.url});
  const accepted=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:outcome.reply,browser_meta:{tab_id:tab.id,reused,managed_tabs:1,bridge_total_ms:Date.now()-started}})});
  if(!accepted.ok)throw Error('doubao_result_rejected');
 }catch(error){if(job)await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:'reply'})}).catch(()=>{});}
 finally{doubaoBusy=false;}
}
async function connectDoubao(){
 if(doubaoSocket)return;const {token}=await chrome.storage.local.get('token');if(!token)return;
 const socket=new WebSocket('ws://127.0.0.1:18769/browser-bridge/events');doubaoSocket=socket;
 socket.onopen=()=>{socket.send(JSON.stringify({token,provider:'doubao'}));doubaoPump();};socket.onmessage=()=>doubaoPump();
 socket.onclose=()=>{doubaoSocket=null;setTimeout(connectDoubao,2000);};socket.onerror=()=>{try{socket.close();}catch{}};
}
chrome.alarms.onAlarm.addListener(()=>{connectDoubao();doubaoPump();});connectDoubao();

// Optional, isolated DeepSeek draft channel. No hidden API calls or quota bypass.
let deepseekBusy=false,deepseekSocket=null;
function deepseekErrorCode(error){
 const allowed=new Set(['deepseek_recovery_unproven','deepseek_recovery_tab_unavailable','deepseek_visual_not_verified','deepseek_seed_tab_unavailable','deepseek_owned_tab_unavailable','deepseek_owned_tab_unproven','deepseek_user_editing','deepseek_known_conversation_missing','deepseek_known_turn_not_visible','deepseek_conversation_not_loaded','deepseek_ready_script_failed','deepseek_page_loading','deepseek_login_or_load_required','deepseek_reply_missing_result','deepseek_empty_or_wrong_conversation','deepseek_result_url_changed','deepseek_result_rejected','deepseek_conversation_changed','deepseek_wrong_conversation','deepseek_not_fresh_home','deepseek_existing_conversation','deepseek_wrong_contact','deepseek_user_editing_or_not_logged_in','deepseek_mode_control_missing','deepseek_fast_mode_not_verified','deepseek_turn_already_present','deepseek_prompt_not_inserted','deepseek_ownership_lost','deepseek_newer_user_turn','deepseek_turn_not_visible','deepseek_reply_timeout']);
 const message=String(error?.message||'');
 if(/^background_window_[a-z_]+$/.test(message))return 'deepseek_background_window_unavailable';
 const code=message.match(/\bdeepseek_[a-z_]+\b/)?.[0];
 return allowed.has(code)?code:'deepseek_unexpected_error';
}
function deepseekErrorStage(value,fallback){
 return new Set(['preflight','mode','prompt','send','reply_marker','reply','persist']).has(value)?value:fallback;
}
async function deepseekPump(){
 if(deepseekBusy)return;
 const {token}=await chrome.storage.local.get('token');if(!token||deepseekBusy)return;
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':'deepseek'};
 const base='http://127.0.0.1:18769/browser-bridge';let job=null,stage='bootstrap',ownedTabId=null,readyState=null;const pumpStarted=Date.now();deepseekBusy=true;
 try{
  stage='background_window';await WechatBackgroundWindow.ensure('deepseek');stage='bootstrap';
  const previous=await chrome.storage.local.get(['deepseekOwnedTab','deepseekOwnedUrl','deepseekConversations','deepseekSeedApplied','deepseekRecoveryApplied']);
  const conversations=previous.deepseekConversations||{};
  const home='https://chat.deepseek.com/';
  const chatUrl=/^https:\/\/chat\.deepseek\.com\/a\/chat\/s\/[^/?#]+$/;
  let bootstrap={};try{bootstrap=await (await fetch(chrome.runtime.getURL('deepseek-bootstrap.local.json'))).json();}catch{}
  const recoveryId=bootstrap.recovery_id;let tab;
  if(recoveryId&&previous.deepseekRecoveryApplied!==recoveryId){
   const ownedUrl=bootstrap.owned_url;
   const known=previous.deepseekOwnedUrl===ownedUrl||Object.values(conversations).some(value=>value?.url===ownedUrl);
   if(typeof recoveryId!=='string'||!/^[a-zA-Z0-9_-]{16,128}$/.test(recoveryId)||!chatUrl.test(ownedUrl||'')||!known)throw Error('deepseek_recovery_unproven');
   const matches=(await chrome.tabs.query({url:'https://chat.deepseek.com/*'})).filter(candidate=>candidate.url===ownedUrl);
   if(matches.length!==1)throw Error('deepseek_recovery_tab_unavailable');
   tab=matches[0];
   await chrome.storage.local.set({deepseekOwnedTab:tab.id,deepseekOwnedUrl:ownedUrl,deepseekRecoveryApplied:recoveryId});
  }
  stage='background_window';await WechatBackgroundWindow.ensure('deepseek');stage='queue';
  const bridgeStarted=Date.now();
  const next=await fetch(base+'/next',{headers});if(!next.ok)return;job=(await next.json()).job;if(!job)return;
  stage='validate_job';if(job.images?.length)throw Error('deepseek_visual_not_verified');
  const key=job.conversation_key;
  const seed=bootstrap.seed;
  if(!tab&&!recoveryId&&seed&&/^[0-9a-f]{64}$/.test(seed.conversation_key||'')&&chatUrl.test(seed.url||'')&&previous.deepseekSeedApplied!==seed.url){
   const open=await chrome.tabs.query({url:'https://chat.deepseek.com/*'});
   tab=open.find(candidate=>candidate.url===seed.url);
   if(!tab)throw Error('deepseek_seed_tab_unavailable');
   if(!conversations[seed.conversation_key])conversations[seed.conversation_key]={url:seed.url,name:String(seed.name||''),used:Date.now(),lastTurnId:''};
   await chrome.storage.local.set({deepseekConversations:conversations,deepseekOwnedTab:tab.id,deepseekOwnedUrl:tab.url,deepseekSeedApplied:seed.url});
  }
  stage='owned_tab';const known=conversations[key];
  let url=chatUrl.test(known?.url||'')?known.url:home;
  if(!tab&&Number.isInteger(previous.deepseekOwnedTab)){
   try{tab=await chrome.tabs.get(previous.deepseekOwnedTab);}catch{}
   if(!tab||!tab.url||new URL(tab.url).origin!=='https://chat.deepseek.com')throw Error('deepseek_owned_tab_unavailable');
  }else if(!tab&&(previous.deepseekOwnedUrl||Object.keys(conversations).length)){
   throw Error('deepseek_owned_tab_unproven');
  }else if(!tab){
   tab=await WechatBackgroundWindow.createModelTab({url:home});
   await chrome.storage.local.set({deepseekOwnedTab:tab.id,deepseekOwnedUrl:home});
  }
  tab=await WechatBackgroundWindow.ensureOwnedTab(tab.id,'deepseek');
  ownedTabId=tab.id;stage='navigation';
  if(tab.url!==url){
   const editing=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>!!document.querySelector('textarea')?.value.trim()});
   if(editing[0]?.result)throw Error('deepseek_user_editing');
   if(url!==home&&!known?.lastTurnId){await chrome.tabs.update(tab.id,{url});}
   else{
   const navigated=await chrome.scripting.executeScript({target:{tabId:tab.id},func:(url)=>{
    if(location.origin!=='https://chat.deepseek.com')return false;
    if(url==='https://chat.deepseek.com/'){
     const control=Array.from(document.querySelectorAll('body *')).find(e=>e.children.length===0&&e.textContent.trim()==='开启新对话');
     if(!control)return false;control.click();delete document.documentElement.dataset.wechatDeepseekOwner;return true;
    }
    const path=new URL(url).pathname;
    const link=Array.from(document.querySelectorAll('a[href]')).find(a=>a.getAttribute('href')===path);
    if(!link)return false;link.click();delete document.documentElement.dataset.wechatDeepseekOwner;return true;
   },args:[url]});
   if(!navigated[0]?.result)await chrome.tabs.update(tab.id,{url});
   }
  }
  const slots=[{id:tab.id,key,used:Date.now()}];
  await chrome.storage.session.set({deepseekSlots:slots});
  let reuse=url!==home,restoredFromLocal=false;
  stage='ready';const deadline=Date.now()+30000;let ready=false,stableSince=0,lastSignature='',lastValue=null,readyScriptFailed=false;
  while(Date.now()<deadline){
   try{const state=await chrome.scripting.executeScript({target:{tabId:tab.id},func:(lastTurnId)=>{const answers=document.querySelectorAll('.ds-assistant-message-main-content');const blocks=Array.from(document.querySelector('.ds-virtual-list-visible-items')?.children||[]);const users=blocks.filter(node=>!node.querySelector('.ds-assistant-message-main-content')&&(node.textContent||'').trim());return {url:location.href,ready:document.readyState,editor:!!document.querySelector('textarea[placeholder="给 DeepSeek 发送消息 "]'),answers:answers.length,users:users.length,lastAnswerLength:answers.length?(answers[answers.length-1].textContent||'').length:0,hasKnownTurn:!lastTurnId||users.some(node=>node.textContent.includes('[wechat-turn:'+lastTurnId+']'))};},args:[reuse?known?.lastTurnId||'':'']});
    const value=state[0]?.result;
    if(value){lastValue=value;readyState={url_matches_expected:value.url===url,document_ready:['loading','interactive','complete'].includes(value.ready)?value.ready:'unknown',editor:!!value.editor,has_known_turn:!!value.hasKnownTurn,visible_users:Number.isInteger(value.users)?value.users:0,visible_answers:Number.isInteger(value.answers)?value.answers:0};}
    if(reuse&&value?.url===home&&value.ready!=='loading'&&value.editor)throw Error('deepseek_known_conversation_missing');
    const eligible=value?.url===url&&value.ready!=='loading'&&value.editor&&value.hasKnownTurn&&(reuse||(!value.answers&&!value.users));
    const signature=eligible?JSON.stringify([value.url,value.users,value.answers,value.lastAnswerLength]):'';
    if(signature&&signature===lastSignature&&Date.now()-stableSince>=900){ready=true;break;}
    if(signature!==lastSignature){lastSignature=signature;stableSince=Date.now();}
   }catch(error){if(error?.message==='deepseek_known_conversation_missing')throw error;readyScriptFailed=true;}
   await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(!ready){
   let code='deepseek_login_or_load_required';
   if(!lastValue&&readyScriptFailed)code='deepseek_ready_script_failed';
   else if(lastValue&&lastValue.url!==url)code='deepseek_conversation_not_loaded';
   else if(lastValue?.ready==='loading')code='deepseek_page_loading';
   else if(reuse&&lastValue?.editor&&!lastValue.hasKnownTurn)code='deepseek_known_turn_not_visible';
   throw Error(code);
  }
  stage='setup';
  if(!reuse)await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{if(location.pathname==='/'&&!document.querySelector('.ds-assistant-message-main-content'))delete document.documentElement.dataset.wechatDeepseekOwner;}});
  await chrome.storage.local.set({deepseekOwnedTab:tab.id,deepseekOwnedUrl:url});
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_web.js']});
  stage='reply';const started=Date.now();const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(prompt,key,reuse,expectedUrl,turnId,contactName)=>{
   // Chromium may omit the result when an injected async function rejects.
   // Catch inside the injected function and carry only the fixed diagnostic tokens.
   try{return await window.wechatDeepseekDraft(prompt,key,reuse,expectedUrl,turnId,contactName);}
   catch(error){return {error:error?.deepseekCode||'deepseek_unexpected_error',stage:error?.deepseekStage||'reply'};}
  },args:[job.prompt,key,reuse,url,job.id,job.contact_name||'']});
  const outcome=result[0]?.result;
  if(outcome?.error){stage=deepseekErrorStage(outcome.stage,stage);throw Error(deepseekErrorCode({message:outcome.error}));}
  if(!outcome)throw Error('deepseek_reply_missing_result');
  if(!outcome.reply||!chatUrl.test(outcome.url)||(!outcome.reset&&reuse&&outcome.url!==url))throw Error('deepseek_empty_or_wrong_conversation');
  stage='title';if(job.contact_name){try{await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_title.js']});await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(name)=>await window.wechatDeepseekRename(name),args:[job.contact_name]});}catch(error){console.warn('DeepSeek contact title pending');}}
  stage='complete';
  const finished=await chrome.tabs.get(tab.id);
  if(finished.url!==outcome.url)throw Error('deepseek_result_url_changed');
  conversations[key]={url:finished.url,name:job.contact_name||'',used:Date.now(),lastTurnId:job.id};await chrome.storage.local.set({deepseekConversations:conversations,deepseekOwnedUrl:finished.url});
  const accepted=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:outcome.reply,browser_meta:{tab_id:tab.id,queue_wait_ms:Math.max(0,bridgeStarted-job.created*1000),bridge_total_ms:Date.now()-bridgeStarted,web_reply_ms:Date.now()-started,reused:outcome.reused,restored_from_local_context:restoredFromLocal,managed_tabs:slots.length}})});
  if(!accepted.ok)throw Error('deepseek_result_rejected');
 }catch(error){
  const code=deepseekErrorCode(error),failure={code,stage,job_id:job?.id||'',at:Date.now(),elapsed_ms:Date.now()-pumpStarted};
  if(Number.isInteger(ownedTabId))failure.tab_id=ownedTabId;
  if(stage==='ready'&&readyState)failure.ready_state=readyState;
  await chrome.storage.local.set({deepseekLastFailure:failure}).catch(()=>{});
  console.warn('DeepSeekBridge failure: '+code+' at '+stage);
  if(job)await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:code,stage})}).catch(()=>{});
 }finally{deepseekBusy=false;}
}
async function connectDeepseek(){
 if(deepseekSocket)return;const {token}=await chrome.storage.local.get('token');if(!token)return;
 const socket=new WebSocket('ws://127.0.0.1:18769/browser-bridge/events');deepseekSocket=socket;
 socket.onopen=()=>{socket.send(JSON.stringify({token,provider:'deepseek'}));deepseekPump();};
 socket.onmessage=()=>deepseekPump();socket.onclose=()=>{deepseekSocket=null;setTimeout(connectDeepseek,2000);};socket.onerror=()=>{try{socket.close();}catch{}};
}
chrome.alarms.onAlarm.addListener(()=>{connectDeepseek();deepseekPump();});
connectDeepseek();

// Optional, isolated DeepSeek draft channel. No hidden API calls or quota bypass.
let deepseekBusy=false,deepseekSocket=null;
async function deepseekPump(){
 if(deepseekBusy)return;
 const {token}=await chrome.storage.local.get('token');if(!token||deepseekBusy)return;
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':'deepseek'};
 const base='http://127.0.0.1:18769/browser-bridge';let job=null;deepseekBusy=true;
 try{
  await WechatBackgroundWindow.ensure();
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
  await WechatBackgroundWindow.ensure();
  const bridgeStarted=Date.now();
  const next=await fetch(base+'/next',{headers});if(!next.ok)return;job=(await next.json()).job;if(!job)return;
  if(job.images?.length)throw Error('deepseek_visual_not_verified');
  const key=job.conversation_key;
  const seed=bootstrap.seed;
  if(!tab&&!recoveryId&&seed&&/^[0-9a-f]{64}$/.test(seed.conversation_key||'')&&chatUrl.test(seed.url||'')&&previous.deepseekSeedApplied!==seed.url){
   const open=await chrome.tabs.query({url:'https://chat.deepseek.com/*'});
   tab=open.find(candidate=>candidate.url===seed.url);
   if(!tab)throw Error('deepseek_seed_tab_unavailable');
   if(!conversations[seed.conversation_key])conversations[seed.conversation_key]={url:seed.url,name:String(seed.name||''),used:Date.now(),lastTurnId:''};
   await chrome.storage.local.set({deepseekConversations:conversations,deepseekOwnedTab:tab.id,deepseekOwnedUrl:tab.url,deepseekSeedApplied:seed.url});
  }
  const known=conversations[key];
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
  const deadline=Date.now()+30000;let ready=false,stableSince=0,lastSignature='';
  while(Date.now()<deadline){
   try{const state=await chrome.scripting.executeScript({target:{tabId:tab.id},func:(lastTurnId)=>{const answers=document.querySelectorAll('.ds-assistant-message-main-content');const blocks=Array.from(document.querySelector('.ds-virtual-list-visible-items')?.children||[]);const users=blocks.filter(node=>!node.querySelector('.ds-assistant-message-main-content')&&(node.textContent||'').trim());return {url:location.href,ready:document.readyState,editor:!!document.querySelector('textarea[placeholder="给 DeepSeek 发送消息 "]'),answers:answers.length,users:users.length,lastAnswerLength:answers.length?(answers[answers.length-1].textContent||'').length:0,hasKnownTurn:!lastTurnId||users.some(node=>node.textContent.includes('[wechat-turn:'+lastTurnId+']'))};},args:[reuse?known?.lastTurnId||'':'']});
    const value=state[0]?.result;
    if(reuse&&value?.url===home&&value.ready!=='loading'&&value.editor)throw Error('deepseek_known_conversation_missing');
    const eligible=value?.url===url&&value.ready!=='loading'&&value.editor&&value.hasKnownTurn&&(reuse||(!value.answers&&!value.users));
    const signature=eligible?JSON.stringify([value.url,value.users,value.answers,value.lastAnswerLength]):'';
    if(signature&&signature===lastSignature&&Date.now()-stableSince>=900){ready=true;break;}
    if(signature!==lastSignature){lastSignature=signature;stableSince=Date.now();}
   }catch(error){if(error?.message==='deepseek_known_conversation_missing')throw error;}
   await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(!ready)throw Error('deepseek_login_or_load_required');
  if(!reuse)await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>{if(location.pathname==='/'&&!document.querySelector('.ds-assistant-message-main-content'))delete document.documentElement.dataset.wechatDeepseekOwner;}});
  await chrome.storage.local.set({deepseekOwnedTab:tab.id,deepseekOwnedUrl:url});
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_web.js']});
  const started=Date.now();const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(prompt,key,reuse,expectedUrl,turnId,contactName)=>await window.wechatDeepseekDraft(prompt,key,reuse,expectedUrl,turnId,contactName),args:[job.prompt,key,reuse,url,job.id,job.contact_name||'']});
  const outcome=result[0]?.result;if(!outcome?.reply||!chatUrl.test(outcome.url)||(!outcome.reset&&reuse&&outcome.url!==url))throw Error('deepseek_empty_or_wrong_conversation');
  if(job.contact_name){try{await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_title.js']});await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(name)=>await window.wechatDeepseekRename(name),args:[job.contact_name]});}catch(error){console.warn('DeepSeek contact title pending');}}
  const finished=await chrome.tabs.get(tab.id);
  if(finished.url!==outcome.url)throw Error('deepseek_result_url_changed');
  conversations[key]={url:finished.url,name:job.contact_name||'',used:Date.now(),lastTurnId:job.id};await chrome.storage.local.set({deepseekConversations:conversations,deepseekOwnedUrl:finished.url});
  const accepted=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:outcome.reply,browser_meta:{tab_id:tab.id,queue_wait_ms:Math.max(0,bridgeStarted-job.created*1000),bridge_total_ms:Date.now()-bridgeStarted,web_reply_ms:Date.now()-started,reused:outcome.reused,restored_from_local_context:restoredFromLocal,managed_tabs:slots.length}})});
  if(!accepted.ok)throw Error('deepseek_result_rejected');
 }catch(error){
  console.warn('DeepSeekBridge failure: '+String(error?.message||'unknown').slice(0,120));
  if(job)await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:'reply'})}).catch(()=>{});
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

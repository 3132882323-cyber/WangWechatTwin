// Fixed owned tab pool. Same contact keeps its temporary chat; no prior user tabs.
importScripts('pool_core.js');
importScripts('background_window.js');
importScripts('deepseek_background.js');
importScripts('doubao_background.js');
const base='http://127.0.0.1:18769/browser-bridge';
const rootUrl='https://chatgpt.com/?temporary-chat=true';
let busy=false;
chrome.storage.local.set({bridgeWorkerVersion:4});
chrome.alarms.create('poll',{periodInMinutes:.5});
chrome.runtime.onInstalled.addListener(()=>chrome.alarms.create('poll',{periodInMinutes:.5}));
chrome.action.onClicked.addListener(()=>chrome.runtime.openOptionsPage());
async function store(slots){await WechatPool.writePool(chrome.storage,slots);}
let chatgptMigrationInFlight=null;
function migrateChatgptPool(){if(!chatgptMigrationInFlight)chatgptMigrationInFlight=migrateChatgptPoolInner().finally(()=>{chatgptMigrationInFlight=null;});return chatgptMigrationInFlight;}
async function migrateChatgptPoolInner(){
 const existing=await WechatPool.readPool(chrome.storage);
 let migration={};try{migration=await (await fetch(chrome.runtime.getURL('gpt-migration.local.json'))).json();}catch{return;}
 if(!/^[a-zA-Z0-9_-]{16,128}$/.test(migration.migration_id||''))return;
 const applied=await chrome.storage.local.get('chatgptMigrationApplied');if(applied.chatgptMigrationApplied===migration.migration_id)return;
 if(!Array.isArray(migration.slots)||!migration.slots.length||migration.slots.length>3)throw Error('chatgpt_migration_unproven');
 const merged=existing.map(slot=>({...slot}));
 for(const claim of migration.slots){
  if(!Number.isInteger(claim?.tabId)||claim.tabId<0||!Number.isInteger(claim.slotId)||claim.slotId<0||claim.slotId>2||!/^[0-9a-f]{64}$/.test(claim.key||'')||!WechatPool.validUrl(claim.url))throw Error('chatgpt_migration_unproven');
  let tab;try{tab=await chrome.tabs.get(claim.tabId);}catch{throw Error('chatgpt_migration_tab_unavailable');}
  if(tab.url!==claim.url)throw Error('chatgpt_migration_tab_unavailable');
  const proof=await chrome.scripting.executeScript({target:{tabId:claim.tabId},func:(url,key)=>{
   const editor=document.querySelector('[role="textbox"][contenteditable="true"]');
   return location.href===url&&location.origin==='https://chatgpt.com'&&new URL(location.href).searchParams.get('temporary-chat')==='true'&&document.readyState!=='loading'&&document.documentElement.dataset.wechatBridgeOwner===key&&!!editor&&!editor.textContent.trim();
  },args:[claim.url,claim.key]});
  if(proof[0]?.result!==true)throw Error('chatgpt_migration_page_unproven');
  const same=merged.find(slot=>slot.tabId===claim.tabId);
  if(same){if(same.url!==claim.url||same.key!==claim.key||same.slotId!==claim.slotId)throw Error('chatgpt_migration_conflict');continue;}
  if(merged.some(slot=>slot.slotId===claim.slotId)||merged.length>=3)throw Error('chatgpt_migration_conflict');
  merged.push({tabId:claim.tabId,slotId:claim.slotId,url:claim.url,key:claim.key,turns:Number.isInteger(claim.turns)&&claim.turns>=0?claim.turns:1,used:Date.now()});
 }
 await chrome.storage.local.set({bridgePool:merged,chatgptMigrationApplied:migration.migration_id});
}
let chatgptRecoveryInFlight=null;
function recoverChatgptPage(){if(!chatgptRecoveryInFlight)chatgptRecoveryInFlight=recoverChatgptPageInner().finally(()=>{chatgptRecoveryInFlight=null;});return chatgptRecoveryInFlight;}
async function recoverChatgptPageInner(){
 await migrateChatgptPool();
 let b={};try{b=await (await fetch(chrome.runtime.getURL('chatgpt-bootstrap.local.json'))).json();}catch{return;}
 if(!/^[a-zA-Z0-9_-]{8,80}$/.test(b.recovery_id||'')||b.owned_url!==rootUrl)return;
 const saved=await chrome.storage.local.get('chatgptRecoveryApplied');if(saved.chatgptRecoveryApplied===b.recovery_id)return;
 const open=await chrome.tabs.query({url:'https://chatgpt.com/*'});const matches=open.filter(t=>t.url===rootUrl);if(matches.length!==1)throw Error('chatgpt_recovery_page_unavailable');
 const t=matches[0];const check=await chrome.scripting.executeScript({target:{tabId:t.id},func:()=>{const editor=document.querySelector('[role="textbox"][contenteditable="true"]');return {blank:document.readyState!=='loading'&&!!editor&&!editor.textContent.trim()&&!document.querySelector('[data-message-author-role], [data-content-search-unit-key$=":user"], [data-content-search-unit-key$=":assistant"]'),temporary:location.href==='https://chatgpt.com/?temporary-chat=true'};}});
 if(!check[0]?.result?.blank||!check[0]?.result?.temporary)throw Error('chatgpt_recovery_page_not_blank');
 const slots=await WechatPool.readPool(chrome.storage);
 if(!slots.some(slot=>slot.tabId===t.id)){if(slots.length>=3)throw Error('chatgpt_recovery_capacity');slots.push({tabId:t.id,slotId:[0,1,2].find(id=>!slots.some(slot=>slot.slotId===id)),url:rootUrl,turns:0,bootstrapIdle:true,used:Date.now()});}
 await store(slots);await WechatBackgroundWindow.ensureOwnedTab(t.id,'chatgpt');await chrome.storage.local.set({chatgptRecoveryApplied:b.recovery_id});
}
Promise.resolve(WechatBackgroundWindow.startup()).then(()=>migrateChatgptPool()).then(()=>recoverChatgptPage()).catch(()=>console.warn('ChatGPT explicit page migration/recovery pending'));
async function acquire(key){
 await WechatBackgroundWindow.ensure('chatgpt');
 await migrateChatgptPool();
 await recoverChatgptPage();
 const bridgePool=await WechatPool.readPool(chrome.storage);
 const slots=[];
 for(const s of bridgePool){let t;try{t=await chrome.tabs.get(s.tabId);}catch{throw Error('chatgpt_owned_tab_unavailable');}if(t.url!==s.url||!WechatPool.validUrl(t.url))throw Error('chatgpt_owned_tab_unavailable');slots.push(s);}
 await WechatBackgroundWindow.ensure('chatgpt');
 const idle=slots.find(slot=>slot.bootstrapIdle);const choice=idle?{slot:idle,reset:false,reused:false}:WechatPool.choose(slots,key,Date.now());let s=choice.slot;
 if(!s){const t=await WechatBackgroundWindow.createModelTab({url:rootUrl});s={tabId:t.id,slotId:[0,1,2].find(i=>!slots.some(x=>x.slotId===i)),turns:0};slots.push(s);}
 else if(choice.reset){
  try{const old=await chrome.scripting.executeScript({target:{tabId:s.tabId},func:()=>performance.timeOrigin});s.navigationOriginBefore=old[0]?.result;}catch{s.navigationOriginBefore=null;}
  await chrome.tabs.update(s.tabId,{url:rootUrl});
 }else{s.navigationOriginBefore=null;}
 if(choice.reset){s.turns=0;s.failed=false;s.url=rootUrl;}
 delete s.bootstrapIdle;s.key=key;s.used=Date.now();await store(slots);
 return {slots,slot:s,reused:choice.reused};
}
async function loadTab(id,expectedUrl,oldOrigin){
 const deadline=Date.now()+60000;
 while(Date.now()<deadline){
  try{
   const t=await chrome.tabs.get(id);
   if(t.url===expectedUrl){
    const result=await chrome.scripting.executeScript({target:{tabId:id},func:()=>({url:location.href,ready:document.readyState,origin:performance.timeOrigin,editor:!!document.querySelector('[role="textbox"][contenteditable="true"]')})});
    const page=result[0]?.result;
    if(page?.url===expectedUrl&&page.ready!=='loading'&&page.editor&&(!oldOrigin||page.origin!==oldOrigin))return;
   }
  }catch{}
  await new Promise(r=>setTimeout(r,750));
 }
 throw Error('load');
}
async function pump(){
 if(busy)return;
 const {token}=await chrome.storage.local.get('token');if(!token)return;
 const {bridgeLease}=await chrome.storage.session.get('bridgeLease');if(bridgeLease?.until>Date.now())return;
 await chrome.storage.local.set({bridgeWorkerVersion:4});
 busy=true;let job=null,pool=null,stage='queue';
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Version':'4','X-Wechat-Bridge-Build':'fast-sticker-v2'};
 try{
   await WechatBackgroundWindow.ensure('chatgpt');
  const r=await fetch(base+'/next',{headers});if(!r.ok)throw Error('bridge');
  job=(await r.json()).job;if(!job)return;
  const bridgeStarted=Date.now();const queueWait=Math.max(0,Math.round(bridgeStarted-job.created*1000));
  await chrome.storage.session.set({bridgeLease:{id:job.id,until:Date.now()+240000}});
  pool=await acquire(job.conversation_key||'isolated:'+job.id);stage='load';await loadTab(pool.slot.tabId,pool.slot.url,pool.slot.navigationOriginBefore);
  stage='setup';await chrome.scripting.executeScript({target:{tabId:pool.slot.tabId},files:['web_chat.js']});
  stage='reply';const replyStarted=Date.now();
  const reply=await chrome.scripting.executeScript({target:{tabId:pool.slot.tabId},func:async(prompt,key,reused,images)=>await window.wechatWebReply(prompt,key,reused,images),args:[job.prompt,pool.slot.key,pool.reused,job.images||[]]});
  const result=reply[0]?.result;if(!result)throw Error('empty');
  const tab=await chrome.tabs.get(pool.slot.tabId);if(!WechatPool.validUrl(tab.url))throw Error('ownership');
  pool.slot.turns+=1;pool.slot.url=tab.url;pool.slot.used=Date.now();pool.slot.failed=false;await store(pool.slots);
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(tab.url));
  const fingerprint=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
  const browser_meta={tab_id:tab.id,slot_id:pool.slot.slotId,turn:pool.slot.turns,reused:pool.reused,managed_tabs:pool.slots.length,conversation_fingerprint:fingerprint,queue_wait_ms:queueWait,web_reply_ms:Date.now()-replyStarted,bridge_total_ms:Date.now()-bridgeStarted};
  stage='complete';const sent=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result,browser_meta})});if(!sent.ok)throw Error('result rejected');
 }catch(e){
  if(pool){try{const t=await chrome.tabs.get(pool.slot.tabId);if(/outside_turn|user_editing|ownership_lost/.test(String(e.message))){pool.slots=pool.slots.filter(s=>s.tabId!==pool.slot.tabId);}else{pool.slot.url=t.url;pool.slot.failed=true;}await store(pool.slots);}catch{}}
  if(job)try{await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:stage})});}catch{}
 }finally{
  if(job)await chrome.storage.session.remove('bridgeLease');busy=false;
  // Keep owned tabs and their current conversations. Never close/create per reply.
 }
}
chrome.alarms.onAlarm.addListener(()=>{connectPush();pump();});
let pushSocket=null;
async function connectPush(){
 if(pushSocket)return;const {token}=await chrome.storage.local.get('token');if(!token)return;
 const socket=new WebSocket('ws://127.0.0.1:18769/browser-bridge/events');pushSocket=socket;
 socket.onopen=()=>{socket.send(JSON.stringify({token}));pump();};
 socket.onmessage=e=>{try{if(JSON.parse(e.data).type==='ready')pump();}catch{}};
 socket.onclose=()=>{pushSocket=null;setTimeout(connectPush,2000);};
 socket.onerror=()=>socket.close();
}
connectPush();


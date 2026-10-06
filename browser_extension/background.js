// Fixed owned tab pool. Same contact keeps its temporary chat; no prior user tabs.
importScripts('pool_core.js');
const base='http://127.0.0.1:18769/browser-bridge';
const rootUrl='https://chatgpt.com/?temporary-chat=true';
let busy=false;
chrome.storage.local.set({bridgeWorkerVersion:4});
chrome.alarms.create('poll',{periodInMinutes:.5});
chrome.runtime.onInstalled.addListener(()=>chrome.alarms.create('poll',{periodInMinutes:.5}));
chrome.action.onClicked.addListener(()=>chrome.runtime.openOptionsPage());
async function store(slots){await chrome.storage.session.set({bridgePool:slots});}
async function acquire(key){
 const {bridgePool=[]}=await chrome.storage.session.get('bridgePool');
 const slots=[];
 for(const s of bridgePool){try{const t=await chrome.tabs.get(s.tabId);if(t.url===s.url&&WechatPool.validUrl(t.url))slots.push(s);}catch{}}
 const choice=WechatPool.choose(slots,key,Date.now());let s=choice.slot;
 if(!s){const t=await chrome.tabs.create({url:rootUrl,active:false});s={tabId:t.id,slotId:[0,1,2].find(i=>!slots.some(x=>x.slotId===i)),turns:0};slots.push(s);}
 else if(choice.reset){
  try{const old=await chrome.scripting.executeScript({target:{tabId:s.tabId},func:()=>performance.timeOrigin});s.navigationOriginBefore=old[0]?.result;}catch{s.navigationOriginBefore=null;}
  await chrome.tabs.update(s.tabId,{url:rootUrl});
 }else{s.navigationOriginBefore=null;}
 if(choice.reset){s.turns=0;s.failed=false;s.url=rootUrl;}
 s.key=key;s.used=Date.now();await store(slots);
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
chrome.alarms.onAlarm.addListener(async()=>{
 if(busy)return;
 const {token}=await chrome.storage.local.get('token');if(!token)return;
 const {bridgeLease}=await chrome.storage.session.get('bridgeLease');if(bridgeLease?.until>Date.now())return;
 await chrome.storage.local.set({bridgeWorkerVersion:4});
 busy=true;let job=null,pool=null,stage='queue';
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Version':'4','X-Wechat-Bridge-Build':'sticker-vision-v1'};
 try{
  const r=await fetch(base+'/next',{headers});if(!r.ok)throw Error('bridge');
  job=(await r.json()).job;if(!job)return;
  await chrome.storage.session.set({bridgeLease:{id:job.id,until:Date.now()+240000}});
  pool=await acquire(job.conversation_key||'isolated:'+job.id);stage='load';await loadTab(pool.slot.tabId,pool.slot.url,pool.slot.navigationOriginBefore);
  stage='setup';await chrome.scripting.executeScript({target:{tabId:pool.slot.tabId},files:['web_chat.js']});
  stage='reply';
  const reply=await chrome.scripting.executeScript({target:{tabId:pool.slot.tabId},func:async(prompt,key,reused,images)=>await window.wechatWebReply(prompt,key,reused,images),args:[job.prompt,pool.slot.key,pool.reused,job.images||[]]});
  const result=reply[0]?.result;if(!result)throw Error('empty');
  const tab=await chrome.tabs.get(pool.slot.tabId);if(!WechatPool.validUrl(tab.url))throw Error('ownership');
  pool.slot.turns+=1;pool.slot.url=tab.url;pool.slot.used=Date.now();pool.slot.failed=false;await store(pool.slots);
  const digest=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(tab.url));
  const fingerprint=Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('');
  const browser_meta={tab_id:tab.id,slot_id:pool.slot.slotId,turn:pool.slot.turns,reused:pool.reused,managed_tabs:pool.slots.length,conversation_fingerprint:fingerprint};
  stage='complete';const sent=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result,browser_meta})});if(!sent.ok)throw Error('result rejected');
 }catch(e){
  if(pool){try{const t=await chrome.tabs.get(pool.slot.tabId);if(/outside_turn|user_editing|ownership_lost/.test(String(e.message))){pool.slots=pool.slots.filter(s=>s.tabId!==pool.slot.tabId);}else{pool.slot.url=t.url;pool.slot.failed=true;}await store(pool.slots);}catch{}}
  if(job)try{await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:stage})});}catch{}
 }finally{
  if(job)await chrome.storage.session.remove('bridgeLease');busy=false;
  // Keep owned tabs and their current conversations. Never close/create per reply.
 }
});

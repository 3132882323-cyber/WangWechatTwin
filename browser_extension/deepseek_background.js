// Optional, isolated DeepSeek draft channel. No hidden API calls or quota bypass.
let deepseekBusy=false,deepseekSocket=null;
async function deepseekPump(){
 if(deepseekBusy)return;
 const {token}=await chrome.storage.local.get('token');if(!token)return;
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':'deepseek'};
 const base='http://127.0.0.1:18769/browser-bridge';let job=null;deepseekBusy=true;
 try{
  const bridgeStarted=Date.now();
  const next=await fetch(base+'/next',{headers});if(!next.ok)return;job=(await next.json()).job;if(!job)return;
  if(job.images?.length)throw Error('deepseek_visual_not_verified');
  const key=job.conversation_key;
  const saved=await chrome.storage.local.get('deepseekConversations');
  const conversations=saved.deepseekConversations||{};
  const known=conversations[key];
  const url=known?.url?.startsWith('https://chat.deepseek.com/a/chat/s/')?known.url:'https://chat.deepseek.com/';
  const session=await chrome.storage.session.get('deepseekSlots');let slots=session.deepseekSlots||[],tab;
  if(!slots.length){
   const open=await chrome.tabs.query({url:'https://chat.deepseek.com/*'});
   for(const candidate of open){const entry=Object.entries(conversations).find(([key,value])=>value.url===candidate.url);if(entry)slots.push({id:candidate.id,key:entry[0],used:entry[1].used||0});}
  }
  for(const slot of slots){if(slot.key===key){try{tab=await chrome.tabs.get(slot.id);}catch{}break;}}
  if(!tab){
   for(const slot of [...slots].sort((a,b)=>a.used-b.used)){try{const candidate=await chrome.tabs.get(slot.id);if(candidate.url?.startsWith('https://chat.deepseek.com/')){tab=candidate;break;}}catch{}}
  }
  if(!tab)tab=await chrome.tabs.create({url,active:false});
  else if(tab.url!==url){
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
  slots=slots.filter(slot=>slot.id!==tab.id);slots.push({id:tab.id,key,used:Date.now()});
  await chrome.storage.session.set({deepseekSlots:slots});
  const reuse=url!=='https://chat.deepseek.com/';
  const deadline=Date.now()+30000;let ready=false;
  while(Date.now()<deadline){
   try{const state=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>({url:location.href,ready:document.readyState,editor:!!document.querySelector('textarea[placeholder="给 DeepSeek 发送消息 "]')})});
    const value=state[0]?.result;if(value?.url===url&&value.ready!=='loading'&&value.editor){ready=true;break;}
   }catch{}
   await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(!ready)throw Error('deepseek_login_or_load_required');
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_web.js']});
  const started=Date.now();const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(prompt,key,reuse)=>await window.wechatDeepseekDraft(prompt,key,reuse),args:[job.prompt,key,reuse]});
  const reply=result[0]?.result;if(!reply)throw Error('deepseek_empty_result');
  if(job.contact_name){try{await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_title.js']});await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(name)=>await window.wechatDeepseekRename(name),args:[job.contact_name]});}catch(error){console.warn('DeepSeek contact title pending');}}
  const finished=await chrome.tabs.get(tab.id);
  if(finished.url?.startsWith('https://chat.deepseek.com/a/chat/s/')){conversations[key]={url:finished.url,used:Date.now()};await chrome.storage.local.set({deepseekConversations:conversations});}
  const accepted=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:reply,browser_meta:{tab_id:tab.id,queue_wait_ms:Math.max(0,bridgeStarted-job.created*1000),bridge_total_ms:Date.now()-bridgeStarted,web_reply_ms:Date.now()-started,reused:reuse,managed_tabs:slots.length}})});
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

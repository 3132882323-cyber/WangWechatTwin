// Optional, isolated DeepSeek draft channel. No hidden API calls or quota bypass.
let deepseekBusy=false,deepseekSocket=null;
async function deepseekPump(){
 if(deepseekBusy)return;
 const {token}=await chrome.storage.local.get('token');if(!token)return;
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':'deepseek'};
 const base='http://127.0.0.1:18769/browser-bridge';let job=null;deepseekBusy=true;
 try{
  const next=await fetch(base+'/next',{headers});if(!next.ok)return;job=(await next.json()).job;if(!job)return;
  if(job.images?.length)throw Error('deepseek_visual_not_verified');
  const key=job.conversation_key;
  const saved=await chrome.storage.local.get('deepseekConversations');
  const conversations=saved.deepseekConversations||{};
  const known=conversations[key];
  const url=known?.url?.startsWith('https://chat.deepseek.com/a/chat/s/')?known.url:'https://chat.deepseek.com/';
  const session=await chrome.storage.session.get('deepseekSlots');let slots=session.deepseekSlots||[],tab;
  for(const slot of slots){if(slot.key===key){try{tab=await chrome.tabs.get(slot.id);}catch{}break;}}
  if(!tab && slots.length>=10){slots.sort((a,b)=>a.used-b.used);const old=slots.shift();try{await chrome.tabs.remove(old.id);}catch{}}
  if(!tab)tab=await chrome.tabs.create({url,active:false});
  else if(tab.url!==url)tab=await chrome.tabs.update(tab.id,{url});
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
  await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:reply,browser_meta:{tab_id:tab.id,web_reply_ms:Date.now()-started,reused:reuse,managed_tabs:slots.length}})});
 }catch(error){
  console.warn('DeepSeekBridge failure: '+String(error?.message||'unknown').slice(0,120));
  if(job)await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:'reply'})}).catch(()=>{});
 }finally{deepseekBusy=false;}
}
async function connectDeepseek(){
 if(deepseekSocket)return;const {token}=await chrome.storage.local.get('token');if(!token)return;
 const socket=new WebSocket('ws://127.0.0.1:18769/browser-bridge/events');deepseekSocket=socket;
 socket.onopen=()=>{socket.send(JSON.stringify({token,provider:'deepseek'}));deepseekPump();};
 socket.onmessage=()=>deepseekPump();socket.onclose=()=>{deepseekSocket=null;};socket.onerror=()=>{try{socket.close();}catch{}};
}
chrome.alarms.onAlarm.addListener(()=>{connectDeepseek();deepseekPump();});
connectDeepseek();

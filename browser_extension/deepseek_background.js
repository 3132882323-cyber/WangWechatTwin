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
  const stored=await chrome.storage.session.get('deepseekOwnedTab');let tab;
  if(stored.deepseekOwnedTab){try{tab=await chrome.tabs.get(stored.deepseekOwnedTab);}catch{}}
  if(tab?.url?.startsWith('https://chat.deepseek.com/'))tab=await chrome.tabs.update(tab.id,{url:'https://chat.deepseek.com/'});
  else tab=await chrome.tabs.create({url:'https://chat.deepseek.com/',active:false});
  await chrome.storage.session.set({deepseekOwnedTab:tab.id});
  const deadline=Date.now()+30000;let ready=false;
  while(Date.now()<deadline){
   try{const state=await chrome.scripting.executeScript({target:{tabId:tab.id},func:()=>({url:location.href,ready:document.readyState,editor:!!document.querySelector('textarea[placeholder="给 DeepSeek 发送消息 "]')})});
    const value=state[0]?.result;if(value?.url==='https://chat.deepseek.com/'&&value.ready!=='loading'&&value.editor){ready=true;break;}
   }catch{}
   await new Promise(resolve=>setTimeout(resolve,400));
  }
  if(!ready)throw Error('deepseek_login_or_load_required');
  await chrome.scripting.executeScript({target:{tabId:tab.id},files:['deepseek_web.js']});
  const started=Date.now();const result=await chrome.scripting.executeScript({target:{tabId:tab.id},func:async(prompt,key)=>await window.wechatDeepseekDraft(prompt,key),args:[job.prompt,job.conversation_key]});
  const reply=result[0]?.result;if(!reply)throw Error('deepseek_empty_result');
  await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result:reply,browser_meta:{tab_id:tab.id,web_reply_ms:Date.now()-started}})});
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

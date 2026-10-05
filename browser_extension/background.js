// Ordinary visible Chat UI only. No session tokens, private API or existing chats.
let busy=false;
const base='http://127.0.0.1:18769/browser-bridge';
chrome.runtime.onInstalled.addListener(()=>chrome.alarms.create('poll',{periodInMinutes:.5}));
chrome.action.onClicked.addListener(()=>chrome.runtime.openOptionsPage());
chrome.alarms.onAlarm.addListener(async()=>{
 if(busy)return;
 const {token}=await chrome.storage.local.get('token');if(!token)return;
 busy=true;let job=null,createdTab=null,stage='queue';
 const headers={Authorization:'Bearer '+token,'Content-Type':'application/json'};
 try{
  const r=await fetch(base+'/next',{headers});if(!r.ok)throw Error('bridge');
  job=(await r.json()).job;if(!job)return;
  stage='load';createdTab=await chrome.tabs.create({url:'https://chatgpt.com/?temporary-chat=true',active:false});
  await new Promise((resolve,reject)=>{let n=0;const timer=setInterval(async()=>{try{const t=await chrome.tabs.get(createdTab.id);if(t.status==='complete'){clearInterval(timer);resolve();}else if(++n>60){clearInterval(timer);reject(Error('load'));}}catch(e){clearInterval(timer);reject(e);}},500);});
  stage='setup';const results=await chrome.scripting.executeScript({target:{tabId:createdTab.id},files:['web_chat.js']});
  stage='reply';
  const reply=await chrome.scripting.executeScript({target:{tabId:createdTab.id},func:async(prompt)=>await window.wechatWebReply(prompt),args:[job.prompt]});
  const result=reply[0]?.result;if(!result)throw Error('empty');
  stage='complete';const sent=await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,result})});if(!sent.ok)throw Error('result rejected');
 }catch(e){if(job){try{await fetch(base+'/result',{method:'POST',headers,body:JSON.stringify({id:job.id,error:stage})});}catch{}}}
 finally{if(createdTab)try{await chrome.tabs.remove(createdTab.id);}catch{}busy=false;}
});

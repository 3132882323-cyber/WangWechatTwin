// This extension-owned active tab gives the native window a unique stable title.
// It has no account access, network requests, or model generation behavior.
(function(){
 const validTitle=/^WeChatTwin Background [0-9a-f]{32}$/;
 let issuedTitle='',ownTab=null;
 chrome.tabs.getCurrent().then(async tab=>{
  ownTab=tab;
  try{
   const saved=await chrome.storage.local.get(['bridgeBackgroundWindow','token']);
   const context=saved.bridgeBackgroundWindow;
   if(!context||tab.id!==context.hostTabId||tab.windowId!==context.windowId||!saved.token)return;
   const response=await fetch('http://127.0.0.1:18769/browser-bridge/window/register',{
    method:'POST',headers:{Authorization:'Bearer '+saved.token,'Content-Type':'application/json'},
    body:JSON.stringify({window_id:tab.windowId,host_tab_id:tab.id})});
   const registered=await response.json();
   if(registered.ok&&registered.window_id===tab.windowId&&registered.host_tab_id===tab.id&&validTitle.test(registered.title||'')){
    issuedTitle=registered.title;document.title=issuedTitle;
    document.getElementById('restore-status').textContent='这是原后台控制页，已完成本机登记。';
   }
  }catch{}
 });
 chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(message?.type!=='wechat-background-host-title')return;
  if(sender.id!==chrome.runtime.id||!validTitle.test(message.title||''))return;
  if(!ownTab||ownTab.id!==message.host_tab_id||ownTab.windowId!==message.window_id)return;
  chrome.tabs.getCurrent().then(tab=>{
   if(tab?.id!==message.host_tab_id||tab.windowId!==message.window_id){return;}
   issuedTitle=message.title;document.title=issuedTitle;
   respond({ok:true,window_id:tab.windowId,host_tab_id:tab.id,title:document.title});
  },()=>respond({ok:false}));
  return true;
 });
 const restore=document.getElementById('restore-host'),status=document.getElementById('restore-status');
 restore.addEventListener('click',async()=>{
  restore.disabled=true;status.textContent='正在恢复已登记的控制页…';
  try{
   const saved=await chrome.storage.local.get('bridgeBackgroundWindow'),context=saved.bridgeBackgroundWindow;
   if(!context||!Number.isInteger(context.hostTabId)||!Number.isInteger(context.windowId))throw Error('尚无后台窗口登记');
   const host=await chrome.tabs.get(context.hostTabId);
   if(host.windowId!==context.windowId)throw Error('原控制页不在已登记窗口，已停止操作');
   await chrome.tabs.update(host.id,{url:chrome.runtime.getURL('background_host.html'),active:true});
   status.textContent='原控制页已恢复，请等待连接器重新隐藏并接回模型。这个辅助页可以关闭。';
  }catch(error){status.textContent='未能恢复，请从本机审核台检查后台状态。';}
  finally{restore.disabled=false;}
 });
 const restoreModels=document.getElementById('restore-models');
 restoreModels.addEventListener('click',async()=>{
  restoreModels.disabled=true;status.textContent='正在一起恢复三路模型页…';
  try{
   const saved=await chrome.storage.local.get(['bridgeBackgroundWindow','deepseekOwnedTab','deepseekOwnedUrl','doubaoOwnedTab','doubaoOwnedUrl','bridgePool']);
   const context=saved.bridgeBackgroundWindow;
   if(!context||!Number.isInteger(context.windowId)||!Number.isInteger(context.hostTabId))throw Error('no_registration');
   const host=await chrome.tabs.get(context.hostTabId);if(host.windowId!==context.windowId)throw Error('changed_host');
   const originMatches=(url,origin)=>{try{return new URL(url).origin===origin;}catch{return false;}};
   for(const [provider,origin,home] of [['deepseek','https://chat.deepseek.com','https://chat.deepseek.com/'],['doubao','https://www.doubao.com','https://www.doubao.com/chat/']]){
    const url=saved[provider+'OwnedUrl']||home;if(!originMatches(url,origin))throw Error('invalid_saved_url');
    let tab;try{tab=await chrome.tabs.get(saved[provider+'OwnedTab']);}catch{}
    if(tab&&originMatches(tab.url,origin)){if(tab.windowId!==context.windowId)await chrome.tabs.move(tab.id,{windowId:context.windowId,index:-1});}
    else{tab=await chrome.tabs.create({windowId:context.windowId,url,active:false});}
    await chrome.tabs.update(tab.id,{autoDiscardable:false});
    await chrome.storage.local.set({[provider+'OwnedTab']:tab.id,[provider+'OwnedUrl']:url});
   }
   const pool=[];
   for(const slot of saved.bridgePool||[]){let tab;try{tab=await chrome.tabs.get(slot.tabId);}catch{}
    if(!tab||!originMatches(tab.url,'https://chatgpt.com')||!new URL(tab.url).searchParams.has('temporary-chat'))continue;
    if(tab.windowId!==context.windowId)await chrome.tabs.move(tab.id,{windowId:context.windowId,index:-1});pool.push(slot);
   }
   if(!pool.length){const url='https://chatgpt.com/?temporary-chat=true';const tab=await chrome.tabs.create({windowId:context.windowId,url,active:false});await chrome.tabs.update(tab.id,{autoDiscardable:false});pool.push({tabId:tab.id,slotId:0,url,turns:0,bootstrapIdle:true,used:Date.now()});}
   await chrome.storage.local.set({bridgePool:pool});
   await chrome.tabs.update(host.id,{url:chrome.runtime.getURL('background_host.html'),active:true});
   status.textContent='三路模型页已接回，请等待心跳与草稿验证。';
   if(ownTab&&ownTab.id!==host.id&&ownTab.windowId===context.windowId)await chrome.tabs.remove(ownTab.id);
  }catch{status.textContent='恢复尚未完成，请保留此页并查看本机审核台。';}
  finally{restoreModels.disabled=false;}
 });
 new MutationObserver(()=>{if(issuedTitle&&document.title!==issuedTitle)document.title=issuedTitle;}).observe(document.querySelector('title'),{childList:true,subtree:true,characterData:true});
})();

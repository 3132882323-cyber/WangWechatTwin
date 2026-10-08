// This extension-owned active tab gives the native window a unique stable title.
// It has no account access, network requests, or model generation behavior.
(function(){
 const validTitle=/^WeChatTwin Background [0-9a-f]{32}$/;
 let issuedTitle='';
 chrome.runtime.onMessage.addListener((message,sender,respond)=>{
  if(message?.type!=='wechat-background-host-title')return;
  if(sender.id!==chrome.runtime.id||!validTitle.test(message.title||''))return;
  chrome.tabs.getCurrent().then(tab=>{
   if(tab?.id!==message.host_tab_id||tab.windowId!==message.window_id){respond({ok:false});return;}
   issuedTitle=message.title;document.title=issuedTitle;
   respond({ok:true,window_id:tab.windowId,host_tab_id:tab.id,title:document.title});
  },()=>respond({ok:false}));
  return true;
 });
 new MutationObserver(()=>{if(issuedTitle&&document.title!==issuedTitle)document.title=issuedTitle;}).observe(document.querySelector('title'),{childList:true,subtree:true,characterData:true});
})();

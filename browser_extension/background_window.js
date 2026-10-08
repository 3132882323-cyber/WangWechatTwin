// A separate, same-profile Chrome window for proven bridge-owned model tabs.
// Chrome has no hidden window state. Only the authenticated native backend can
// confirm hiding; minimized is used solely while the empty host is created.
(function(root){
 const base='http://127.0.0.1:18769/browser-bridge/window';
 const stateKey='bridgeBackgroundWindow',pendingKey='bridgeBackgroundPendingTabs';
 const titlePattern=/^WeChatTwin Background [0-9a-f]{32}$/;
 const home={deepseek:'https://chat.deepseek.com/',doubao:'https://www.doubao.com/chat/',chatgpt:'https://chatgpt.com/?temporary-chat=true'};
 let serial=Promise.resolve(),ensureFlight=null,cached=null,activationTimer=null;
 const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const enqueue=task=>{const next=serial.then(task,task);serial=next.catch(()=>{});return next;};
 const hostUrl=()=>chrome.runtime.getURL('background_host.html');
 const id=value=>Number.isInteger(value)&&value>=0;
 function providerUrl(provider,url){
  if(typeof url!=='string')return false;
  try{
   const parsed=new URL(url);
   if(provider==='deepseek')return parsed.origin==='https://chat.deepseek.com'&&(parsed.pathname==='/'||/^\/a\/chat\/s\/[^/?#]+$/.test(parsed.pathname))&&!parsed.search&&!parsed.hash;
   if(provider==='doubao')return parsed.origin==='https://www.doubao.com'&&/^\/chat(?:\/(?:\d+|local_\d+)?)?$/.test(parsed.pathname)&&!parsed.search&&!parsed.hash;
   return provider==='chatgpt'&&parsed.origin==='https://chatgpt.com'&&parsed.searchParams.get('temporary-chat')==='true';
  }catch{return false;}
 }
 function urlProvider(url){return Object.keys(home).find(provider=>providerUrl(provider,url));}
 function providerOrigin(provider,url){
  if(typeof url!=='string'||!home[provider])return false;
  try{const value=new URL(url);return value.origin===new URL(home[provider]).origin&&!value.username&&!value.password;}catch{return false;}
 }
 async function readOwnership(){
  // The explicitly authorized migration must claim an orphaned GPT ID before
  // the window is inspected; it never searches model pages by their URL.
  if(typeof root.migrateChatgptPool==='function')await root.migrateChatgptPool();
  const [local,session]=await Promise.all([
   chrome.storage.local.get([stateKey,'token','bridgePool','deepseekOwnedTab','deepseekOwnedUrl','deepseekConversations','doubaoOwnedTab','doubaoOwnedUrl','doubaoConversations']),
   chrome.storage.session.get(['bridgePool',pendingKey])
  ]);
  const pool=local.bridgePool!==undefined?local.bridgePool:(session.bridgePool||[]),pending=session[pendingKey]||[];
  if(local.bridgePool===undefined&&session.bridgePool!==undefined)await chrome.storage.local.set({bridgePool:pool});
  if(!Array.isArray(pool)||pool.length>3||!Array.isArray(pending))throw Error('background_window_invalid_ownership');
  const claims=[];
  for(const provider of ['deepseek','doubao']){
   const tabId=local[provider+'OwnedTab'],url=local[provider+'OwnedUrl'];
   // The saved ID plus an owned provider URL establishes the tab's origin.
   // Provider SPA navigation may precede writing the latest conversation URL.
   if(id(tabId)&&providerOrigin(provider,url))claims.push({tabId,provider,url});
  }
  for(const slot of pool){
   if(!slot||!id(slot.tabId)||!providerUrl('chatgpt',slot.url))throw Error('background_window_invalid_ownership');
   claims.push({tabId:slot.tabId,provider:'chatgpt',url:slot.url,exact:true});
  }
  let retained=[];
  for(const item of pending){
   if(!item||!id(item.tabId)||!id(item.windowId)||!providerUrl(item.provider,item.url))throw Error('background_window_invalid_ownership');
   const stored=claims.find(claim=>claim.tabId===item.tabId);
   if(stored){if(stored.provider!==item.provider)throw Error('background_window_invalid_ownership');stored.creation=item;}
   else claims.push({...item,created:true,creation:item});
   retained.push(item);
  }
  if(new Set(claims.map(claim=>claim.tabId)).size!==claims.length)throw Error('background_window_invalid_ownership');
  if(Object.keys(home).some(provider=>claims.filter(claim=>claim.provider===provider).length>(provider==='chatgpt'?3:1)))throw Error('background_window_invalid_ownership');
  const tabs=[],missing=[];
  for(const claim of claims){
   let tab;try{tab=await chrome.tabs.get(claim.tabId);}catch{missing.push(claim.tabId);continue;}
   const sameOrigin=providerOrigin(claim.provider,tab.url)&&new URL(tab.url).origin===new URL(claim.url).origin;
   const settled=sameOrigin&&(claim.provider!=='chatgpt'||providerUrl('chatgpt',tab.url))&&(!claim.exact||tab.url===claim.url);
   // tabs.create may return before Chrome commits its first navigation. Only
   // our recorded newly-created ID in its exact hidden window may be pending.
   const initialPending=claim.creation&&tab.windowId===claim.creation.windowId&&(!tab.url||tab.url==='about:blank')&&tab.pendingUrl===claim.creation.url;
   if(!settled&&!initialPending){missing.push(claim.tabId);continue;}
   // Consume pending proof only after provider storage and live URL agree.
   // An explicit future release then cannot resurrect the previous tab.
   if(settled&&claim.creation&&!claim.created)retained=retained.filter(item=>item.tabId!==claim.tabId);
   tabs.push({claim,tab});
  }
  if(retained.length!==pending.length){await chrome.storage.session.set({[pendingKey]:retained});session[pendingKey]=retained;}
  return {local,session,claims,tabs,missing};
 }
 async function health(values){await chrome.storage.local.set({bridgeBackgroundWindowHealth:{at:Date.now(),...values}});}
 async function request(path,token,body){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
  try{
   const response=await fetch(base+path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});
   if(!response.ok)throw Error('background_window_bridge_unavailable');
   return await response.json();
  }catch(error){if(error?.message==='background_window_bridge_unavailable')throw error;throw Error('background_window_bridge_unavailable');}
  finally{clearTimeout(timer);}
 }
 async function validateContents(windowId,hostTabId,ownership){
  const window=await chrome.windows.get(windowId);
  if(window.type!=='normal'||window.incognito)throw Error('background_window_wrong_type');
  const tabs=await chrome.tabs.query({windowId});
  const host=tabs.find(tab=>tab.id===hostTabId&&tab.url===hostUrl());
  const owned=new Set(ownership.tabs.map(item=>item.tab.id));
  if(!host||tabs.some(tab=>tab.id!==hostTabId&&!owned.has(tab.id)))throw Error('background_window_mixed_tabs');
  return host;
 }
 async function findHost(ownership){
  const saved=ownership.local[stateKey];
  if(saved&&id(saved.hostTabId)){
   let tab;try{tab=await chrome.tabs.get(saved.hostTabId);}catch{}
   if(tab?.url===hostUrl()){
    await validateContents(tab.windowId,tab.id,ownership);
    return {windowId:tab.windowId,hostTabId:tab.id};
   }
  }
  // Recover only this extension's exact controller URL. Model pages are never
  // adopted by URL search here, and unrelated tabs are never moved or hidden.
  const hosts=(await chrome.tabs.query({})).filter(tab=>tab.url===hostUrl());
  if(hosts.length>1)throw Error('background_window_multiple_hosts');
  if(hosts.length===1){
   const tab=hosts[0];await validateContents(tab.windowId,tab.id,ownership);
   return {windowId:tab.windowId,hostTabId:tab.id};
  }
  const window=await chrome.windows.create({url:hostUrl(),type:'normal',focused:false,state:'minimized'});
  if(!id(window?.id))throw Error('background_window_create_failed');
  const tabs=await chrome.tabs.query({windowId:window.id});
  const host=tabs.find(tab=>tab.url===hostUrl());
  if(!host)throw Error('background_window_host_unavailable');
  const context={windowId:window.id,hostTabId:host.id};
  // Persist before native registration so a retry reuses this one host window.
  await chrome.storage.local.set({[stateKey]:context});
  await validateContents(context.windowId,context.hostTabId,ownership);
  return context;
 }
 async function setTitle(context,title){
  const deadline=Date.now()+4000;
  while(Date.now()<deadline){
   try{
    let timer;
    const pending=chrome.runtime.sendMessage({type:'wechat-background-host-title',window_id:context.windowId,host_tab_id:context.hostTabId,title});
    const reply=await Promise.race([pending,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),500);})]).finally(()=>clearTimeout(timer));
    if(reply?.ok===true&&reply.window_id===context.windowId&&reply.host_tab_id===context.hostTabId&&reply.title===title)return;
   }catch{}
   await pause(100);
  }
  throw Error('background_window_host_unavailable');
 }
 function nativeError(reply){
  const error=String(reply?.error||'');
  return /^(?:background_window_[a-z_]+|owned_window_missing|owned_window_title_mismatch)$/.test(error)?error:'background_window_not_hidden';
 }
 async function hide(context,token){
  // Activating a tab does not focus the window. The stable controller title is
  // the only title the backend may use to identify and hide a native HWND.
  await chrome.tabs.update(context.hostTabId,{active:true});
  await setTitle(context,context.title);
  const body={window_id:context.windowId,host_tab_id:context.hostTabId,title:context.title};
  // Chromium may update its native caption just after the controller replies.
  // Retry only those two caption timing failures, with the identical identity.
  for(let attempt=0;attempt<4;attempt++){
   const reply=await request('/hide',token,body);
   if(reply?.ok===true&&reply.hidden===true&&reply.visible===false&&reply.verified===true&&reply.window_id===context.windowId&&reply.host_tab_id===context.hostTabId&&reply.title===context.title)return;
   const transient=reply?.ok===false&&['owned_window_missing','owned_window_title_mismatch'].includes(reply.error);
   if(!transient||attempt===3)throw Error(nativeError(reply));
   await pause(100);
  }
 }
 async function prepare(){
  const ownership=await readOwnership(),token=ownership.local.token;
  if(!token)throw Error('background_window_token_required');
  const context=await findHost(ownership);
  const reply=await request('/register',token,{window_id:context.windowId,host_tab_id:context.hostTabId});
  if(reply?.ok!==true||reply.window_id!==context.windowId||reply.host_tab_id!==context.hostTabId||!titlePattern.test(reply.title||''))throw Error('background_window_registration_rejected');
  // A controlled reveal pauses work before changing the user's active tab.
  if(reply.manual_reveal===true)throw Error('background_window_manual_reveal');
  context.title=reply.title;
  await chrome.storage.local.set({[stateKey]:context});cached=context;
  await validateContents(context.windowId,context.hostTabId,ownership);
  // Verify the empty/already-owned window is truly hidden before moving pages.
  await hide(context,token);
  for(const {tab} of ownership.tabs){
   if(tab.windowId===context.windowId)continue;
   const source=await chrome.windows.get(tab.windowId);
   if(source.type!=='normal')throw Error('background_window_source_unsupported');
   // Recheck the saved claim after native registration, before changing tabs.
   const current=await readOwnership();
   if(!current.tabs.some(item=>item.tab.id===tab.id))throw Error('background_window_ownership_changed');
   await chrome.tabs.move(tab.id,{windowId:context.windowId,index:-1});
  }
  const finalOwnership=await readOwnership();
  await validateContents(context.windowId,context.hostTabId,finalOwnership);
  await hide(context,token);
  await health({ready:true,hidden:true,visible:false,verified:true,window_id:context.windowId,host_tab_id:context.hostTabId,managed_tabs:finalOwnership.tabs.length,missing_tab_ids:finalOwnership.missing});
  return {context,ownership:finalOwnership};
 }
 async function guarded(task){
  try{return await task();}
  catch(error){await health({ready:false,hidden:false,visible:null,verified:false,error:String(error?.message||'background_window_failed')}).catch(()=>{});throw error;}
 }
 function ensure(){
  if(!ensureFlight)ensureFlight=enqueue(()=>guarded(async()=>{const {context}=await prepare();return context;})).finally(()=>{ensureFlight=null;});
  return ensureFlight;
 }
 function ensureOwnedTab(tabId,provider){
  return enqueue(()=>guarded(async()=>{
   const before=await readOwnership();
   if(!before.tabs.some(item=>item.tab.id===tabId&&item.claim.provider===provider))throw Error('background_window_tab_unproven');
   const {context,ownership}=await prepare();
   const item=ownership.tabs.find(item=>item.tab.id===tabId&&item.claim.provider===provider);
   if(!item||item.tab.windowId!==context.windowId)throw Error('background_window_ownership_changed');
   return item.tab;
  }));
 }
 function createModelTab(properties){
  return enqueue(()=>guarded(async()=>{
   const provider=urlProvider(properties?.url);
   if(!provider||properties.url!==home[provider])throw Error('background_window_creation_url_rejected');
   const {context,ownership}=await prepare();
   const same=ownership.claims.filter(claim=>claim.provider===provider);
   if((provider==='chatgpt'&&same.length>=3)||(provider!=='chatgpt'&&(same.length||ownership.local[provider+'OwnedUrl']||Object.keys(ownership.local[provider+'Conversations']||{}).length)))throw Error('background_window_model_capacity');
   const tab=await chrome.tabs.create({url:properties.url,windowId:context.windowId,active:false});
   const pending=ownership.session[pendingKey]||[];
   await chrome.storage.session.set({[pendingKey]:[...pending,{tabId:tab.id,provider,url:properties.url,windowId:context.windowId}]});
   // A native hide failure keeps the created page recorded; never create a
   // replacement or expose it in the ordinary current/focused Chrome window.
   await validateContents(context.windowId,context.hostTabId,await readOwnership());
   await hide(context,ownership.local.token);
   // Provider code needs a committed URL before it can navigate or inspect the
   // page. Wait on the same ID; a slow/closed page never causes a replacement.
   const deadline=Date.now()+15000;
   while(Date.now()<deadline){
    let current;try{current=await chrome.tabs.get(tab.id);}catch{throw Error('background_window_created_tab_unavailable');}
    if(current.windowId!==context.windowId)throw Error('background_window_ownership_changed');
    if(providerOrigin(provider,current.url)&&current.url!=='about:blank')return current;
    if(current.url&&current.url!=='about:blank')throw Error('background_window_ownership_changed');
    await pause(100);
   }
   throw Error('background_window_created_tab_timeout');
  }));
 }
 function startup(){return ensure().catch(()=>console.warn('Bridge background window requires attention'));}
 const api={ensure,ensureOwnedTab,createModelTab,startup};root.WechatBackgroundWindow=api;
 // Events only retry the window contract; they never create model pages.
 if(chrome.runtime.onStartup)chrome.runtime.onStartup.addListener(startup);
 if(chrome.runtime.onInstalled)chrome.runtime.onInstalled.addListener(startup);
 if(chrome.tabs.onUpdated)chrome.tabs.onUpdated.addListener((tabId,change)=>{if(cached?.hostTabId===tabId&&change.status==='complete')startup();});
 if(chrome.tabs.onAttached)chrome.tabs.onAttached.addListener((tabId,info)=>{if(cached&&info.newWindowId===cached.windowId)startup();});
 if(chrome.tabs.onActivated)chrome.tabs.onActivated.addListener(info=>{
  if(!cached||info.windowId!==cached.windowId||info.tabId===cached.hostTabId)return;
  clearTimeout(activationTimer);
  // Chrome's tab search can activate a tab in another window. Debounce those
  // events and recheck manual_reveal before selecting/hiding the controller.
  activationTimer=setTimeout(()=>{activationTimer=null;startup();},100);
 });
 if(chrome.storage.onChanged)chrome.storage.onChanged.addListener((changes,area)=>{if(area==='local'&&changes.token?.newValue)startup();});
})(globalThis);

// A separate, same-profile Chrome window for proven bridge-owned model tabs.
// Chrome has no hidden window state. Windows uses authenticated native hiding;
// macOS explicitly uses a minimized dedicated window, verified by this extension.
(function(root){
 const base='http://127.0.0.1:18769/browser-bridge/window';
 const stateKey='bridgeBackgroundWindow',pendingKey='bridgeBackgroundPendingTabs',recoveryKey='bridgeBackgroundRecoveryApplied',renderKey='bridgeBackgroundRenderingLease',relocationKey='bridgeBackgroundRecoveryRelocation';
 const titlePattern=/^WeChatTwin Background [0-9a-f]{32}$/;
 const recoveryPattern=/^[0-9a-f]{32}$/;
 const home={deepseek:'https://chat.deepseek.com/',doubao:'https://www.doubao.com/chat/',chatgpt:'https://chatgpt.com/?temporary-chat=true'};
 let serial=Promise.resolve(),ensureFlight=null,cached=null,activationTimer=null,renderLease=null,renderTimer=null,renderWatch=null;
 const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
 const enqueue=task=>{const next=serial.then(task,task);serial=next.catch(()=>{});return next;};
 const hostUrl=()=> 'http://127.0.0.1:18769/browser-window/host';
 const id=value=>Number.isInteger(value)&&value>=0;
 function providerUrl(provider,url){
  if(typeof url!=='string')return false;
  try{
   const parsed=new URL(url);
   if(parsed.username||parsed.password)return false;
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
 function retiredHostUrl(url){
  // A legacy URL is only compared on the saved host ID. It is never visited.
  try{const value=new URL(url);return value.protocol==='chrome-extension:'&&value.hostname===chrome.runtime.id&&value.pathname==='/background_host.html'&&!value.search&&!value.hash;}catch{return false;}
 }
 function repairableHostUrl(url){return ['chrome://newtab/','about:blank'].includes(url)||retiredHostUrl(url);}
 async function readOwnership(){
  // The explicitly authorized migration must claim an orphaned GPT ID before
  // the window is inspected; it never searches model pages by their URL.
  const modelErrors={};
  if(typeof root.migrateChatgptPool==='function'){
   try{await root.migrateChatgptPool();}catch(error){modelErrors.chatgpt=/^chatgpt_[a-z_]+$/.test(error?.message||'')?error.message:'chatgpt_migration_failed';}
  }
  const [local,session]=await Promise.all([
   chrome.storage.local.get([stateKey,pendingKey,recoveryKey,renderKey,relocationKey,'token','bridgePool','deepseekOwnedTab','deepseekOwnedUrl','deepseekConversations','doubaoOwnedTab','doubaoOwnedUrl','doubaoConversations']),
   chrome.storage.session.get(['bridgePool',pendingKey])
  ]);
  const pool=local.bridgePool!==undefined?local.bridgePool:(session.bridgePool||[]),pending=local[pendingKey]!==undefined?local[pendingKey]:(session[pendingKey]||[]);
  if(local.bridgePool===undefined&&session.bridgePool!==undefined)await chrome.storage.local.set({bridgePool:pool});
  if(local[pendingKey]===undefined)await chrome.storage.local.set({[pendingKey]:pending});
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
   const validPendingUrl=item?.provider==='chatgpt'?providerUrl('chatgpt',item.url):providerOrigin(item?.provider,item?.url);
   if(!item||!id(item.tabId)||!id(item.windowId)||!validPendingUrl)throw Error('background_window_invalid_ownership');
   const stored=claims.find(claim=>claim.tabId===item.tabId);
   if(stored){if(stored.provider!==item.provider)throw Error('background_window_invalid_ownership');stored.creation=item;}
   else claims.push({...item,created:true,creation:item});
   retained.push(item);
  }
  if(new Set(claims.map(claim=>claim.tabId)).size!==claims.length)throw Error('background_window_invalid_ownership');
  if(Object.keys(home).some(provider=>claims.filter(claim=>claim.provider===provider).length>(provider==='chatgpt'?3:1)))throw Error('background_window_invalid_ownership');
  const tabs=[],contained=[],missing=[],absent=[];
  for(const claim of claims){
   let tab;try{tab=await chrome.tabs.get(claim.tabId);}catch{missing.push(claim.tabId);absent.push(claim);continue;}
   const sameOrigin=providerOrigin(claim.provider,tab.url)&&new URL(tab.url).origin===new URL(claim.url).origin;
   const settled=sameOrigin&&(claim.provider!=='chatgpt'||providerUrl('chatgpt',tab.url))&&(!claim.exact||tab.url===claim.url);
   // tabs.create may return before Chrome commits its first navigation. Only
   // our recorded newly-created ID in its exact hidden window may be pending.
   const initialPending=claim.creation&&tab.windowId===claim.creation.windowId&&(!tab.url||tab.url==='about:blank')&&tab.pendingUrl===claim.creation.url;
   if(!settled&&!initialPending){
    missing.push(claim.tabId);
    // A GPT login/changed page can stay in its previously owned container, but
    // it cannot be adopted or moved back from an ordinary user window.
    const ownWindow=tab.windowId===local[stateKey]?.windowId||tab.windowId===claim.creation?.windowId;
    if(sameOrigin&&claim.provider==='chatgpt'&&ownWindow){contained.push({claim,tab});modelErrors.chatgpt='chatgpt_owned_url_changed';}
    continue;
   }
   // Consume pending proof only after provider storage and live URL agree.
   // An explicit future release then cannot resurrect the previous tab.
   if(settled&&claim.creation&&!claim.created)retained=retained.filter(item=>item.tabId!==claim.tabId);
   tabs.push({claim,tab});
  }
  if(retained.length!==pending.length)await chrome.storage.local.set({[pendingKey]:retained});
  // LOCAL survives an extension reload; SESSION is only a compatibility mirror.
  await chrome.storage.session.set({[pendingKey]:retained});
  local[pendingKey]=retained;session[pendingKey]=retained;
  return {local,session,pool,claims,tabs,contained,missing,absent,modelErrors};
 }
 async function health(values){
  await chrome.storage.local.set({bridgeBackgroundWindowHealth:{at:Date.now(),...values}});
  if(values.error){try{const {token}=await chrome.storage.local.get('token');if(token)await request('/diagnostic',token,{code:String(values.error)});}catch{}}
 }
 async function request(path,token,body){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);
  try{
   const response=await fetch(base+path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body),signal:controller.signal});
   if(!response.ok)throw Error('background_window_bridge_unavailable');
   return await response.json();
  }catch(error){if(error?.message==='background_window_bridge_unavailable')throw error;throw Error('background_window_bridge_unavailable');}
  finally{clearTimeout(timer);}
 }
 async function validateContents(windowId,hostTabId,ownership,expectedHostUrl=hostUrl()){
  const window=await chrome.windows.get(windowId);
  if(window.type!=='normal'||window.incognito)throw Error('background_window_wrong_type');
  const tabs=await chrome.tabs.query({windowId});
  const host=tabs.find(tab=>tab.id===hostTabId&&(tab.url||'')===expectedHostUrl);
  const owned=new Set([...ownership.tabs,...ownership.contained.filter(item=>item.tab.windowId===windowId)].map(item=>item.tab.id));
  if(!host||tabs.some(tab=>tab.id!==hostTabId&&!owned.has(tab.id)))throw Error('background_window_mixed_tabs');
  return host;
 }
 async function recoveryRegistration(context,token,expectedNonce){
  const reply=await request('/register',token,{window_id:context.windowId,host_tab_id:context.hostTabId});
  if(reply?.ok!==true||reply.window_id!==context.windowId||reply.host_tab_id!==context.hostTabId||!titlePattern.test(reply.title||''))throw Error('background_window_registration_rejected');
  if(reply.manual_reveal===true)throw Error('background_window_manual_reveal');
  if(windowMode(reply)==='extension_minimized')throw Error('background_window_mixed_tabs');
  const nonce=reply.recovery_requested?.nonce;
  if(nonce===undefined)throw Error('background_window_mixed_tabs');
  if(typeof nonce!=='string'||!recoveryPattern.test(nonce)||(expectedNonce&&nonce!==expectedNonce))throw Error('background_window_recovery_rejected');
  return {nonce,title:reply.title};
 }
 function retirementVerified(reply,plan){
  return reply?.ok===true&&reply.retired===true&&reply.visible===true&&reply.verified===true&&reply.manual_reveal===false&&reply.window_id===plan.sourceWindowId&&reply.host_tab_id===plan.hostTabId&&reply.nonce===plan.nonce;
 }
 async function retireForRecovery(plan,token){
  const reply=await request('/retire-for-recovery',token,{window_id:plan.sourceWindowId,host_tab_id:plan.hostTabId,nonce:plan.nonce});
  if(!retirementVerified(reply,plan))throw Error('background_window_retirement_rejected');
 }
 async function resumeRelocation(plan,ownership,retired=false){
  const saved=ownership.local[stateKey],token=ownership.local.token;
  if(!plan||!recoveryPattern.test(plan.nonce||'')||!id(plan.sourceWindowId)||!id(plan.hostTabId)||!titlePattern.test(plan.title||'')||!saved||saved.windowId!==plan.sourceWindowId||saved.hostTabId!==plan.hostTabId)throw Error('background_window_recovery_rejected');
  if(!retired){
   const approval=await recoveryRegistration({windowId:plan.sourceWindowId,hostTabId:plan.hostTabId},token,plan.nonce);
   if(approval.title!==plan.title)throw Error('background_window_registration_rejected');
   await retireForRecovery(plan,token);
  }
  // The native receipt proves the old, possibly mixed window is visible before
  // its controller leaves. A stored exact-ID intent also survives a worker
  // restart between windows.create and the atomic destination-state write.
  const current=await readOwnership(),intent=current.local[relocationKey];
  if(!intent||intent.nonce!==plan.nonce||intent.sourceWindowId!==plan.sourceWindowId||intent.hostTabId!==plan.hostTabId||intent.title!==plan.title||current.local[stateKey]?.windowId!==plan.sourceWindowId||current.local[stateKey]?.hostTabId!==plan.hostTabId)throw Error('background_window_ownership_changed');
  let host;try{host=await chrome.tabs.get(plan.hostTabId);}catch{throw Error('background_window_host_unavailable');}
  if(host.url!==hostUrl())throw Error('background_window_host_changed');
  let context;
  if(host.windowId===plan.sourceWindowId){
   const source=await chrome.windows.get(plan.sourceWindowId);
   if(source.type!=='normal'||source.incognito)throw Error('background_window_wrong_type');
   const window=await chrome.windows.create({tabId:plan.hostTabId,type:'normal',focused:false});
   if(!id(window?.id)||window.id===plan.sourceWindowId)throw Error('background_window_create_failed');
   const moved=await chrome.tabs.get(plan.hostTabId);
   if(moved.windowId!==window.id||moved.url!==hostUrl())throw Error('background_window_host_changed');
   context={windowId:window.id,hostTabId:plan.hostTabId};
  }else{
   // Never search for another controller by URL. Only the original persisted
   // host ID may complete an interrupted move, in a proven-only normal window.
   context={windowId:host.windowId,hostTabId:plan.hostTabId};
  }
  await validateContents(context.windowId,context.hostTabId,await readOwnership());
  // Persist before any new registration/hide or closed-model restoration.
  // Existing model IDs remain claimed and the normal prepare path moves only
  // those whose live provider URL still matches their saved ownership proof.
  await chrome.storage.local.set({[stateKey]:context,[relocationKey]:null});
  return context;
 }
 async function relocateMixedHost(context,ownership){
  const approval=await recoveryRegistration(context,ownership.local.token);
  if(ownership.local[recoveryKey]===approval.nonce)throw Error('background_window_recovery_rejected');
  const plan={nonce:approval.nonce,sourceWindowId:context.windowId,hostTabId:context.hostTabId,title:approval.title};
  const host=await chrome.tabs.get(context.hostTabId);
  if(host.windowId!==context.windowId||host.url!==hostUrl())throw Error('background_window_host_changed');
  await retireForRecovery(plan,ownership.local.token);
  await chrome.storage.local.set({[relocationKey]:plan});
  return resumeRelocation(plan,await readOwnership(),true);
 }
 async function findHost(ownership){
  if(ownership.local[relocationKey])return resumeRelocation(ownership.local[relocationKey],ownership);
  const saved=ownership.local[stateKey];
  if(saved&&id(saved.hostTabId)){
   let tab;try{tab=await chrome.tabs.get(saved.hostTabId);}catch{}
   if(tab?.url===hostUrl()){
    if(!id(saved.windowId)||tab.windowId!==saved.windowId)throw Error('background_window_host_changed');
    try{await validateContents(tab.windowId,tab.id,ownership);}
    catch(error){if(error?.message!=='background_window_mixed_tabs')throw error;return relocateMixedHost({windowId:tab.windowId,hostTabId:tab.id},ownership);}
    return {windowId:tab.windowId,hostTabId:tab.id};
   }
   if(tab){
    // Chrome may invalidate an extension page to newtab on extension reload.
    // Repair only the persisted host ID in its persisted, proven-only window.
    const initialPending=(!tab.url||tab.url==='about:blank')&&tab.pendingUrl===hostUrl();
    if((!repairableHostUrl(tab.url)&&!initialPending)||!id(saved.windowId)||tab.windowId!==saved.windowId)throw Error('background_window_host_changed');
    await validateContents(saved.windowId,saved.hostTabId,ownership,tab.url||'');
    return {windowId:saved.windowId,hostTabId:saved.hostTabId,restoreHost:true,restoreHostUrl:tab.url||''};
   }
  }
  // Public HTTP pages are never searched, adopted, moved or hidden. Only a
  // freshly created host ID or the persisted host ID proves our controller.
  const window=await chrome.windows.create({url:hostUrl(),type:'normal',focused:false,state:'minimized'});
  if(!id(window?.id))throw Error('background_window_create_failed');
  const tabs=await chrome.tabs.query({windowId:window.id});
  const host=tabs.find(tab=>tab.url===hostUrl()||((!tab.url||tab.url==='about:blank')&&tab.pendingUrl===hostUrl()));
  if(!host||tabs.length!==1)throw Error('background_window_host_unavailable');
  const context={windowId:window.id,hostTabId:host.id};
  if(host.url!==hostUrl())Object.assign(context,{restoreHost:true,restoreHostUrl:host.url||''});
  // Persist before native registration so a retry reuses this one host window.
  await chrome.storage.local.set({[stateKey]:context});
  await validateContents(context.windowId,context.hostTabId,ownership,host.url||'');
  return context;
 }
 async function restoreHost(context){
  // Recheck immediately before navigating. No model or unrelated tab changes.
  const ownership=await readOwnership();
  const current=await chrome.tabs.get(context.hostTabId);
  if(current.windowId===context.windowId&&current.url===hostUrl()){
   await validateContents(context.windowId,context.hostTabId,ownership);
   delete context.restoreHost;delete context.restoreHostUrl;return;
  }
  await validateContents(context.windowId,context.hostTabId,ownership,context.restoreHostUrl);
  await chrome.tabs.update(context.hostTabId,{url:hostUrl()});
  const deadline=Date.now()+4000;
  while(Date.now()<deadline){
   const tab=await chrome.tabs.get(context.hostTabId);
   if(tab.windowId!==context.windowId)throw Error('background_window_host_changed');
   if(tab.url===hostUrl()){delete context.restoreHost;delete context.restoreHostUrl;return;}
   if(![context.restoreHostUrl,'about:blank',''].includes(tab.url||'')||(tab.pendingUrl&&tab.pendingUrl!==hostUrl()))throw Error('background_window_host_changed');
   await pause(100);
  }
  throw Error('background_window_host_unavailable');
 }
 async function setTitle(context,title){
  const deadline=Date.now()+4000;
  while(Date.now()<deadline){
   try{
    const tab=await chrome.tabs.get(context.hostTabId);
    if(tab.windowId!==context.windowId||tab.url!==hostUrl())throw Error('background_window_host_changed');
    const results=await chrome.scripting.executeScript({target:{tabId:context.hostTabId,frameIds:[0]},func:(expectedUrl,value)=>{
     if(location.href!==expectedUrl||location.origin!=='http://127.0.0.1:18769'||location.pathname!=='/browser-window/host')return {ok:false};
     document.title=value;
     return {ok:true,url:location.href,title:document.title};
    },args:[hostUrl(),title]});
    const reply=results.find(result=>result.frameId===0)?.result;
    if(reply?.ok===true&&reply.url===hostUrl()&&reply.title===title){
     const current=await chrome.tabs.get(context.hostTabId);
     if(current.windowId===context.windowId&&current.url===hostUrl())return;
    }
   }catch{}
   await pause(100);
  }
  throw Error('background_window_host_unavailable');
 }
 function nativeError(reply){
  const error=String(reply?.error||'');
  return /^(?:background_window_[a-z_]+|owned_window_missing|owned_window_title_mismatch)$/.test(error)?error:'background_window_not_hidden';
 }
 function windowMode(reply){
  const capability=reply?.capabilities;
  // Older Windows backends have no capability field and retain their strict
  // native receipt contract. A macOS fallback requires explicit negotiation.
  if(capability===undefined)return 'native_hidden';
  if(capability?.mode==='native_hidden'&&capability.platform==='windows'&&capability.native_hide===true)return 'native_hidden';
  if(capability?.mode==='extension_minimized'&&capability.platform==='macos'&&capability.native_hide===false&&capability.extension_minimize===true&&capability.fully_hidden===false)return 'extension_minimized';
  throw Error('background_window_platform_unsupported');
 }
 function backgroundState(context){
  return context.windowMode==='extension_minimized'
   ?{hidden:false,minimized:true,visible:false,verified:true,window_mode:'extension_minimized',verification_source:'chrome_extension'}
   :{hidden:true,visible:false,verified:true};
 }
 async function extensionVisibility(context,token,state){
  const expectedUrl=context.restoreHost?context.restoreHostUrl:hostUrl();
  await validateContents(context.windowId,context.hostTabId,await readOwnership(),expectedUrl);
  const before=await chrome.windows.get(context.windowId);
  if(before.state!==state)await chrome.windows.update(context.windowId,{state,...(state==='normal'?{focused:true}:{})});
  // Recheck both the exact owned container and the observed result. Neither an
  // update promise nor a backend acknowledgement alone proves minimization.
  await validateContents(context.windowId,context.hostTabId,await readOwnership(),expectedUrl);
  const observed=await chrome.windows.get(context.windowId);
  if(observed.state!==state)throw Error('background_window_visibility_unverified');
  const reply=await request('/visibility',token,{window_id:context.windowId,host_tab_id:context.hostTabId,title:context.title,state});
  if(reply?.ok!==true||reply.window_id!==context.windowId||reply.host_tab_id!==context.hostTabId||reply.title!==context.title||reply.window_mode!=='extension_minimized'||reply.hidden!==false||reply.verified!==true||reply.verification_source!=='chrome_extension'||reply.minimized!==(state==='minimized')||reply.visible!==(state==='normal')||reply.manual_reveal!==(state==='normal'))throw Error('background_window_visibility_rejected');
 }
 async function manualReveal(context,reply,token){
  context.windowMode=windowMode(reply);
  if(reply.manual_reveal!==true)return false;
  if(context.windowMode==='extension_minimized'){
   context.title=reply.title;
   await extensionVisibility(context,token,'normal');
  }
  return true;
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
   if(context.windowMode==='extension_minimized'){
    if(reply?.ok!==true||reply.action!=='minimize'||reply.window_mode!=='extension_minimized'||reply.hidden!==false||reply.manual_reveal!==false||reply.window_id!==context.windowId||reply.host_tab_id!==context.hostTabId||reply.title!==context.title)throw Error('background_window_minimization_rejected');
    await extensionVisibility(context,token,'minimized');return;
   }
   if(reply?.ok===true&&reply.hidden===true&&reply.visible===false&&reply.verified===true&&reply.window_id===context.windowId&&reply.host_tab_id===context.hostTabId&&reply.title===context.title)return;
   const transient=reply?.ok===false&&['owned_window_missing','owned_window_title_mismatch'].includes(reply.error);
   if(!transient||attempt===3)throw Error(nativeError(reply));
   await pause(100);
  }
 }
 async function persistPending(pending,updates={}){
  // Record ownership and creation proof together before waiting on navigation.
  await chrome.storage.local.set({...updates,[pendingKey]:pending});
  await chrome.storage.session.set({[pendingKey]:pending,...(updates.bridgePool?{bridgePool:updates.bridgePool}:{})});
 }
 async function waitCreated(context,tabId,provider){
  const deadline=Date.now()+15000;
  while(Date.now()<deadline){
   let current;try{current=await chrome.tabs.get(tabId);}catch{throw Error('background_window_created_tab_unavailable');}
   if(current.windowId!==context.windowId)throw Error('background_window_ownership_changed');
   if(providerOrigin(provider,current.url)&&current.url!=='about:blank')return current;
   if(current.url&&current.url!=='about:blank')throw Error('background_window_ownership_changed');
   await pause(100);
  }
  throw Error('background_window_created_tab_timeout');
 }
 function recoveryUrl(claim,ownership){
  if(claim.provider==='chatgpt')return home.chatgpt;
  const isHome=url=>url===home[claim.provider]||(claim.provider==='doubao'&&url==='https://www.doubao.com/chat');
  if(!isHome(claim.url)&&providerUrl(claim.provider,claim.url))return claim.url;
  const records=Object.values(ownership.local[claim.provider+'Conversations']||{}).map((value,index)=>({value,index})).filter(item=>providerUrl(claim.provider,item.value?.url)&&!isHome(item.value.url));
  records.sort((a,b)=>(Number(b.value.used)||0)-(Number(a.value.used)||0)||b.index-a.index);
  return records[0]?.value.url||claim.url;
 }
 async function restoreClosed(context,token,requested,ownership){
  if(requested===undefined)return {ownership,errors:{}};
  if(!requested||typeof requested.nonce!=='string'||!recoveryPattern.test(requested.nonce))throw Error('background_window_recovery_rejected');
  const nonce=requested.nonce,errors={};
  if(ownership.local[recoveryKey]===nonce)return {ownership,errors,nonce,applied:true};
  for(const claim of ownership.absent){
   try{
    const current=await readOwnership();
    if(!current.absent.some(item=>item.tabId===claim.tabId&&item.provider===claim.provider&&item.url===claim.url))continue;
    const url=recoveryUrl(claim,current);
    if(!providerUrl(claim.provider,url))throw Error('background_window_recovery_unproven');
    let pool,slotId;
    if(claim.provider==='chatgpt'){
     const old=current.pool.find(slot=>slot.tabId===claim.tabId);
     slotId=old?.slotId??[0,1,2].find(value=>!current.pool.some(slot=>slot.slotId===value));
     if(!Number.isInteger(slotId)||slotId<0||slotId>2)throw Error('background_window_model_capacity');
    }
    const tab=await chrome.tabs.create({url,windowId:context.windowId,active:false});
    if(!id(tab?.id)||tab.windowId!==context.windowId)throw Error('background_window_create_failed');
    const pending=[...current.local[pendingKey].filter(item=>item.tabId!==claim.tabId),{tabId:tab.id,provider:claim.provider,url,windowId:context.windowId}];
    let updates;
    if(claim.provider==='chatgpt'){
     // A closed temporary chat cannot prove retained conversation context.
     // Restore a blank idle slot; the next assigned contact starts afresh.
     const slot={tabId:tab.id,slotId,url,turns:0,used:Date.now(),bootstrapIdle:true};
     pool=current.pool.some(item=>item.tabId===claim.tabId)?current.pool.map(item=>item.tabId===claim.tabId?slot:item):[...current.pool,slot];
     if(pool.length>3)throw Error('background_window_model_capacity');
     updates={bridgePool:pool};
    }else updates={[claim.provider+'OwnedTab']:tab.id,[claim.provider+'OwnedUrl']:url};
    await persistPending(pending,updates);
    await waitCreated(context,tab.id,claim.provider);
   }catch(error){errors[claim.provider]=/^background_window_[a-z_]+$/.test(error?.message||'')?error.message:'background_window_recovery_failed';}
  }
  return {ownership:await readOwnership(),errors,nonce};
 }
 async function prepare(){
  let ownership=await readOwnership();const token=ownership.local.token;
  if(!token)throw Error('background_window_token_required');
  if(!renderLease&&ownership.local[renderKey])await restoreRendering(ownership);
  if(renderLease){
   const context=renderLease.context;
   await validateContents(context.windowId,context.hostTabId,ownership);
   const registered=await request('/register',token,{window_id:context.windowId,host_tab_id:context.hostTabId});
   if(registered?.ok!==true||registered.window_id!==context.windowId||registered.host_tab_id!==context.hostTabId||registered.title!==context.title)throw Error('background_window_registration_rejected');
   if(await manualReveal(context,registered,token))throw Error('background_window_manual_reveal');
   if(context.windowMode==='extension_minimized')await extensionVisibility(context,token,'minimized');
   // A verified hidden window remains the same window when only its active tab
   // changes. Other providers may work, but must not reselect the controller.
   if(renderLease.releasing){
    await finishRendering(renderLease,token);return prepare();
   }
   if(renderLease.recovered)watchRendering(renderLease.id);
   cached=context;return {context,ownership};
  }
  const context=await findHost(ownership);
  const reply=await request('/register',token,{window_id:context.windowId,host_tab_id:context.hostTabId});
  if(reply?.ok!==true||reply.window_id!==context.windowId||reply.host_tab_id!==context.hostTabId||!titlePattern.test(reply.title||''))throw Error('background_window_registration_rejected');
  // A controlled reveal pauses work before changing the user's active tab.
  if(await manualReveal(context,reply,token))throw Error('background_window_manual_reveal');
  if(reply.recovery_requested!==undefined&&(!reply.recovery_requested||typeof reply.recovery_requested.nonce!=='string'||!recoveryPattern.test(reply.recovery_requested.nonce)))throw Error('background_window_recovery_rejected');
  // Registration/manual mode is checked before restoring even a blank host.
  if(context.restoreHost)await restoreHost(context);
  context.title=reply.title;
  await chrome.storage.local.set({[stateKey]:context});cached=context;
  await validateContents(context.windowId,context.hostTabId,ownership);
  // Verify the empty/already-owned window's negotiated background state before
  // moving pages. macOS remains a minimized Chrome window, never native-hidden.
  await hide(context,token);
  const recovery=await restoreClosed(context,token,reply.recovery_requested,ownership);ownership=recovery.ownership;
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
  const modelErrors={...finalOwnership.modelErrors,...recovery.errors};
  for(const claim of finalOwnership.claims)if(finalOwnership.missing.includes(claim.tabId)&&!modelErrors[claim.provider])modelErrors[claim.provider]=claim.provider+'_owned_tab_unavailable';
  let recoveryPending=!!recovery.nonce,recoveryError;
  if(recovery.nonce&&!finalOwnership.missing.length&&!Object.keys(modelErrors).length){
   try{
    const done=await request('/recovered',token,{nonce:recovery.nonce,window_id:context.windowId,host_tab_id:context.hostTabId});
    if(done?.ok!==true||done.nonce!==recovery.nonce||done.window_id!==context.windowId||done.host_tab_id!==context.hostTabId)throw Error('background_window_recovery_acknowledgement_rejected');
    await chrome.storage.local.set({[recoveryKey]:recovery.nonce});recoveryPending=false;
   }catch(error){recoveryError=error?.message||'background_window_recovery_failed';}
  }
  await health({ready:true,...backgroundState(context),window_id:context.windowId,host_tab_id:context.hostTabId,managed_tabs:finalOwnership.tabs.length,missing_tab_ids:finalOwnership.missing,model_errors:modelErrors,recovery_pending:recoveryPending,...(recoveryError?{recovery_error:recoveryError}:{})});
  return {context,ownership:finalOwnership};
 }
 async function guarded(task){
  try{return await task();}
  catch(error){await health({ready:false,hidden:false,visible:null,verified:false,error:String(error?.message||'background_window_failed')}).catch(()=>{});throw error;}
 }
 async function pulse(provider,failureCode){
  if(!home[provider])return false;
  let timer;
  try{
   const keys=[stateKey,'bridgeBackgroundWindowHealth','token','bridgePool',provider+'OwnedTab',provider+'OwnedUrl'];
   const saved=await chrome.storage.local.get(keys),context=saved[stateKey],windowHealth=saved.bridgeBackgroundWindowHealth;
   if(!saved.token)return false;
   let code=typeof failureCode==='string'&&/^(?:background_window_|owned_window_)[a-z_]+$/.test(failureCode)?failureCode:(failureCode?provider+'_background_window_unavailable':'');
   let candidates=[];
   let backgroundVerified=windowHealth?.verified===true&&windowHealth.visible===false&&windowHealth.hidden===true;
   if(context?.windowMode==='extension_minimized'){
    backgroundVerified=windowHealth?.verified===true&&windowHealth.visible===false&&windowHealth.hidden===false&&windowHealth.minimized===true&&windowHealth.window_mode==='extension_minimized'&&windowHealth.verification_source==='chrome_extension';
    if(backgroundVerified)backgroundVerified=(await chrome.windows.get(context.windowId)).state==='minimized';
   }
   if(!code&&context&&id(context.windowId)&&id(context.hostTabId)&&windowHealth?.ready===true&&backgroundVerified&&windowHealth.window_id===context.windowId&&windowHealth.host_tab_id===context.hostTabId&&!windowHealth.model_errors?.[provider]){
    if(provider==='chatgpt'){
     const pool=saved.bridgePool;
     if(Array.isArray(pool)&&pool.length<=3&&pool.every(slot=>slot&&id(slot.tabId)&&providerUrl(provider,slot.url)))candidates=pool.map(slot=>({tabId:slot.tabId,url:slot.url,exact:true}));
    }else if(id(saved[provider+'OwnedTab'])&&providerOrigin(provider,saved[provider+'OwnedUrl']))candidates=[{tabId:saved[provider+'OwnedTab']}];
   }
   let owned=0;
   for(const candidate of candidates){
    let tab;try{tab=await chrome.tabs.get(candidate.tabId);}catch{continue;}
    if(tab.windowId===context.windowId&&providerUrl(provider,tab.url)&&(!candidate.exact||tab.url===candidate.url))owned++;
   }
   if(!code&&!owned)code=provider+'_owned_tab_unavailable';
   const body={blocked:!!code,...(code?{code}:{})};
   const controller=new AbortController();timer=setTimeout(()=>controller.abort(),8000);
   const headers={Authorization:'Bearer '+saved.token,'Content-Type':'application/json','X-Wechat-Bridge-Provider':provider};
   if(provider==='chatgpt')headers['X-Wechat-Bridge-Build']='fast-sticker-v2';
   const reply=await fetch('http://127.0.0.1:18769/browser-bridge/heartbeat',{method:'POST',headers,body:JSON.stringify(body),signal:controller.signal});
   return reply.ok===true;
  }catch{return false;}
  finally{if(timer)clearTimeout(timer);}
 }
 function ensure(provider){
  if(!ensureFlight)ensureFlight=enqueue(()=>guarded(async()=>{const {context}=await prepare();return context;})).finally(()=>{ensureFlight=null;});
  const flight=ensureFlight;
  if(provider===undefined)return flight;
  return flight.then(async context=>{await pulse(provider);return context;},async error=>{await pulse(provider,error?.message||'background_window_failed');throw error;});
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
   const pending=ownership.local[pendingKey]||[];
   await persistPending([...pending,{tabId:tab.id,provider,url:properties.url,windowId:context.windowId}]);
   // A native hide failure keeps the created page recorded; never create a
   // replacement or expose it in the ordinary current/focused Chrome window.
   await validateContents(context.windowId,context.hostTabId,await readOwnership());
   if(!renderLease)await hide(context,ownership.local.token);
   // Provider code needs a committed URL before it can navigate or inspect the
   // page. Wait on the same ID; a slow/closed page never causes a replacement.
   return await waitCreated(context,tab.id,provider);
  }));
 }
 async function renderingActive(lease){
  const results=await chrome.scripting.executeScript({target:{tabId:lease.tabId,frameIds:[0]},func:jobId=>window.wechatDoubaoActiveTurn===jobId,args:[lease.jobId]});
  const active=results.find(result=>result.frameId===0)?.result;
  if(typeof active!=='boolean')throw Error('background_window_rendering_unverified');
  return active;
 }
 async function restoreRendering(ownership){
  const saved=ownership.local[renderKey],context=ownership.local[stateKey];
  if(!saved||typeof saved.id!=='string'||!/^[-0-9a-f]{36}$/.test(saved.id)||saved.provider!=='doubao'||!id(saved.tabId)||typeof saved.jobId!=='string'||!context||saved.windowId!==context.windowId||saved.hostTabId!==context.hostTabId||!titlePattern.test(context.title||''))throw Error('background_window_rendering_invalid');
  const item=[...ownership.tabs,...ownership.contained].find(item=>item.tab.id===saved.tabId&&item.claim.provider===saved.provider&&item.tab.windowId===context.windowId);
  if(!item){
   if(ownership.absent.some(claim=>claim.tabId===saved.tabId)){await chrome.storage.local.remove(renderKey);return;}
   throw Error('background_window_rendering_unproven');
  }
  renderLease={...saved,context,recovered:true};
 }
 function watchRendering(leaseId){
  if(renderLease?.id!==leaseId||renderTimer||(renderWatch?.id===leaseId&&!renderLease.releasing))return;
  renderTimer=setTimeout(()=>{
   renderTimer=null;
   if(renderLease?.id!==leaseId)return;
   const flight={id:leaseId};renderWatch=flight;
   return (async()=>{
    const lease=await enqueue(()=>guarded(async()=>{
     if(renderLease?.id!==leaseId)return null;
     const ownership=await readOwnership();
     await validateContents(renderLease.context.windowId,renderLease.context.hostTabId,ownership);
     return renderLease;
    }));
    if(!lease)return;
    // A recovered content probe may never settle. Keep it single-flight and
    // outside the shared queue so DeepSeek and GPT can still prepare their tabs.
    // Only a confirmed idle result may release the lease; no timeout cancels it.
    if(!lease.releasing&&await renderingActive(lease))return;
    await enqueue(()=>guarded(async()=>{
     if(renderLease?.id!==leaseId)return;
     const ownership=await readOwnership();
     // finishRendering rechecks manual reveal and current ownership after the
     // asynchronous probe, before selecting the controller or hiding anything.
     await finishRendering(renderLease,ownership.local.token);
    }));
   })().catch(()=>{}).finally(()=>{
    if(renderWatch===flight)renderWatch=null;
    if(renderLease?.id===leaseId)watchRendering(leaseId);
   });
  },1000);
 }
 function acquireRendering(tabId,provider,jobId){
  return enqueue(()=>guarded(async()=>{
   if(renderLease)throw Error('background_window_rendering_busy');
   if(provider!=='doubao'||typeof jobId!=='string'||!/^[a-zA-Z0-9_-]{1,128}$/.test(jobId))throw Error('background_window_rendering_rejected');
   const {context,ownership}=await prepare();
   if(renderLease)throw Error('background_window_rendering_busy');
   const item=ownership.tabs.find(item=>item.tab.id===tabId&&item.claim.provider===provider&&item.tab.windowId===context.windowId);
   if(!item||!providerOrigin(provider,item.tab.url))throw Error('background_window_tab_unproven');
   const lease={id:crypto.randomUUID(),provider,tabId,jobId,windowId:context.windowId,hostTabId:context.hostTabId,context};
   // Persist before activation. A worker restart inspects only the owned
   // content promise marker and never uses a timeout to cancel that promise.
   await chrome.storage.local.set({[renderKey]:{id:lease.id,provider,tabId,jobId,windowId:context.windowId,hostTabId:context.hostTabId}});
   renderLease=lease;clearTimeout(activationTimer);activationTimer=null;
   try{
    await chrome.tabs.update(tabId,{active:true});
    if(context.windowMode==='extension_minimized')await extensionVisibility(context,ownership.local.token,'minimized');
    await health({ready:true,...backgroundState(context),window_id:context.windowId,host_tab_id:context.hostTabId,rendering:true,rendering_provider:provider});
    return lease.id;
   }catch(error){
    // No content script has been started by the caller yet. Restore the host
    // even when activation succeeded but the health write failed.
    lease.releasing=true;
    try{await finishRendering(lease,ownership.local.token);}catch{if(renderLease?.id===lease.id)watchRendering(lease.id);}
    throw error;
   }
  }));
 }
 async function finishRendering(lease,token,checkManual=true){
  if(renderLease?.id!==lease.id)return false;
  const context=lease.context;
  const registered=checkManual?await request('/register',token,{window_id:context.windowId,host_tab_id:context.hostTabId}):null;
  if(registered&&(registered.ok!==true||registered.window_id!==context.windowId||registered.host_tab_id!==context.hostTabId||registered.title!==context.title))throw Error('background_window_registration_rejected');
  if(registered&&await manualReveal(context,registered,token)){
   renderLease=null;clearTimeout(renderTimer);renderTimer=null;await chrome.storage.local.remove(renderKey);
   await health({ready:false,hidden:false,visible:true,verified:false,rendering:false,error:'background_window_manual_reveal'});return true;
  }
  const ownership=await readOwnership();
  await validateContents(context.windowId,context.hostTabId,ownership);
  await hide(context,token);
  if(renderLease?.id!==lease.id)return false;
  renderLease=null;clearTimeout(renderTimer);renderTimer=null;await chrome.storage.local.remove(renderKey);
  await health({ready:true,...backgroundState(context),window_id:context.windowId,host_tab_id:context.hostTabId,rendering:false});return true;
 }
 function releaseRendering(leaseId){
  return enqueue(()=>guarded(async()=>{
   if(!leaseId||renderLease?.id!==leaseId)return false;
   const lease=renderLease;lease.releasing=true;
   try{const ownership=await readOwnership();return await finishRendering(lease,ownership.local.token);}
   catch(error){if(renderLease?.id===lease.id)watchRendering(lease.id);throw error;}
  }));
 }
 function isRenderingIdle(provider){
  return enqueue(()=>guarded(async()=>{
   if(provider!=='doubao')throw Error('background_window_rendering_rejected');
   await prepare();return !renderLease;
  }));
 }
 function startup(){return ensure().catch(()=>console.warn('Bridge background window requires attention'));}
  const api={ensure,pulse,ensureOwnedTab,createModelTab,startup,acquireRendering,releaseRendering,isRenderingIdle};root.WechatBackgroundWindow=api;
 // Events only retry the window contract; they never create model pages.
 if(chrome.runtime.onStartup)chrome.runtime.onStartup.addListener(startup);
 if(chrome.runtime.onInstalled)chrome.runtime.onInstalled.addListener(startup);
 if(chrome.tabs.onUpdated)chrome.tabs.onUpdated.addListener((tabId,change)=>{if(cached?.hostTabId===tabId&&change.status==='complete')startup();});
 if(chrome.tabs.onAttached)chrome.tabs.onAttached.addListener((tabId,info)=>{if(cached&&info.newWindowId===cached.windowId)startup();});
 if(chrome.tabs.onActivated)chrome.tabs.onActivated.addListener(info=>{
  if(renderLease&&info.windowId===renderLease.context.windowId&&info.tabId===renderLease.tabId)return;
  if(!cached||info.windowId!==cached.windowId||info.tabId===cached.hostTabId)return;
  clearTimeout(activationTimer);
  // Chrome's tab search can activate a tab in another window. Debounce those
  // events and recheck manual_reveal before selecting/hiding the controller.
  activationTimer=setTimeout(()=>{activationTimer=null;startup();},100);
 });
 if(chrome.storage.onChanged)chrome.storage.onChanged.addListener((changes,area)=>{if(area==='local'&&changes.token?.newValue)startup();});
})(globalThis);

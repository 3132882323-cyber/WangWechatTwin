const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {webcrypto}=require('node:crypto');

const hostUrl='http://127.0.0.1:18769/browser-window/host';
const retiredHostUrl='chrome-extension://bridge-test/background_host.html';
const title='WeChatTwin Background '+ 'a'.repeat(32);
const recoveryNonce='c'.repeat(32);
const gptHome='https://chatgpt.com/?temporary-chat=true';
const dsHome='https://chat.deepseek.com/';
const dsUrl='https://chat.deepseek.com/a/chat/s/owned-one';
const duoHome='https://www.doubao.com/chat/';
const duoUrl='https://www.doubao.com/chat/12345';
const listener=()=>({listeners:[],addListener(fn){this.listeners.push(fn);}});

function harness(options={}){
 const local={token:'offline-test-token',...structuredClone(options.local||{})};
 const session=structuredClone(options.session||{});
 const tabs=new Map((options.tabs||[]).map(tab=>[tab.id,{active:false,...structuredClone(tab)}]));
 const windows=new Map((options.windows||[{id:1,type:'normal',incognito:false}]).map(window=>[window.id,structuredClone(window)]));
 const calls={windows:[],creates:[],moves:[],updates:[],requests:[],titles:[],queries:[],scripts:[],messages:[],storageWrites:[]};
 let nextWindow=100,nextTab=1000;
 const storage=target=>({
  async get(keys){
   if(typeof keys==='string')return {[keys]:structuredClone(target[keys])};
   if(Array.isArray(keys))return Object.fromEntries(keys.map(key=>[key,structuredClone(target[key])]));
   return structuredClone(target);
  },
  async set(values){options.beforeStorageSet?.(values,target===local?'local':'session');calls.storageWrites.push({area:target===local?'local':'session',values:structuredClone(values)});Object.assign(target,structuredClone(values));},
  async remove(key){delete target[key];}
 });
 const chrome={
  storage:{local:storage(local),session:storage(session),onChanged:listener()},
  runtime:{id:'bridge-test',getURL:file=>'chrome-extension://bridge-test/'+file,onStartup:listener(),onInstalled:listener(),onMessage:listener(),openOptionsPage(){},
   async sendMessage(message){calls.messages.push(structuredClone(message));throw Error('host messaging is retired');}
  },
  windows:{
   async get(windowId){if(!windows.has(windowId))throw Error('window closed');return structuredClone(windows.get(windowId));},
   async create(properties){
    calls.windows.push(structuredClone(properties));const window={id:nextWindow++,type:properties.type,incognito:false,state:properties.state,focused:properties.focused};windows.set(window.id,window);
    const tab={id:nextTab++,windowId:window.id,url:properties.url,active:true};tabs.set(tab.id,tab);options.onWindowCreate?.(tab,window);
    return structuredClone({...window,tabs:[tab]});
   }
  },
  tabs:{
   onUpdated:listener(),onAttached:listener(),onActivated:listener(),
   async get(tabId){if(!tabs.has(tabId))throw Error('tab closed');options.onGet?.(tabs.get(tabId));return structuredClone(tabs.get(tabId));},
   async query(query){calls.queries.push(structuredClone(query));return [...tabs.values()].filter(tab=>query.windowId===undefined||tab.windowId===query.windowId).map(tab=>structuredClone(tab));},
   async move(tabId,properties){calls.moves.push({tabId,...structuredClone(properties)});const tab=tabs.get(tabId);if(!tab)throw Error('closed');tab.windowId=properties.windowId;tab.active=false;return structuredClone(tab);},
   async create(properties){calls.creates.push(structuredClone(properties));options.beforeCreate?.(properties,{calls,tabs,windows});const tab={id:nextTab++,...structuredClone(properties)};tabs.set(tab.id,tab);options.onCreate?.(tab);return structuredClone(tab);},
   async update(tabId,properties){
    calls.updates.push({tabId,...structuredClone(properties)});const tab=tabs.get(tabId);if(!tab)throw Error('closed');
    if(properties.active)for(const other of tabs.values())if(other.windowId===tab.windowId)other.active=false;
    Object.assign(tab,structuredClone(properties));options.onUpdate?.(tab,properties,{calls,tabs,windows});return structuredClone(tab);
   }
  },
  scripting:{async executeScript(properties){
   calls.scripts.push({target:structuredClone(properties.target),args:structuredClone(properties.args),func:properties.func?.toString()});
   if(options.rejectHost)throw Error('host not ready');
   const tab=tabs.get(properties.target.tabId);if(!tab)throw Error('closed');options.onScript?.(tab,properties);
   const pageUrl=options.pageUrl?.(tab)||tab.url,parsed=new URL(pageUrl),document={title:tab.title||'WeChatTwin Background'};
   const result=vm.runInNewContext('('+properties.func.toString()+')(...args)',{location:{href:pageUrl,origin:parsed.origin,pathname:parsed.pathname},document,window:{wechatDoubaoActiveTurn:tab.activeTurn},args:properties.args||[]});
   tab.title=document.title;
   calls.titles.push({window_id:tab.windowId,host_tab_id:tab.id,title:tab.title});
   return [{frameId:options.scriptFrameId??0,result}];
  }},
  alarms:{create(){},onAlarm:listener()},action:{onClicked:listener()}
 };
 async function fetch(url,request={}){
  if(url.endsWith('-bootstrap.local.json')||url.endsWith('/gpt-migration.local.json'))return {async json(){return {};}};
  const body=request.body?JSON.parse(request.body):{},kind=url.split('/').at(-1);
  calls.requests.push({kind,...request,body,bodyText:request.body});
  const reply={ok:true,window_id:body.window_id,host_tab_id:body.host_tab_id,title,manual_reveal:options.manualReveal===true};
  if(kind==='next')return {ok:true,async json(){return {job:null};}};
  if(kind==='register'&&options.recoveryNonce!==undefined)reply.recovery_requested={nonce:options.recoveryNonce};
  if(kind==='recovered')reply.nonce=body.nonce;
  if(kind==='hide'){
   if(options.manualReveal){Object.assign(reply,{ok:false,hidden:false,visible:null,verified:false,error:'background_window_manual_reveal'});}
   else Object.assign(reply,{hidden:true,visible:false,verified:true});
   options.onHide?.({tabs,windows,calls,reply});
  }
  options.reply?.(kind,reply,body);
  if(kind==='recovered'&&reply.ok===true&&reply.nonce===body.nonce&&reply.window_id===body.window_id&&reply.host_tab_id===body.host_tab_id)delete options.recoveryNonce;
  return {ok:options.httpOk!==false,async json(){return structuredClone(reply);}};
 }
 const context=vm.createContext({chrome,fetch,URL,Date,AbortController,crypto:webcrypto,setTimeout:options.setTimeout||setTimeout,clearTimeout:options.clearTimeout||clearTimeout,console:{warn(){}},WebSocket:class{constructor(){}},
  importScripts(file){if(['pool_core.js','background_window.js'].includes(file))vm.runInContext(fs.readFileSync(path.join(__dirname,file),'utf8'),context,{filename:file});else assert.ok(['deepseek_background.js','doubao_background.js'].includes(file));}
 });
 const load=()=>vm.runInContext(fs.readFileSync(path.join(__dirname,'background_window.js'),'utf8'),context,{filename:'background_window.js'});
 load();
 return {api:()=>context.WechatBackgroundWindow,context,local,session,tabs,windows,calls,options,load};
}

function ownedOptions(){
 return {
  local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,deepseekConversations:{contactA:{url:dsUrl,name:'A',lastTurnId:'turn-a'}},doubaoOwnedTab:12,doubaoOwnedUrl:duoUrl,doubaoConversations:{contactB:{url:duoUrl,name:'B',lastTurnId:'turn-b'}}},
  session:{bridgePool:[0,1,2].map(index=>({tabId:20+index,slotId:index,url:gptHome,key:'contact-'+index,turns:5,used:index}))},
  tabs:[{id:11,windowId:1,url:dsUrl},{id:12,windowId:1,url:duoUrl},...[20,21,22].map(id=>({id,windowId:1,url:gptHome})),{id:99,windowId:1,url:'https://example.com/',active:true},{id:98,windowId:1,url:dsUrl}]
 };
}

function renderingHarness(){
 let next=0;const timers=new Map();
 const fixture=harness({...ownedOptions(),
  setTimeout(done,ms){const timer=++next;timers.set(timer,{done,ms});return timer;},
  clearTimeout(timer){timers.delete(timer);}
 });
 fixture.fireRenderingRetry=async()=>{
  const timer=[...timers].find(([,value])=>value.ms===1000);
  assert.ok(timer,'a rendering recovery retry is scheduled');
  timers.delete(timer[0]);timer[1].done();
  await fixture.api().releaseRendering('not-the-owner');
 };
 return fixture;
}

test('Doubao exclusively renders in the hidden owned window while DeepSeek and GPT stay inactive',async()=>{
 const fixture=renderingHarness();const state=await fixture.api().ensure();
 const lease=await fixture.api().acquireRendering(12,'doubao','render-turn');
 const before={updates:fixture.calls.updates.length,hides:fixture.calls.requests.filter(call=>call.kind==='hide').length};
 await fixture.api().ensureOwnedTab(11,'deepseek');await fixture.api().ensureOwnedTab(20,'chatgpt');
 assert.equal(await fixture.api().isRenderingIdle('doubao'),false);
 await assert.rejects(fixture.api().acquireRendering(12,'doubao','other-turn'),/background_window_rendering_busy/);
 assert.equal(fixture.calls.updates.length,before.updates);assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide').length,before.hides);
 assert.equal(fixture.tabs.get(12).active,true);assert.equal(fixture.tabs.get(11).active,false);assert.equal(fixture.tabs.get(20).active,false);
 assert.equal(fixture.tabs.get(99).windowId,1);assert.equal(fixture.tabs.get(99).active,true);
 assert.equal(await fixture.api().releaseRendering('wrong-lease'),false);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease.id,lease);
 assert.equal(await fixture.api().releaseRendering(lease),true);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);assert.equal(fixture.tabs.get(state.hostTabId).active,true);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);assert.equal(fixture.local.bridgeBackgroundWindowHealth.rendering,false);
});

test('a rendering lease cannot activate an ordinary user page',async()=>{
 const fixture=renderingHarness();await fixture.api().ensure();
 await assert.rejects(fixture.api().acquireRendering(99,'doubao','render-turn'),/background_window_tab_unproven/);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);assert.equal(fixture.tabs.get(99).windowId,1);
 assert.equal(fixture.calls.updates.some(call=>call.tabId===99),false);
});

for(const failure of ['activation','health']){
 test(`a ${failure} failure after rendering lease persistence restores the host and permits the next turn`,async()=>{
  const fixture=renderingHarness();const state=await fixture.api().ensure();let failed=false;
  if(failure==='activation')fixture.options.onUpdate=(tab,properties)=>{if(tab.id===12&&properties.active&&!failed){failed=true;throw Error('temporary_activation_error');}};
  else fixture.options.beforeStorageSet=values=>{if(values.bridgeBackgroundWindowHealth?.rendering===true&&!failed){failed=true;throw Error('temporary_health_error');}};
  await assert.rejects(fixture.api().acquireRendering(12,'doubao','failed-turn'),/temporary_(activation|health)_error/);
  assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);assert.equal(fixture.tabs.get(state.hostTabId).active,true);
  const lease=await fixture.api().acquireRendering(12,'doubao','next-turn');await fixture.api().releaseRendering(lease);
  assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);
 });
}

test('failed activation cleanup retries native hiding without permanently reserving the Doubao page',async()=>{
 const fixture=renderingHarness();await fixture.api().ensure();let failed=false,denyHide=false;
 fixture.options.onUpdate=(tab,properties)=>{if(tab.id===12&&properties.active&&!failed){failed=true;denyHide=true;throw Error('temporary_activation_error');}};
 fixture.options.reply=(kind,reply)=>{if(kind==='hide'&&denyHide)reply.verified=false;};
 await assert.rejects(fixture.api().acquireRendering(12,'doubao','failed-turn'),/temporary_activation_error/);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease.jobId,'failed-turn');
 denyHide=false;await fixture.fireRenderingRetry();
 assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);
 const lease=await fixture.api().acquireRendering(12,'doubao','next-turn');await fixture.api().releaseRendering(lease);
});

test('a transient native hide failure on release is retried before the next rendering turn',async()=>{
 const fixture=renderingHarness();const state=await fixture.api().ensure();
 const lease=await fixture.api().acquireRendering(12,'doubao','render-turn');
 fixture.options.reply=(kind,reply)=>{if(kind==='hide')reply.verified=false;};
 await assert.rejects(fixture.api().releaseRendering(lease),/background_window_not_hidden/);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease.id,lease);assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,false);
 delete fixture.options.reply;await fixture.fireRenderingRetry();
 assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);assert.equal(fixture.tabs.get(state.hostTabId).active,true);
 assert.equal(await fixture.api().isRenderingIdle('doubao'),true);
});

test('a recovered active Doubao turn stays leased, cannot be overwritten, and releases only after its promise ends',async()=>{
 const fixture=renderingHarness();const state=await fixture.api().ensure();
 const lease=await fixture.api().acquireRendering(12,'doubao','old-turn');fixture.tabs.get(12).activeTurn='old-turn';
 fixture.load();const before=fixture.calls.updates.length;
 await assert.rejects(fixture.api().acquireRendering(12,'doubao','new-turn'),/background_window_rendering_busy/);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease.id,lease);assert.equal(await fixture.api().isRenderingIdle('doubao'),false);
 await fixture.api().ensureOwnedTab(11,'deepseek');await fixture.fireRenderingRetry();
 assert.equal(fixture.calls.updates.length,before);assert.equal(fixture.local.bridgeBackgroundRenderingLease.jobId,'old-turn');
 delete fixture.tabs.get(12).activeTurn;await fixture.fireRenderingRetry();
 assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);assert.equal(fixture.tabs.get(state.hostTabId).active,true);
});

test('manual reveal during rendering release leaves the user selected page untouched',async()=>{
 const fixture=renderingHarness();await fixture.api().ensure();
 const lease=await fixture.api().acquireRendering(12,'doubao','render-turn');fixture.options.manualReveal=true;
 const before=fixture.calls.updates.length,hides=fixture.calls.requests.filter(call=>call.kind==='hide').length;
 assert.equal(await fixture.api().releaseRendering(lease),true);
 assert.equal(fixture.local.bridgeBackgroundRenderingLease,undefined);assert.equal(fixture.tabs.get(12).active,true);
 assert.equal(fixture.calls.updates.length,before);assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide').length,hides);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.error,'background_window_manual_reveal');
});

function diagnosticOnly(fixture,from,code){
 const added=fixture.calls.requests.slice(from);
 assert.equal(added.length,1);
 assert.equal(added[0].kind,'diagnostic');assert.equal(added[0].method,'POST');
 assert.equal(added[0].headers.Authorization,'Bearer offline-test-token');assert.deepEqual(added[0].body,{code});
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.error,code);assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,false);
}

test('migration keeps every saved model tab ID and contact map and leaves personal tabs in the daily window',async()=>{
 const fixture=harness(ownedOptions());
 const maps=structuredClone({deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations,pool:fixture.session.bridgePool});
 const result=await fixture.api().ensure();
 assert.equal(fixture.calls.windows.length,1);
 assert.deepEqual(fixture.calls.windows[0],{url:hostUrl,type:'normal',focused:false,state:'minimized'});
 assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[11,12,20,21,22]);
 assert.equal(fixture.calls.creates.length,0);
 assert.equal(fixture.tabs.get(99).windowId,1);assert.equal(fixture.tabs.get(98).windowId,1);
 assert.equal(fixture.tabs.get(99).active,true);
 for(const tabId of [11,12,20,21,22])assert.equal(fixture.tabs.get(tabId).windowId,result.windowId);
 assert.equal(fixture.tabs.get(result.hostTabId).active,true);
 assert.equal(fixture.tabs.get(result.hostTabId).title,title);
 assert.deepEqual({deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations,pool:fixture.session.bridgePool},maps);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.visible,false);
 const hides=fixture.calls.requests.filter(call=>call.kind==='hide');
 assert.equal(hides.length,2);
 for(const hide of hides){assert.equal(JSON.parse(hide.bodyText).title,title);assert.equal(hide.headers.Authorization,'Bearer offline-test-token');}
 assert.ok(fixture.calls.updates.every(call=>call.tabId===result.hostTabId&&call.active===true&&!call.url));
});

test('concurrent provider startup creates one controller and worker restart reuses its persisted tab ID',async()=>{
 const fixture=harness(ownedOptions());
 const one=fixture.api().ensure(),two=fixture.api().ensure(),three=fixture.api().ensure();
 assert.equal(one,two);assert.equal(two,three);
 const result=await one;await Promise.all([two,three]);
 fixture.load();
 const restored=await fixture.api().ensure();
 assert.deepEqual(JSON.parse(JSON.stringify(restored)),JSON.parse(JSON.stringify(result)));
 assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.moves.length,5);
});

test('window ownership still includes the same GPT IDs after extension reload clears all session pool records',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();const durable=structuredClone(fixture.local.bridgePool);
 delete fixture.session.bridgePool;fixture.load();await fixture.api().ensure();
 assert.deepEqual(fixture.local.bridgePool,durable);assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.moves.length,5);
 for(const tabId of [20,21,22])assert.equal(fixture.tabs.get(tabId).windowId,state.windowId);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);
});

test('extension reload restores only the saved newtab controller ID after registration and committed URL proof',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();
 const preserved=structuredClone({pool:fixture.local.bridgePool,deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations});
 const before=fixture.calls.updates.length,windows=fixture.calls.windows.length,moves=fixture.calls.moves.length;
 fixture.tabs.get(state.hostTabId).url='chrome://newtab/';delete fixture.session.bridgePool;
 let pending=false,reads=0;
 fixture.options.onUpdate=(tab,properties,{calls})=>{
  if(properties.url!==hostUrl)return;
  assert.equal(tab.id,state.hostTabId);assert.equal(calls.requests.at(-1).kind,'register');
  assert.equal(calls.requests.at(-1).body.window_id,state.windowId);assert.equal(calls.requests.at(-1).body.host_tab_id,state.hostTabId);
  tab.url='chrome://newtab/';tab.pendingUrl=hostUrl;pending=true;
 };
 fixture.options.onGet=tab=>{if(pending&&tab.id===state.hostTabId&&++reads===2){tab.url=hostUrl;delete tab.pendingUrl;}};
 fixture.load();const restored=await fixture.api().ensure();
 assert.equal(restored.hostTabId,state.hostTabId);assert.equal(restored.windowId,state.windowId);assert.equal(restored.restoreHost,undefined);
 assert.equal(fixture.tabs.get(state.hostTabId).url,hostUrl);assert.equal(fixture.calls.windows.length,windows);assert.equal(fixture.calls.moves.length,moves);assert.equal(fixture.calls.creates.length,0);
 assert.deepEqual(fixture.calls.updates.slice(before).filter(call=>call.url),[{tabId:state.hostTabId,url:hostUrl}]);
 assert.deepEqual({pool:fixture.local.bridgePool,deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations},preserved);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);assert.equal(fixture.local.bridgeBackgroundWindowHealth.visible,false);
});

for(const blankUrl of ['chrome://newtab/','about:blank']){
test('manual reveal prevents saved '+blankUrl+' controller restoration and keeps the user account tab selected',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();
 fixture.tabs.get(state.hostTabId).url=blankUrl;fixture.tabs.get(state.hostTabId).active=false;fixture.tabs.get(11).active=true;
 fixture.options.manualReveal=true;fixture.load();
 const requests=fixture.calls.requests.length,updates=fixture.calls.updates.length,hides=fixture.calls.requests.filter(call=>call.kind==='hide').length;
 await assert.rejects(fixture.api().ensure(),/background_window_manual_reveal/);
 assert.equal(fixture.calls.requests[requests].kind,'register');assert.deepEqual(fixture.calls.requests[requests].body,{window_id:state.windowId,host_tab_id:state.hostTabId});diagnosticOnly(fixture,requests+1,'background_window_manual_reveal');
 assert.equal(fixture.calls.updates.length,updates);assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide').length,hides);
 assert.equal(fixture.tabs.get(state.hostTabId).url,blankUrl);assert.equal(fixture.tabs.get(11).active,true);assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);
});
}

test('only the saved proven about:blank controller is restored; an unrelated blank tab and all model pages are untouched',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();
 const before=fixture.calls.updates.length,requests=fixture.calls.requests.length,moves=fixture.calls.moves.length;
 const maps=structuredClone({pool:fixture.local.bridgePool,deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations});
 fixture.tabs.get(state.hostTabId).url='about:blank';fixture.tabs.get(99).url='about:blank';delete fixture.session.bridgePool;
 fixture.options.onUpdate=(tab,properties,{calls})=>{if(properties.url){assert.equal(tab.id,state.hostTabId);assert.equal(calls.requests[requests].kind,'register');assert.deepEqual(calls.requests[requests].body,{window_id:state.windowId,host_tab_id:state.hostTabId});}};
 fixture.load();await fixture.api().ensure();
 assert.deepEqual(fixture.calls.updates.slice(before).filter(call=>call.url),[{tabId:state.hostTabId,url:hostUrl}]);
 assert.equal(fixture.tabs.get(99).url,'about:blank');assert.equal(fixture.tabs.get(99).windowId,1);
 assert.deepEqual({pool:fixture.local.bridgePool,deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations},maps);
 assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.moves.length,moves);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.visible,false);assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);
});

for(const blankUrl of ['chrome://newtab/','about:blank']){
test(blankUrl+' controller recovery rejects a window containing an unclaimed personal tab before registration',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();
 fixture.tabs.get(state.hostTabId).url=blankUrl;fixture.tabs.set(999,{id:999,windowId:state.windowId,url:'https://example.org/',active:true});fixture.load();
 const requests=fixture.calls.requests.length,updates=fixture.calls.updates.length;
 await assert.rejects(fixture.api().ensure(),/background_window_mixed_tabs/);
 diagnosticOnly(fixture,requests,'background_window_mixed_tabs');assert.equal(fixture.calls.updates.length,updates);assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);
 assert.equal(fixture.tabs.get(999).url,'https://example.org/');
});
}

for(const url of ['https://example.org/','about:blank#user-state','chrome://extensions/','chrome://newtab/#user-state']){
 test('saved controller navigation to '+url+' is rejected without replacing its page or creating another window',async()=>{
  const fixture=harness(ownedOptions());const state=await fixture.api().ensure();fixture.tabs.get(state.hostTabId).url=url;fixture.load();
  const requests=fixture.calls.requests.length,updates=fixture.calls.updates.length;
  await assert.rejects(fixture.api().ensure(),/background_window_host_changed/);
  diagnosticOnly(fixture,requests,'background_window_host_changed');assert.equal(fixture.calls.updates.length,updates);assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.tabs.get(state.hostTabId).url,url);
 });
}

for(const blankUrl of ['chrome://newtab/','about:blank']){
test(blankUrl+' controller restoration refuses a changed window ID even though the host tab ID is unchanged',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();const tab=fixture.tabs.get(state.hostTabId);tab.url=blankUrl;tab.windowId=1;fixture.load();
 const requests=fixture.calls.requests.length,updates=fixture.calls.updates.length;
 await assert.rejects(fixture.api().ensure(),/background_window_host_changed/);
 diagnosticOnly(fixture,requests,'background_window_host_changed');assert.equal(fixture.calls.updates.length,updates);assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);
});
}

test('an empty authoritative local pool leaves stale session GPT tabs in the daily browser',async()=>{
 const options=ownedOptions();options.local.bridgePool=[];const fixture=harness(options);await fixture.api().ensure();
 assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[11,12]);
 for(const tabId of [20,21,22])assert.equal(fixture.tabs.get(tabId).windowId,1);
 assert.deepEqual(fixture.local.bridgePool,[]);assert.equal(fixture.calls.creates.length,0);
});

test('only IDs with saved provider origin proof are moved; matching user websites are left alone',async()=>{
 const options=ownedOptions();options.tabs.find(tab=>tab.id===11).url='https://example.com/';
 const fixture=harness(options);await fixture.api().ensure();
 assert.equal(fixture.tabs.get(11).windowId,1);assert.equal(fixture.tabs.get(98).windowId,1);
 assert.deepEqual(fixture.local.bridgeBackgroundWindowHealth.missing_tab_ids,[11]);
 await assert.rejects(fixture.api().ensureOwnedTab(98,'deepseek'),/background_window_tab_unproven/);
 assert.equal(fixture.calls.creates.length,0);
});

 test('a saved host in a window with a personal tab is never hidden or moved',async()=>{
  const fixture=harness({local:{bridgeBackgroundWindow:{windowId:1,hostTabId:50}},tabs:[{id:50,windowId:1,url:hostUrl},{id:99,windowId:1,url:'https://example.com/',active:true}]});
  await assert.rejects(fixture.api().ensure(),/background_window_mixed_tabs/);
  diagnosticOnly(fixture,0,'background_window_mixed_tabs');assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.updates.length,0);assert.equal(fixture.calls.windows.length,0);
  assert.equal(fixture.tabs.get(99).active,true);
 });

test('a saved host dragged into a personal window is refused without hiding that window',async()=>{
 const fixture=harness({local:{bridgeBackgroundWindow:{windowId:7,hostTabId:50}},tabs:[{id:50,windowId:1,url:hostUrl},{id:99,windowId:1,url:'https://example.com/'}]});
 await assert.rejects(fixture.api().ensure(),/background_window_host_changed/);
 diagnosticOnly(fixture,0,'background_window_host_changed');assert.equal(fixture.calls.windows.length,0);assert.equal(fixture.calls.updates.length,0);assert.equal(fixture.calls.moves.length,0);
});

test('a proven tab in a popup source is not recreated to bypass the normal-window API constraint',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl},tabs:[{id:11,windowId:1,url:dsUrl}],windows:[{id:1,type:'popup',incognito:false}]});
 await assert.rejects(fixture.api().ensure(),/background_window_source_unsupported/);
 assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.tabs.get(11).windowId,1);
});

for(const [name,change] of [
 ['visible despite minimized',{visible:true}],
 ['missing native verification',{verified:undefined}],
 ['wrong native tab identity',{host_tab_id:999}],
 ['wrong unique title',{title:'WeChatTwin Background '+'b'.repeat(32)}]
]){
 test('native hide response '+name+' blocks migration and never claims hidden success',async()=>{
  const options=ownedOptions();options.reply=(kind,reply)=>{if(kind==='hide')Object.assign(reply,change);};
  const fixture=harness(options);await assert.rejects(fixture.api().ensure(),/background_window_not_hidden/);
  assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.creates.length,0);
  assert.equal(fixture.local.bridgeBackgroundWindowHealth.hidden,false);assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,false);
 });
}

test('a backend-issued title must match the unique nonce format before any activation or migration',async()=>{
 const options=ownedOptions();options.reply=(kind,reply)=>{if(kind==='register')reply.title='Google Chrome';};
 const fixture=harness(options);await assert.rejects(fixture.api().ensure(),/background_window_registration_rejected/);
 assert.equal(fixture.calls.titles.length,0);assert.equal(fixture.calls.updates.length,0);assert.equal(fixture.calls.moves.length,0);
});

test('native caption propagation is retried with exactly the same identity and no extra window',async()=>{
 let attempts=0;const options=ownedOptions();
 options.reply=(kind,reply)=>{if(kind==='hide'&&++attempts<=2)Object.assign(reply,{ok:false,hidden:false,visible:null,verified:false,error:attempts===1?'owned_window_missing':'owned_window_title_mismatch'});};
 const fixture=harness(options);await fixture.api().ensure();
 const hides=fixture.calls.requests.filter(call=>call.kind==='hide');assert.equal(hides.length,4);
 assert.ok(hides.every(hide=>hide.bodyText===hides[0].bodyText));assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);
});

for(const code of ['owned_window_ambiguous','owned_window_identity_changed','background_window_manual_reveal']){
 test('native identity/reveal failure '+code+' is never retried as a caption delay',async()=>{
  const options=ownedOptions();options.reply=(kind,reply)=>{if(kind==='hide')Object.assign(reply,{ok:false,hidden:false,visible:null,verified:false,error:code});};
  const fixture=harness(options);await assert.rejects(fixture.api().ensure());
  assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide').length,1);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.creates.length,0);
 });
}

test('without a token the extension does not open a window or touch model pages',async()=>{
 const options=ownedOptions();options.local.token='';const fixture=harness(options);
 await assert.rejects(fixture.api().ensure(),/background_window_token_required/);
 assert.equal(fixture.calls.windows.length+fixture.calls.creates.length+fixture.calls.moves.length+fixture.calls.requests.length,0);
});

test('closed owned tabs stay recorded and are not rediscovered by URL or automatically replaced',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,deepseekConversations:{known:{url:dsUrl}}},tabs:[{id:98,windowId:1,url:dsUrl}]});
 await fixture.api().ensure();
 await assert.rejects(fixture.api().ensureOwnedTab(11,'deepseek'),/background_window_tab_unproven/);
 await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_model_capacity/);
 assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.local.deepseekOwnedTab,11);assert.equal(fixture.tabs.get(98).windowId,1);
});

test('first model page is created inside the verified hidden window and stays inactive',async()=>{
 const fixture=harness();const created=await fixture.api().createModelTab({url:dsHome});
 const state=fixture.local.bridgeBackgroundWindow;
 assert.equal(created.windowId,state.windowId);assert.equal(created.active,false);
 assert.deepEqual(fixture.calls.creates,[{url:dsHome,windowId:state.windowId,active:false}]);
 assert.equal(fixture.tabs.get(state.hostTabId).active,true);
 Object.assign(fixture.local,{deepseekOwnedTab:created.id,deepseekOwnedUrl:dsHome});
 await fixture.api().ensureOwnedTab(created.id,'deepseek');
 assert.deepEqual(fixture.session.bridgeBackgroundPendingTabs,[]);
 await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_model_capacity/);
 assert.equal(fixture.calls.creates.length,1);
});

test('a newly created exact tab can finish its pending home navigation without losing its ownership proof',async()=>{
 let reads=0;
 const fixture=harness({onCreate(tab){tab.pendingUrl=tab.url;tab.url='';},onGet(tab){if(tab.pendingUrl&&++reads===2){tab.url=tab.pendingUrl;delete tab.pendingUrl;}}});
 const created=await fixture.api().createModelTab({url:dsHome});
 assert.equal(created.url,dsHome);assert.equal(fixture.session.bridgeBackgroundPendingTabs.length,1);
 Object.assign(fixture.local,{deepseekOwnedTab:created.id,deepseekOwnedUrl:dsHome});
 await fixture.api().ensureOwnedTab(created.id,'deepseek');
 assert.deepEqual(fixture.session.bridgeBackgroundPendingTabs,[]);
 assert.equal(fixture.calls.creates.length,1);assert.equal(fixture.calls.windows.length,1);
});

test('an arbitrary pending tab is not adopted from a matching provider URL',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsHome},tabs:[{id:11,windowId:1,url:'',pendingUrl:dsHome}]});
 await fixture.api().ensure();
 await assert.rejects(fixture.api().ensureOwnedTab(11,'deepseek'),/background_window_tab_unproven/);
 assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.creates.length,0);
});

test('saved DeepSeek and Doubao tab IDs retain origin proof through login redirects',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsHome,doubaoOwnedTab:12,doubaoOwnedUrl:duoHome},tabs:[{id:11,windowId:1,url:'https://chat.deepseek.com/sign_in'},{id:12,windowId:1,url:'https://www.doubao.com/passport/login'}]});
 await fixture.api().ensure();assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[11,12]);
 assert.equal(fixture.calls.creates.length,0);
});

test('Doubao has one page and the GPT pool cannot exceed three',async()=>{
 const fixture=harness();const doubao=await fixture.api().createModelTab({url:duoHome});
 Object.assign(fixture.local,{doubaoOwnedTab:doubao.id,doubaoOwnedUrl:duoHome});
 await assert.rejects(fixture.api().createModelTab({url:duoHome}),/background_window_model_capacity/);
 fixture.local.bridgePool=[];
 for(let index=0;index<3;index++){
  const tab=await fixture.api().createModelTab({url:gptHome});
  fixture.local.bridgePool.push({tabId:tab.id,slotId:index,url:gptHome,key:'contact-'+index,turns:0});
 }
 await assert.rejects(fixture.api().createModelTab({url:gptHome}),/background_window_model_capacity/);
 assert.equal(fixture.calls.creates.length,4);assert.equal(fixture.calls.windows.length,1);
});

test('a remembered conversation without an owned ID blocks replacement creation',async()=>{
 const fixture=harness({local:{deepseekConversations:{known:{url:dsUrl}}}});
 await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_model_capacity/);
 assert.equal(fixture.calls.creates.length,0);
});

test('failed post-creation hiding retains the pending ID across worker restart instead of duplicating the page',async()=>{
 let hides=0;const fixture=harness({reply(kind,reply){if(kind==='hide'&&++hides===3)reply.verified=false;}});
 await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_not_hidden/);
 const tabId=fixture.session.bridgeBackgroundPendingTabs[0].tabId;
 fixture.load();await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_model_capacity/);
 assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,1);assert.equal(fixture.tabs.get(tabId).url,dsHome);
});

test('explicit release of an adopted GPT slot does not resurrect it from pending creation records',async()=>{
 const fixture=harness();const created=await fixture.api().createModelTab({url:gptHome});
 fixture.local.bridgePool=[{tabId:created.id,slotId:0,url:gptHome,key:'old',turns:1}];
 await fixture.api().ensureOwnedTab(created.id,'chatgpt');assert.deepEqual(fixture.session.bridgeBackgroundPendingTabs,[]);
 fixture.local.bridgePool=[];fixture.tabs.get(created.id).windowId=1;
 const before=fixture.calls.moves.length;await fixture.api().ensure();assert.equal(fixture.calls.moves.length,before);assert.equal(fixture.tabs.get(created.id).windowId,1);
});

for(const url of ['https://example.com/','https://chatgpt.com/?temporary-chat=false',dsUrl,duoUrl,'https://www.doubao.com/chat123']){
 test('creation wrapper rejects a non-home or non-model URL '+url,async()=>{
  const fixture=harness();await assert.rejects(fixture.api().createModelTab({url}),/background_window_creation_url_rejected/);
  assert.equal(fixture.calls.windows.length+fixture.calls.creates.length,0);
 });
}

test('ownership is rechecked after hiding, so a model tab navigating outside its provider is not moved',async()=>{
 const options=ownedOptions();let once=false;
 options.onHide=({tabs})=>{if(!once){once=true;tabs.get(11).url='https://example.com/';}};
 const fixture=harness(options);await assert.rejects(fixture.api().ensure(),/background_window_ownership_changed/);
 assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.tabs.get(11).windowId,1);assert.equal(fixture.calls.creates.length,0);
});

test('manual reveal pauses before selecting the controller or moving pages, then explicit resume restores hiding',async()=>{
 const options=ownedOptions();options.local.bridgeBackgroundWindow={windowId:100,hostTabId:50};
 options.windows=[{id:1,type:'normal',incognito:false},{id:100,type:'normal',incognito:false}];options.tabs.push({id:50,windowId:100,url:hostUrl});options.manualReveal=true;
 const fixture=harness(options);await assert.rejects(fixture.api().ensure(),/background_window_manual_reveal/);
 assert.equal(fixture.calls.updates.length,0);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.titles.length,0);assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide').length,0);
 fixture.options.manualReveal=false;await fixture.api().ensure();
 assert.equal(fixture.calls.moves.length,5);assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);assert.equal(fixture.calls.windows.length,0);
});

test('accidental activation of an owned model tab is debounced and returns to the verified hidden controller',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();
 const handlers=fixture.context.chrome.tabs.onActivated.listeners;
 const before=fixture.calls.requests.filter(call=>call.kind==='register').length;
 fixture.tabs.get(state.hostTabId).active=false;fixture.tabs.get(11).active=true;
 for(let i=0;i<10;i++)for(const handler of handlers)handler({tabId:11,windowId:state.windowId});
 await new Promise(resolve=>setTimeout(resolve,200));
 assert.equal(fixture.calls.requests.filter(call=>call.kind==='register').length,before+1);
 assert.equal(fixture.tabs.get(state.hostTabId).active,true);assert.equal(fixture.tabs.get(11).active,false);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.calls.creates.length,0);
 const after=fixture.calls.requests.length;
 for(const handler of handlers){handler({tabId:state.hostTabId,windowId:state.windowId});handler({tabId:99,windowId:1});}
 await new Promise(resolve=>setTimeout(resolve,150));assert.equal(fixture.calls.requests.length,after);
});

test('owned model activation during manual reveal does not select the controller or hide the user login page',async()=>{
 const fixture=harness(ownedOptions());const state=await fixture.api().ensure();
 fixture.options.manualReveal=true;fixture.tabs.get(state.hostTabId).active=false;fixture.tabs.get(11).active=true;
 const before=fixture.calls.updates.length,hides=fixture.calls.requests.filter(call=>call.kind==='hide').length;
 for(const handler of fixture.context.chrome.tabs.onActivated.listeners)handler({tabId:11,windowId:state.windowId});
 await new Promise(resolve=>setTimeout(resolve,200));
 assert.equal(fixture.calls.updates.length,before);assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide').length,hides);
 assert.equal(fixture.tabs.get(11).active,true);assert.equal(fixture.tabs.get(state.hostTabId).active,false);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.error,'background_window_manual_reveal');
});

test('GPT acquisition fails on a lost saved pool tab without allocating a replacement',async()=>{
 const fixture=harness({session:{bridgePool:[{tabId:20,slotId:0,url:gptHome,key:'known',turns:1}]},tabs:[{id:98,windowId:1,url:gptHome}]});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'background.js'),'utf8'),fixture.context,{filename:'background.js'});
 await assert.rejects(fixture.context.acquire('known'),/chatgpt_owned_tab_unavailable/);
 await fixture.api().ensure();
 assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.session.bridgePool[0].tabId,20);assert.equal(fixture.tabs.get(98).windowId,1);
});

test('HTTP host title injection is limited to its exact top frame and never messages an extension page',async()=>{
 const fixture=harness();const state=await fixture.api().ensure();
 assert.equal(fixture.calls.messages.length,0);
 for(const script of fixture.calls.scripts){
  assert.deepEqual(script.target,{tabId:state.hostTabId,frameIds:[0]});assert.deepEqual(script.args,[hostUrl,title]);
 }
 const func=fixture.calls.scripts[0].func;
 for(const url of [retiredHostUrl,'https://example.org/',hostUrl+'?extra=true',hostUrl+'/other',hostUrl+'#extra']){
  const parsed=new URL(url),document={title:'Unchanged'};
  const result=vm.runInNewContext('('+func+')(...args)',{location:{href:url,origin:parsed.origin,pathname:parsed.pathname},document,args:[hostUrl,title]});
  assert.equal(result.ok,false);assert.equal(document.title,'Unchanged');
 }
});

test('saved legacy host migrates only its proven tab ID to HTTP and retains every owned model and contact map',async()=>{
 const options=ownedOptions();options.local.bridgeBackgroundWindow={windowId:100,hostTabId:50};
 options.windows=[{id:1,type:'normal',incognito:false},{id:100,type:'normal',incognito:false}];
 for(const tab of options.tabs)if([11,12,20,21,22].includes(tab.id))tab.windowId=100;
 options.tabs.push({id:50,windowId:100,url:retiredHostUrl});
 const fixture=harness(options),maps=structuredClone({deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations,pool:fixture.session.bridgePool});
 await fixture.api().ensure();
 assert.deepEqual(fixture.calls.updates.filter(call=>call.url),[{tabId:50,url:hostUrl}]);
 assert.equal(fixture.calls.requests[0].kind,'register');assert.equal(fixture.calls.windows.length,0);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.messages.length,0);
 assert.equal(fixture.tabs.get(50).url,hostUrl);assert.equal(fixture.tabs.get(99).windowId,1);
 assert.deepEqual({deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations,pool:fixture.local.bridgePool},maps);
 assert.ok(fixture.calls.scripts.every(call=>call.target.tabId===50&&call.args[0]===hostUrl));
 assert.equal(fixture.local.bridgeBackgroundWindow.restoreHostUrl,undefined);
});

test('ordinary public HTTP and unclaimed legacy host pages remain untouched while a new dedicated controller is created',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl},tabs:[{id:11,windowId:1,url:dsUrl},{id:50,windowId:1,url:hostUrl,active:true},{id:51,windowId:1,url:retiredHostUrl},{id:99,windowId:1,url:'https://example.org/'}]});
 const state=await fixture.api().ensure();
 assert.equal(fixture.calls.windows.length,1);assert.notEqual(state.hostTabId,50);assert.notEqual(state.hostTabId,51);
 assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[11]);
 for(const tabId of [50,51,99])assert.equal(fixture.tabs.get(tabId).windowId,1);
 assert.equal(fixture.tabs.get(50).active,true);assert.equal(fixture.tabs.get(51).url,retiredHostUrl);
 assert.ok(fixture.calls.queries.every(query=>query.windowId!==undefined));
 assert.ok(fixture.calls.scripts.every(call=>call.target.tabId===state.hostTabId));
});

test('a newly created host with a pending HTTP navigation saves its exact identity before registration and commit',async()=>{
 let reads=0;
 const fixture=harness({onWindowCreate(tab){tab.pendingUrl=tab.url;tab.url='';},onGet(tab){if(tab.pendingUrl===hostUrl&&++reads===2){tab.url=hostUrl;delete tab.pendingUrl;}},onUpdate(tab,properties){if(properties.url===hostUrl){tab.url='';tab.pendingUrl=hostUrl;}}});
 const state=await fixture.api().ensure();
 assert.equal(fixture.calls.windows.length,1);assert.equal(fixture.tabs.get(state.hostTabId).url,hostUrl);
 const record=fixture.calls.storageWrites.find(write=>write.area==='local'&&write.values.bridgeBackgroundWindow);
 assert.equal(record.values.bridgeBackgroundWindow.hostTabId,state.hostTabId);assert.equal(record.values.bridgeBackgroundWindow.windowId,state.windowId);
 assert.equal(fixture.local.bridgeBackgroundWindow.restoreHost,undefined);assert.equal(fixture.local.bridgeBackgroundWindowHealth.verified,true);
});

test('authenticated recovery recreates only closed saved IDs, preserves contact maps and clears every expired GPT context',async()=>{
 const options=ownedOptions();options.recoveryNonce=recoveryNonce;
 options.local.deepseekOwnedUrl=dsHome;options.local.doubaoOwnedUrl=duoHome;
 const newestDs='https://chat.deepseek.com/a/chat/s/newest-owned',newestDuo='https://www.doubao.com/chat/54321';
 options.local.deepseekConversations.newer={url:newestDs,name:'C',used:99,lastTurnId:'known-c'};
 options.local.doubaoConversations.newer={url:newestDuo,name:'D',lastTurnId:'known-d'};
 options.session.bridgePool[1].url='https://chatgpt.com/c/closed-owned?temporary-chat=true';options.session.bridgePool[1].failed=true;options.session.bridgePool[1].navigationOriginBefore=123;
 options.tabs=options.tabs.filter(tab=>![11,12,20,21].includes(tab.id));options.tabs.push({id:85,windowId:1,url:newestDuo});
 const fixture=harness(options),maps=structuredClone({deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations}),live=structuredClone(fixture.session.bridgePool[2]);
 const state=await fixture.api().ensure();
 assert.deepEqual(fixture.calls.creates.map(call=>call.url),[newestDs,newestDuo,gptHome,gptHome]);
 assert.ok(fixture.calls.creates.every(call=>call.windowId===state.windowId&&call.active===false));
 assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[22]);
 assert.equal(fixture.tabs.get(98).windowId,1);assert.equal(fixture.tabs.get(85).windowId,1);
 assert.deepEqual({deepseek:fixture.local.deepseekConversations,doubao:fixture.local.doubaoConversations},maps);
 assert.equal(fixture.local.deepseekOwnedUrl,newestDs);assert.equal(fixture.local.doubaoOwnedUrl,newestDuo);
 assert.equal(fixture.local.bridgePool.length,3);assert.deepEqual(fixture.local.bridgePool[2],live);
 for(const [index,slot] of fixture.local.bridgePool.slice(0,2).entries()){
  assert.equal(slot.slotId,index);assert.notEqual(slot.tabId,20+index);assert.equal(slot.url,gptHome);assert.equal(slot.turns,0);assert.equal(slot.bootstrapIdle,true);
  for(const name of ['key','failed','navigationOriginBefore'])assert.equal(Object.hasOwn(slot,name),false);
 }
 assert.deepEqual(fixture.local.bridgePool,fixture.session.bridgePool);
 const acknowledgement=fixture.calls.requests.filter(call=>call.kind==='recovered');assert.equal(acknowledgement.length,1);
 assert.deepEqual(acknowledgement[0].body,{nonce:recoveryNonce,window_id:state.windowId,host_tab_id:state.hostTabId});
 assert.equal(acknowledgement[0].headers.Authorization,'Bearer offline-test-token');assert.equal(fixture.local.bridgeBackgroundRecoveryApplied,recoveryNonce);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.recovery_pending,false);assert.equal(fixture.local.bridgeBackgroundWindowHealth.ready,true);
 for(const ownedKey of ['deepseekOwnedTab','doubaoOwnedTab']){
  assert.ok(fixture.calls.storageWrites.some(write=>write.area==='local'&&write.values[ownedKey]&&write.values.bridgeBackgroundPendingTabs.some(item=>item.tabId===write.values[ownedKey])));
 }
 await fixture.api().ensure();assert.equal(fixture.calls.creates.length,4);
});

test('acknowledgement failure and extension reload cannot duplicate atomically recorded recovery pages',async()=>{
 let acknowledgements=0;
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,deepseekConversations:{known:{url:dsUrl}}},recoveryNonce,reply(kind,reply){if(kind==='recovered'&&++acknowledgements===1)reply.ok=false;}});
 await fixture.api().ensure();const tabId=fixture.local.deepseekOwnedTab;
 assert.equal(fixture.calls.creates.length,1);assert.equal(fixture.local.bridgeBackgroundRecoveryApplied,undefined);assert.equal(fixture.local.bridgeBackgroundWindowHealth.recovery_pending,true);
 for(const key of Object.keys(fixture.session))delete fixture.session[key];fixture.load();
 await fixture.api().ensure();
 assert.equal(fixture.calls.creates.length,1);assert.equal(fixture.local.deepseekOwnedTab,tabId);assert.equal(fixture.local.bridgeBackgroundRecoveryApplied,recoveryNonce);assert.equal(acknowledgements,2);
});

test('one provider creation failure leaves recovery pending and lets another owned provider recover without duplication',async()=>{
 let fail=true;
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,doubaoOwnedTab:12,doubaoOwnedUrl:duoUrl},recoveryNonce,beforeCreate(properties){if(fail&&properties.url===dsUrl)throw Error('temporary create failure');}});
 await fixture.api().ensure();const duoId=fixture.local.doubaoOwnedTab;
 assert.equal(fixture.local.deepseekOwnedTab,11);assert.notEqual(duoId,12);assert.equal(fixture.calls.requests.filter(call=>call.kind==='recovered').length,0);
 assert.equal(fixture.local.bridgeBackgroundWindowHealth.ready,true);assert.equal(fixture.local.bridgeBackgroundWindowHealth.recovery_pending,true);assert.equal(fixture.local.bridgeBackgroundWindowHealth.model_errors.deepseek,'background_window_recovery_failed');
 assert.equal((await fixture.api().ensureOwnedTab(duoId,'doubao')).id,duoId);
 fail=false;await fixture.api().ensure();
 assert.notEqual(fixture.local.deepseekOwnedTab,11);assert.equal(fixture.local.doubaoOwnedTab,duoId);
 assert.equal([...fixture.tabs.values()].filter(tab=>tab.url===duoUrl).length,1);assert.equal(fixture.local.bridgeBackgroundRecoveryApplied,recoveryNonce);
});

test('a live saved ID navigated away is never recreated by a recovery request',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,doubaoOwnedTab:12,doubaoOwnedUrl:duoUrl},tabs:[{id:11,windowId:1,url:'https://example.org/',active:true}],recoveryNonce});
 await fixture.api().ensure();
 assert.deepEqual(fixture.calls.creates.map(call=>call.url),[duoUrl]);assert.equal(fixture.local.deepseekOwnedTab,11);assert.equal(fixture.tabs.get(11).windowId,1);assert.equal(fixture.tabs.get(11).active,true);
 assert.equal(fixture.calls.requests.filter(call=>call.kind==='recovered').length,0);assert.equal(fixture.local.bridgeBackgroundWindowHealth.ready,true);
});

for(const nonce of ['', 'not-a-backend-issued-nonce', null, 123]){
 test('malformed recovery nonce '+String(nonce)+' causes no model creation or migration',async()=>{
  const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl},recoveryNonce:nonce});
  await assert.rejects(fixture.api().ensure(),/background_window_recovery_rejected/);
  assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.scripts.length,0);assert.equal(fixture.calls.requests.filter(call=>call.kind==='hide'||call.kind==='recovered').length,0);
 });
}

test('a failed stale GPT migration does not gate valid DeepSeek and Doubao window preparation',async()=>{
 const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,doubaoOwnedTab:12,doubaoOwnedUrl:duoUrl},tabs:[{id:11,windowId:1,url:dsUrl},{id:12,windowId:1,url:duoUrl}]});
 fixture.context.migrateChatgptPool=async()=>{throw Error('chatgpt_migration_tab_unavailable');};
 await fixture.api().ensure();
 assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[11,12]);assert.equal(fixture.local.bridgeBackgroundWindowHealth.ready,true);assert.equal(fixture.local.bridgeBackgroundWindowHealth.model_errors.chatgpt,'chatgpt_migration_tab_unavailable');
 assert.equal((await fixture.api().ensureOwnedTab(11,'deepseek')).id,11);
});

for(const contained of [true,false]){
 test('changed GPT page '+(contained?'inside its saved container stays fenced':'in an ordinary window is never moved')+' while DeepSeek remains available',async()=>{
  const fixture=harness({local:{bridgeBackgroundWindow:{windowId:100,hostTabId:50},deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,bridgePool:[{tabId:20,slotId:0,url:gptHome,key:'known',turns:1}]},windows:[{id:1,type:'normal',incognito:false},{id:100,type:'normal',incognito:false}],tabs:[{id:50,windowId:100,url:hostUrl},{id:11,windowId:1,url:dsUrl},{id:20,windowId:contained?100:1,url:'https://chatgpt.com/auth/login',active:!contained}]});
  await fixture.api().ensure();
  assert.deepEqual(fixture.calls.moves.map(call=>call.tabId),[11]);assert.equal(fixture.tabs.get(20).windowId,contained?100:1);assert.equal(fixture.calls.creates.length,0);assert.equal(fixture.local.bridgeBackgroundWindowHealth.ready,true);
  await assert.rejects(fixture.api().ensureOwnedTab(20,'chatgpt'),/background_window_tab_unproven/);assert.equal((await fixture.api().ensureOwnedTab(11,'deepseek')).id,11);
 });
}

test('initial pending model proof survives cleared SESSION storage and blocks duplicate creation after a hide failure',async()=>{
 let hides=0;const fixture=harness({onCreate(tab){tab.pendingUrl=tab.url;tab.url='';},reply(kind,reply){if(kind==='hide'&&++hides===3)reply.verified=false;}});
 await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_not_hidden/);
 const original=fixture.local.bridgeBackgroundPendingTabs[0];assert.ok(original);
 for(const key of Object.keys(fixture.session))delete fixture.session[key];fixture.load();
 await assert.rejects(fixture.api().createModelTab({url:dsHome}),/background_window_model_capacity/);
 assert.equal(fixture.calls.creates.length,1);assert.equal(fixture.local.bridgeBackgroundPendingTabs[0].tabId,original.tabId);assert.equal(fixture.tabs.get(original.tabId).windowId,original.windowId);
});

test('all three provider entry points recover before they capture stored ownership',async()=>{
 for(const provider of ['deepseek','doubao','chatgpt']){
  const fixture=harness({local:{deepseekOwnedTab:11,deepseekOwnedUrl:dsUrl,doubaoOwnedTab:12,doubaoOwnedUrl:duoUrl,bridgePool:[{tabId:20,slotId:0,url:gptHome,key:'expired',turns:8}]},recoveryNonce});
  const name=provider==='chatgpt'?'background.js':provider+'_background.js';
  vm.runInContext(fs.readFileSync(path.join(__dirname,name),'utf8'),fixture.context,{filename:name});
  if(provider==='chatgpt'){
   const acquired=await fixture.context.acquire('new-contact');assert.notEqual(acquired.slot.tabId,20);assert.equal(acquired.reused,false);assert.equal(acquired.slot.key,'new-contact');
  }else await fixture.context[provider+'Pump']();
  assert.notEqual(fixture.local.deepseekOwnedTab,11);assert.notEqual(fixture.local.doubaoOwnedTab,12);assert.notEqual(fixture.local.bridgePool[0].tabId,20);
  assert.equal(fixture.calls.creates.length,3);assert.equal(fixture.local.bridgeBackgroundRecoveryApplied,recoveryNonce);
 }
});

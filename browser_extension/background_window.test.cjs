const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const hostUrl='chrome-extension://bridge-test/background_host.html';
const title='WeChatTwin Background '+ 'a'.repeat(32);
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
 const calls={windows:[],creates:[],moves:[],updates:[],requests:[],titles:[],queries:[]};
 let nextWindow=100,nextTab=1000;
 const storage=target=>({
  async get(keys){
   if(typeof keys==='string')return {[keys]:structuredClone(target[keys])};
   if(Array.isArray(keys))return Object.fromEntries(keys.map(key=>[key,structuredClone(target[key])]));
   return structuredClone(target);
  },
  async set(values){Object.assign(target,structuredClone(values));},
  async remove(key){delete target[key];}
 });
 const chrome={
  storage:{local:storage(local),session:storage(session),onChanged:listener()},
  runtime:{id:'bridge-test',getURL:file=>'chrome-extension://bridge-test/'+file,onStartup:listener(),onInstalled:listener(),onMessage:listener(),openOptionsPage(){},
   async sendMessage(message){
    calls.titles.push(structuredClone(message));
    if(options.rejectHost)throw Error('host not ready');
    const tab=tabs.get(message.host_tab_id);
    if(!tab||tab.url!==hostUrl||tab.windowId!==message.window_id)return {ok:false};
    tab.title=message.title;
    return {ok:true,window_id:tab.windowId,host_tab_id:tab.id,title:tab.title};
   }
  },
  windows:{
   async get(windowId){if(!windows.has(windowId))throw Error('window closed');return structuredClone(windows.get(windowId));},
   async create(properties){
    calls.windows.push(structuredClone(properties));const window={id:nextWindow++,type:properties.type,incognito:false,state:properties.state,focused:properties.focused};windows.set(window.id,window);
    const tab={id:nextTab++,windowId:window.id,url:properties.url,active:true};tabs.set(tab.id,tab);
    return structuredClone({...window,tabs:[tab]});
   }
  },
  tabs:{
   onUpdated:listener(),onAttached:listener(),onActivated:listener(),
   async get(tabId){if(!tabs.has(tabId))throw Error('tab closed');options.onGet?.(tabs.get(tabId));return structuredClone(tabs.get(tabId));},
   async query(query){calls.queries.push(structuredClone(query));return [...tabs.values()].filter(tab=>query.windowId===undefined||tab.windowId===query.windowId).map(tab=>structuredClone(tab));},
   async move(tabId,properties){calls.moves.push({tabId,...structuredClone(properties)});const tab=tabs.get(tabId);if(!tab)throw Error('closed');tab.windowId=properties.windowId;tab.active=false;return structuredClone(tab);},
   async create(properties){calls.creates.push(structuredClone(properties));const tab={id:nextTab++,...structuredClone(properties)};tabs.set(tab.id,tab);options.onCreate?.(tab);return structuredClone(tab);},
   async update(tabId,properties){
    calls.updates.push({tabId,...structuredClone(properties)});const tab=tabs.get(tabId);if(!tab)throw Error('closed');
    if(properties.active)for(const other of tabs.values())if(other.windowId===tab.windowId)other.active=false;
    Object.assign(tab,structuredClone(properties));return structuredClone(tab);
   }
  },
  alarms:{create(){},onAlarm:listener()},action:{onClicked:listener()}
 };
 async function fetch(url,request={}){
  if(url.endsWith('/chatgpt-bootstrap.local.json')||url.endsWith('/gpt-migration.local.json'))return {async json(){return {};}};
  const body=JSON.parse(request.body),kind=url.split('/').at(-1);
  calls.requests.push({kind,body,...request,bodyText:request.body});
  const reply={ok:true,window_id:body.window_id,host_tab_id:body.host_tab_id,title,manual_reveal:options.manualReveal===true};
  if(kind==='hide'){
   if(options.manualReveal){Object.assign(reply,{ok:false,hidden:false,visible:null,verified:false,error:'background_window_manual_reveal'});}
   else Object.assign(reply,{hidden:true,visible:false,verified:true});
   options.onHide?.({tabs,windows,calls,reply});
  }
  options.reply?.(kind,reply,body);
  return {ok:options.httpOk!==false,async json(){return structuredClone(reply);}};
 }
 const context=vm.createContext({chrome,fetch,URL,Date,AbortController,setTimeout,clearTimeout,console:{warn(){}},WebSocket:class{constructor(){}},
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

for(const [name,record] of [['saved host',true],['exact host URL without a saved record',false]]){
 test('a '+name+' in a window with a personal tab is never hidden or moved',async()=>{
  const fixture=harness({local:record?{bridgeBackgroundWindow:{windowId:1,hostTabId:50}}:{},tabs:[{id:50,windowId:1,url:hostUrl},{id:99,windowId:1,url:'https://example.com/',active:true}]});
  await assert.rejects(fixture.api().ensure(),/background_window_mixed_tabs/);
  assert.equal(fixture.calls.requests.length,0);assert.equal(fixture.calls.moves.length,0);assert.equal(fixture.calls.updates.length,0);assert.equal(fixture.calls.windows.length,0);
  assert.equal(fixture.tabs.get(99).active,true);
 });
}

test('a saved host dragged into a personal window is refused without hiding that window',async()=>{
 const fixture=harness({local:{bridgeBackgroundWindow:{windowId:7,hostTabId:50}},tabs:[{id:50,windowId:1,url:hostUrl},{id:99,windowId:1,url:'https://example.com/'}]});
 await assert.rejects(fixture.api().ensure(),/background_window_mixed_tabs/);
 assert.equal(fixture.calls.requests.length,0);assert.equal(fixture.calls.windows.length,0);
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

test('controller accepts only a bridge title addressed to its exact window and tab',async()=>{
 let onMessage,observer;
 const document={title:'WeChatTwin Background',querySelector(){return {};}};
 const context=vm.createContext({document,MutationObserver:class{constructor(fn){observer=fn;}observe(){}},chrome:{runtime:{id:'bridge-test',onMessage:{addListener(fn){onMessage=fn;}}},tabs:{async getCurrent(){return {id:50,windowId:100};}}}});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'background_host.js'),'utf8'),context,{filename:'background_host.js'});
 const receive=(message,sender={id:'bridge-test'})=>new Promise(resolve=>{const kept=onMessage(message,sender,resolve);if(kept!==true)resolve(undefined);});
 const message={type:'wechat-background-host-title',window_id:100,host_tab_id:50,title};
 assert.equal((await receive(message)).ok,true);assert.equal(document.title,title);
 document.title='foreign title';observer();assert.equal(document.title,title);
 assert.equal((await receive({...message,host_tab_id:51})).ok,false);assert.equal(document.title,title);
 assert.equal(await receive({...message,title:'Google Chrome'}),undefined);assert.equal(document.title,title);
 assert.equal(await receive(message,{id:'other-extension'}),undefined);assert.equal(document.title,title);
});

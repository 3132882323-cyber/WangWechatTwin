const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const rootUrl='https://chatgpt.com/?temporary-chat=true';
const nonce='recover-gpt-20261007-explicit';

function harness(options={}){
 const local=options.local||{};
 const session=options.session||{};
 const calls={queries:0,gets:0,scripts:0,creates:0,updates:0,poolWrites:0,localPoolWrites:0,sessionPoolWrites:0,network:0,windowPools:[]};
 let tabs=options.tabs||[{id:32,url:rootUrl}];
 const bootstrap={recovery_id:nonce,owned_url:rootUrl,...options.bootstrap};
 const migration=options.migration||{};
 const page={readyState:'complete',editor:true,draft:'',messageSelector:'',url:rootUrl,owner:'',...options.page};
 const document={
  get readyState(){return page.readyState;},
  documentElement:{dataset:{get wechatBridgeOwner(){return page.owner;}}},
  querySelector(selector){
   if(selector==='[role="textbox"][contenteditable="true"]')return page.editor?{textContent:page.draft}:null;
   if(page.messageSelector&&selector.includes(page.messageSelector))return {};
   return null;
  }
 };
 const storage=(target,area)=>({
  async get(key){return typeof key==='string'?{[key]:structuredClone(target[key])}:structuredClone(target);},
  async set(values){if('bridgePool' in values){calls.poolWrites++;calls[area+'PoolWrites']++;}Object.assign(target,structuredClone(values));},
  async remove(key){delete target[key];}
 });
 const chrome={
  storage:{local:storage(local,'local'),session:storage(session,'session')},
  alarms:{create(){},onAlarm:{addListener(){}}},
  runtime:{getURL:name=>'extension://'+name,onInstalled:{addListener(){}},openOptionsPage(){}},
  action:{onClicked:{addListener(){}}},
  tabs:{
   async query(){calls.queries++;if(options.queryGate)await options.queryGate;return structuredClone(tabs);},
   async get(id){calls.gets++;const tab=tabs.find(t=>t.id===id);if(!tab)throw Error('closed');return structuredClone(tab);},
   async create(){calls.creates++;throw Error('unexpected tab creation');},
   async update(){calls.updates++;throw Error('unexpected navigation');}
  },
  scripting:{async executeScript({func,args=[]}){
   calls.scripts++;
   if(!func)throw Error('unexpected page-script injection');
   const result=vm.runInNewContext('('+func.toString()+')(...args)',{document,URL,location:{href:page.url,origin:new URL(page.url).origin},args});
   return [{result}];
  }}
 };
 const context=vm.createContext({
  chrome,URL,console:{warn(){}},
  WebSocket:class{constructor(){throw Error('unexpected bridge connection');}},
  async fetch(url){
   if(url==='extension://chatgpt-bootstrap.local.json')return {async json(){return structuredClone(bootstrap);}};
   if(url==='extension://gpt-migration.local.json')return {async json(){return structuredClone(migration);}};
   calls.network++;throw Error('unexpected network request');
  },
  importScripts(file){
   if(file==='pool_core.js')vm.runInContext(fs.readFileSync(path.join(__dirname,file),'utf8'),context,{filename:file});
   else if(file==='background_window.js')context.WechatBackgroundWindow={async ensure(){calls.windowPools.push(structuredClone(local.bridgePool||[]));},async ensureOwnedTab(id){calls.windowPools.push(structuredClone(local.bridgePool||[]));return chrome.tabs.get(id);},startup(){calls.windowPools.push(structuredClone(local.bridgePool||[]));},createModelTab(){calls.creates++;throw Error('unexpected tab creation');}};
   else assert.ok(['deepseek_background.js','doubao_background.js'].includes(file));
  }
 });
 vm.runInContext(fs.readFileSync(path.join(__dirname,'background.js'),'utf8'),context,{filename:'background.js'});
 return {context,local,session,calls,page,migration,setTabs(value){tabs=value;}};
}

test('GPT adopts a single explicit empty page and consumes its idle slot without creating a page',async()=>{
 const fixture=harness();
 await fixture.context.recoverChatgptPage();
 assert.equal(fixture.local.chatgptRecoveryApplied,nonce);
 assert.equal(fixture.local.bridgePool.length,1);
 assert.equal(fixture.local.bridgePool[0].bootstrapIdle,true);
 assert.equal(fixture.session.bridgePool,undefined);
 const acquired=await fixture.context.acquire('synthetic-contact-key');
 assert.equal(acquired.slot.tabId,32);
 assert.equal(acquired.slot.key,'synthetic-contact-key');
 assert.equal(acquired.slot.bootstrapIdle,undefined);
 assert.equal(acquired.reused,false);
 assert.equal(fixture.local.bridgePool[0].key,'synthetic-contact-key');
 assert.equal(fixture.calls.queries,1);
 assert.equal(fixture.calls.scripts,1);
 assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);
});

for(const [name,page] of [
 ['missing editor',{editor:false}],
 ['unfinished page',{readyState:'loading'}],
 ['user draft',{draft:'unsent draft'}],
 ['legacy message',{messageSelector:'[data-message-author-role]'}],
 ['modern user message',{messageSelector:'[data-content-search-unit-key$=":user"]'}],
 ['modern assistant message',{messageSelector:'[data-content-search-unit-key$=":assistant"]'}],
 ['navigation after the tab lookup',{url:'https://chatgpt.com/c/existing?temporary-chat=true'}]
]){
 test('GPT refuses recovery for '+name,async()=>{
  const fixture=harness({page});
  await assert.rejects(fixture.context.recoverChatgptPage(),/chatgpt_recovery_page_not_blank/);
  assert.equal(fixture.local.chatgptRecoveryApplied,undefined);
  assert.equal(fixture.session.bridgePool,undefined);
  assert.equal(fixture.local.bridgePool,undefined);
  assert.equal(fixture.calls.poolWrites+fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);
 });
}

for(const tabs of [[],[{id:32,url:rootUrl},{id:33,url:rootUrl}]]){
 test('GPT refuses recovery when the exact page count is '+tabs.length,async()=>{
  const fixture=harness({tabs});
  await assert.rejects(fixture.context.recoverChatgptPage(),/chatgpt_recovery_page_unavailable/);
  assert.equal(fixture.local.chatgptRecoveryApplied,undefined);
  assert.equal(fixture.calls.scripts+fixture.calls.poolWrites+fixture.calls.network,0);
 });
}

test('GPT shares startup recovery with simultaneous recovery and acquisition',async()=>{
 let release;
 const queryGate=new Promise(resolve=>{release=resolve;});
 const fixture=harness({queryGate});
 const first=fixture.context.recoverChatgptPage();
 const second=fixture.context.recoverChatgptPage();
 const acquired=fixture.context.acquire('concurrent-contact-key');
 assert.equal(first,second);
 release();
 await Promise.all([first,second,acquired]);
 assert.equal(fixture.calls.queries,1);
 assert.equal(fixture.calls.scripts,1);
 assert.equal(fixture.calls.poolWrites,2);
 assert.equal(fixture.local.bridgePool[0].key,'concurrent-contact-key');
 assert.equal(fixture.local.bridgePool[0].bootstrapIdle,undefined);
 assert.equal(fixture.calls.localPoolWrites,2);
 assert.equal(fixture.calls.sessionPoolWrites,0);
 assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);
});

test('GPT does not replay a consumed recovery after a worker restart or closed page',async()=>{
 const fixture=harness();
 await fixture.context.recoverChatgptPage();
 const before=structuredClone(fixture.local.bridgePool);
 const restarted=harness({local:fixture.local,session:{},tabs:[{id:99,url:rootUrl}]});
 await restarted.context.recoverChatgptPage();
 assert.deepEqual(restarted.local.bridgePool,before);
 assert.equal(restarted.session.bridgePool,undefined);
 assert.equal(restarted.calls.queries+restarted.calls.scripts+restarted.calls.poolWrites+restarted.calls.network,0);
});

for(const bootstrap of [{recovery_id:'short'},{owned_url:'https://chatgpt.com/c/existing?temporary-chat=true'}]){
 test('GPT ignores an invalid recovery authorization '+Object.keys(bootstrap)[0],async()=>{
  const fixture=harness({bootstrap});
  await fixture.context.recoverChatgptPage();
  assert.equal(fixture.local.chatgptRecoveryApplied,undefined);
  assert.equal(fixture.calls.queries+fixture.calls.scripts+fixture.calls.poolWrites+fixture.calls.network,0);
 });
}

test('the GPT pool survives extension reload with empty session storage and reuses the same owned contact tab',async()=>{
 const slot={tabId:32,slotId:0,url:rootUrl,key:'durable-contact',turns:4,used:1};
 const fixture=harness({local:{bridgePool:[slot]},session:{bridgePool:[{...slot,tabId:99,key:'stale-session'}]},bootstrap:{recovery_id:'short'}});
 await fixture.context.migrateChatgptPool();
 const reloaded=harness({local:fixture.local,session:{},tabs:[{id:32,url:rootUrl}],bootstrap:{recovery_id:'short'}});
 const acquired=await reloaded.context.acquire('durable-contact');
 assert.equal(acquired.slot.tabId,32);assert.equal(acquired.slot.key,'durable-contact');assert.equal(acquired.reused,true);assert.equal(acquired.slot.turns,4);
 assert.equal(reloaded.local.bridgePool.length,1);assert.equal(reloaded.session.bridgePool,undefined);
 assert.equal(reloaded.calls.creates+reloaded.calls.updates+reloaded.calls.queries+reloaded.calls.network,0);
 assert.equal(reloaded.calls.sessionPoolWrites,0);
});

test('a legacy session pool migrates once to local without changing its IDs or contact mapping',async()=>{
 const pool=[{tabId:32,slotId:0,url:rootUrl,key:'legacy-contact',turns:3,used:123}];
 const fixture=harness({session:{bridgePool:pool},bootstrap:{recovery_id:'short'}});
 await fixture.context.migrateChatgptPool();
 assert.deepEqual(fixture.local.bridgePool,pool);assert.deepEqual(fixture.session.bridgePool,pool);assert.equal(fixture.calls.localPoolWrites,1);assert.equal(fixture.calls.sessionPoolWrites,0);
 delete fixture.session.bridgePool;
 await fixture.context.migrateChatgptPool();assert.deepEqual(fixture.local.bridgePool,pool);assert.equal(fixture.calls.localPoolWrites,1);
});

test('an intentionally empty local pool overrides a stale legacy session pool',async()=>{
 const fixture=harness({local:{bridgePool:[]},session:{bridgePool:[{tabId:99,slotId:0,url:rootUrl,key:'released-contact'}]},bootstrap:{recovery_id:'short'}});
 const pool=await fixture.context.WechatPool.readPool(fixture.context.chrome.storage);
 assert.equal(pool.length,0);assert.equal(fixture.calls.poolWrites,0);assert.deepEqual(fixture.local.bridgePool,[]);
});

const migrationKey='a'.repeat(64),migrationUrl='https://chatgpt.com/c/explicit-owned-page?temporary-chat=true';
const migrationConfig=()=>({migration_id:'explicit-owned-gpt-migration-20261008',slots:[{tabId:261,slotId:0,url:migrationUrl,key:migrationKey,turns:1}]});
const migrationOptions=()=>({migration:migrationConfig(),bootstrap:{recovery_id:'short'},tabs:[{id:261,url:migrationUrl}],page:{url:migrationUrl,owner:migrationKey}});

test('explicit GPT migration merges the exact ID with a durable pool only after owner, temporary URL and empty editor proof',async()=>{
 const options=migrationOptions();const existing={tabId:32,slotId:1,url:rootUrl,key:'b'.repeat(64),turns:7,used:123};options.local={bridgePool:[existing]};
 const fixture=harness(options);await fixture.context.migrateChatgptPool();await fixture.context.recoverChatgptPage();
 assert.equal(fixture.local.chatgptMigrationApplied,options.migration.migration_id);assert.equal(fixture.local.bridgePool.length,2);assert.deepEqual(fixture.local.bridgePool[0],existing);
 assert.equal(fixture.local.bridgePool[1].tabId,261);assert.equal(fixture.local.bridgePool[1].url,migrationUrl);assert.equal(fixture.local.bridgePool[1].key,migrationKey);
 assert.equal(fixture.calls.scripts,1);assert.equal(fixture.calls.localPoolWrites,1);assert.equal(fixture.calls.sessionPoolWrites,0);
 assert.equal(fixture.calls.queries+fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);
 for(const pool of fixture.calls.windowPools)assert.equal(pool.find(slot=>slot.tabId===261)?.key,migrationKey);
});

for(const [name,page] of [
 ['wrong DOM owner',{owner:'b'.repeat(64)}],['unsent editor draft',{draft:'user text'}],['missing editor',{editor:false}],['unfinished document',{readyState:'loading'}],['URL changed after ID lookup',{url:'https://chatgpt.com/c/another-page?temporary-chat=true'}]
]){
 test('explicit GPT migration refuses '+name+' without saving ownership or creating a replacement',async()=>{
  const options=migrationOptions();options.page={...options.page,...page};const fixture=harness(options);
  await assert.rejects(fixture.context.migrateChatgptPool(),/chatgpt_migration_page_unproven/);
  assert.equal(fixture.local.bridgePool,undefined);assert.equal(fixture.local.chatgptMigrationApplied,undefined);
  assert.equal(fixture.calls.poolWrites+fixture.calls.queries+fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);
 });
}

test('explicit GPT migration never searches for a matching URL when its exact recorded tab ID has disappeared',async()=>{
 const options=migrationOptions();options.tabs=[{id:99,url:migrationUrl}];const fixture=harness(options);
 await assert.rejects(fixture.context.migrateChatgptPool(),/chatgpt_migration_tab_unavailable/);
 assert.equal(fixture.calls.queries+fixture.calls.creates+fixture.calls.updates+fixture.calls.poolWrites,0);assert.equal(fixture.local.chatgptMigrationApplied,undefined);
});

test('a consumed explicit GPT migration is not replayed after extension reload or tab closure',async()=>{
 const options=migrationOptions();const fixture=harness(options);await fixture.context.migrateChatgptPool();
 const reloaded=harness({...options,local:fixture.local,session:{},tabs:[{id:99,url:migrationUrl}]});
 await reloaded.context.migrateChatgptPool();
 assert.equal(reloaded.local.bridgePool[0].tabId,261);assert.equal(reloaded.calls.gets+reloaded.calls.scripts+reloaded.calls.queries+reloaded.calls.poolWrites,0);
 await assert.rejects(reloaded.context.acquire(migrationKey),/chatgpt_owned_tab_unavailable/);assert.equal(reloaded.calls.creates,0);
});

test('explicit GPT migration cannot overwrite an occupied slot or grow beyond the fixed three-slot pool',async()=>{
 const options=migrationOptions();const pool=[0,1,2].map(slotId=>({tabId:32+slotId,slotId,url:rootUrl,key:'b'.repeat(64),turns:2}));options.local={bridgePool:pool};
 const fixture=harness(options);await assert.rejects(fixture.context.migrateChatgptPool(),/chatgpt_migration_conflict/);
 assert.deepEqual(fixture.local.bridgePool,pool);assert.equal(fixture.local.chatgptMigrationApplied,undefined);assert.equal(fixture.calls.poolWrites+fixture.calls.creates+fixture.calls.updates+fixture.calls.queries,0);
});

const privatePath=path.join(__dirname,'gpt-migration.local.json');
test('the operator-authorized private existing GPT page is accepted with its exact saved DOM owner and empty editor proof',{skip:!fs.existsSync(privatePath)},async()=>{
 const migration=JSON.parse(fs.readFileSync(privatePath,'utf8')),claim=migration.slots[0];
 const fixture=harness({migration,bootstrap:{recovery_id:'short'},tabs:[{id:claim.tabId,url:claim.url}],page:{url:claim.url,owner:claim.key,draft:''}});
 await fixture.context.migrateChatgptPool();
 assert.equal(fixture.local.chatgptMigrationApplied,migration.migration_id);assert.equal(fixture.local.bridgePool.length,migration.slots.length);
 assert.equal(fixture.local.bridgePool[0].tabId,claim.tabId);assert.equal(fixture.local.bridgePool[0].url,claim.url);assert.equal(fixture.local.bridgePool[0].key,claim.key);
 assert.equal(fixture.calls.queries+fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);assert.equal(fixture.calls.sessionPoolWrites,0);
});

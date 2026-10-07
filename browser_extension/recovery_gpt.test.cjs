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
 const calls={queries:0,scripts:0,creates:0,updates:0,poolWrites:0,network:0};
 let tabs=options.tabs||[{id:32,url:rootUrl}];
 const bootstrap={recovery_id:nonce,owned_url:rootUrl,...options.bootstrap};
 const page={readyState:'complete',editor:true,draft:'',messageSelector:'',url:rootUrl,...options.page};
 const document={
  get readyState(){return page.readyState;},
  querySelector(selector){
   if(selector==='[role="textbox"][contenteditable="true"]')return page.editor?{textContent:page.draft}:null;
   if(page.messageSelector&&selector.includes(page.messageSelector))return {};
   return null;
  }
 };
 const storage=target=>({
  async get(key){return typeof key==='string'?{[key]:structuredClone(target[key])}:structuredClone(target);},
  async set(values){if('bridgePool' in values)calls.poolWrites++;Object.assign(target,structuredClone(values));},
  async remove(key){delete target[key];}
 });
 const chrome={
  storage:{local:storage(local),session:storage(session)},
  alarms:{create(){},onAlarm:{addListener(){}}},
  runtime:{getURL:name=>'extension://'+name,onInstalled:{addListener(){}},openOptionsPage(){}},
  action:{onClicked:{addListener(){}}},
  tabs:{
   async query(){calls.queries++;if(options.queryGate)await options.queryGate;return structuredClone(tabs);},
   async get(id){const tab=tabs.find(t=>t.id===id);if(!tab)throw Error('closed');return structuredClone(tab);},
   async create(){calls.creates++;throw Error('unexpected tab creation');},
   async update(){calls.updates++;throw Error('unexpected navigation');}
  },
  scripting:{async executeScript({func,args=[]}){
   calls.scripts++;
   if(!func)throw Error('unexpected page-script injection');
   const result=vm.runInNewContext('('+func.toString()+')(...args)',{document,location:{href:page.url},args});
   return [{result}];
  }}
 };
 const context=vm.createContext({
  chrome,URL,console:{warn(){}},
  WebSocket:class{constructor(){throw Error('unexpected bridge connection');}},
  async fetch(url){
   if(url==='extension://chatgpt-bootstrap.local.json')return {async json(){return structuredClone(bootstrap);}};
   calls.network++;throw Error('unexpected network request');
  },
  importScripts(file){
   if(file==='pool_core.js')vm.runInContext(fs.readFileSync(path.join(__dirname,file),'utf8'),context,{filename:file});
   else assert.ok(['deepseek_background.js','doubao_background.js'].includes(file));
  }
 });
 vm.runInContext(fs.readFileSync(path.join(__dirname,'background.js'),'utf8'),context,{filename:'background.js'});
 return {context,local,session,calls,page,setTabs(value){tabs=value;}};
}

test('GPT adopts a single explicit empty page and consumes its idle slot without creating a page',async()=>{
 const fixture=harness();
 await fixture.context.recoverChatgptPage();
 assert.equal(fixture.local.chatgptRecoveryApplied,nonce);
 assert.equal(fixture.session.bridgePool.length,1);
 assert.equal(fixture.session.bridgePool[0].bootstrapIdle,true);
 const acquired=await fixture.context.acquire('synthetic-contact-key');
 assert.equal(acquired.slot.tabId,32);
 assert.equal(acquired.slot.key,'synthetic-contact-key');
 assert.equal(acquired.slot.bootstrapIdle,undefined);
 assert.equal(acquired.reused,false);
 assert.equal(fixture.session.bridgePool[0].key,'synthetic-contact-key');
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
 assert.equal(fixture.session.bridgePool[0].key,'concurrent-contact-key');
 assert.equal(fixture.session.bridgePool[0].bootstrapIdle,undefined);
 assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.network,0);
});

test('GPT does not replay a consumed recovery after a worker restart or closed page',async()=>{
 const fixture=harness();
 await fixture.context.recoverChatgptPage();
 const before=structuredClone(fixture.session);
 const restarted=harness({local:fixture.local,session:fixture.session,tabs:[{id:99,url:rootUrl}]});
 await restarted.context.recoverChatgptPage();
 assert.deepEqual(restarted.session,before);
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

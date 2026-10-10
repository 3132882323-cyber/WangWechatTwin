const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function readinessFixture(provider,{owner,marker=false,draft=false,attachments=false,pending=false,duplicate=false,blank=false,wrongUrl=false,foreignUser=false}={}){
 const deepseek=provider==='deepseek',key='a'.repeat(64),turn='previous-turn';
 const url=deepseek?'https://chat.deepseek.com/a/chat/s/owned':'https://www.doubao.com/chat/123';
 const otherUrl=deepseek?'https://chat.deepseek.com/a/chat/s/other':'https://www.doubao.com/chat/456';
 const tab={id:42,url},saved=deepseek?
  {deepseekOwnedTab:tab.id,deepseekOwnedUrl:url,deepseekConversations:{[key]:{url,lastTurnId:turn}}}:
  {doubaoOwnedTab:tab.id,doubaoOwnedUrl:url,doubaoConversations:{[key]:{url,lastTurnId:turn,pending}}};
 const conversations=saved[deepseek?'deepseekConversations':'doubaoConversations'];
 if(duplicate)conversations['another-contact']={url,lastTurnId:'another-turn'};
 let now=0,replyCalls=0;const results=[];
 class TestDate extends Date{static now(){return now;}}
 const user={textContent:foreignUser?'manual prompt':marker?'[wechat-turn:'+turn+']':'[wechat-turn:older-turn]',querySelector(){return null;}};
 const answer={textContent:'synthetic prior answer'};
 const editor=deepseek?{value:draft?'manual draft':''}:{textContent:draft?'manual draft':''};
 const document={
  readyState:'complete',
  documentElement:{dataset:{[deepseek?'wechatDeepseekOwner':'wechatDoubaoOwner']:owner||''}},
  querySelector(selector){
   if(deepseek){
    if(selector.startsWith('textarea'))return editor;
    if(selector==='.ds-virtual-list-visible-items')return {children:blank?[]:[user]};
   }else{
    if(selector.includes('contenteditable'))return editor;
    if(selector==='[data-testid="chat_input"] [data-testid="attachment-image-card"]')return attachments?{}:null;
   }
   return null;
  },
  querySelectorAll(selector){
   if(deepseek)return selector==='.ds-assistant-message-main-content'&&!blank?[answer]:[];
   if(selector==='[data-testid="receive_message"]')return blank?[]:[answer];
   if(selector==='[data-testid="send_message"]')return blank?[]:[user];
   return [];
  }
 };
 const chrome={
  storage:{local:{
   async get(item){return item==='token'?{token:'synthetic-token'}:saved;},
   async set(value){Object.assign(saved,value);}
  },session:{async set(){}}},
  runtime:{getURL:()=> 'missing-local-bootstrap'},
  tabs:{async get(){return tab;},async update(){throw Error('must keep the owned tab');}},
  scripting:{async executeScript(options){
   if(options.files)return [{result:null}];
   if(deepseek&&options.args?.[0]==='Synthetic')return [{result:null}];
   if(options.args?.length===1)return [{result:options.func(...options.args)}];
   if(options.args?.length===(deepseek?6:7)){
    replyCalls++;
    return [{result:deepseek?{reply:'{"reply":"synthetic"}',url,reused:true,reset:false}:{reply:'{"reply":"synthetic"}',url}}];
   }
   throw Error('unexpected script injection');
  }},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(endpoint,options)=>{
  if(endpoint==='missing-local-bootstrap')throw Error('missing bootstrap');
  if(endpoint.endsWith('/next'))return {ok:true,json:async()=>({job:{id:'new-turn',conversation_key:key,prompt:'synthetic prompt',images:[],contact_name:'Synthetic',created:0}})};
  if(endpoint.endsWith('/result')){results.push(JSON.parse(options.body));return {ok:true};}
  throw Error('unexpected fetch');
 };
 const context={chrome,fetch,document,location:{href:wrongUrl?otherUrl:url},AbortController,URL,WebSocket:class{},Date:TestDate,
  setTimeout(done,ms){if(ms<=500){now+=ms;queueMicrotask(done);return 0;}return setTimeout(done,ms);},clearTimeout,
  console:{warn(){}},WechatBackgroundWindow:{async ensure(){},async ensureOwnedTab(){return tab;},async isRenderingIdle(){return true;},async acquireRendering(){return 'lease';},async releaseRendering(){return true;}}
 };
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,provider+'_background.js'),'utf8'),context,{filename:provider+'_background.js'});
 return {run:()=>context[provider+'Pump'](),results,replyCalls:()=>replyCalls,now:()=>now,saved};
}

for(const provider of ['deepseek','doubao']){
 test(`${provider} reuses a virtualized conversation with live matching ownership before the 8s deadline`,async()=>{
  const fixture=readinessFixture(provider,{owner:'a'.repeat(64)});
  await fixture.run();
  assert.equal(fixture.replyCalls(),1);
  assert.equal(fixture.results[0].error,undefined);
  assert.ok(fixture.now()>=900&&fixture.now()<8000);
 });

 test(`${provider} does not infer ownership from URL and editor alone`,async()=>{
  const fixture=readinessFixture(provider);
  await fixture.run();
  assert.equal(fixture.replyCalls(),0);
  assert.equal(fixture.results[0].error,provider==='deepseek'?'deepseek_known_turn_not_visible':'doubao_login_or_load');
  assert.ok(fixture.now()>=8000);
 });

 test(`${provider} rejects a foreign page owner and duplicate contact URL`,async()=>{
  for(const options of [{owner:'another-contact'},{owner:'a'.repeat(64),duplicate:true}]){
   const fixture=readinessFixture(provider,options);
   await fixture.run();
   assert.equal(fixture.replyCalls(),0);
   assert.equal(fixture.results[0].error,provider+'_wrong_contact');
   assert.ok(fixture.now()<8000);
  }
 });

 test(`${provider} retains the visible known-turn path when page ownership was reset`,async()=>{
  const fixture=readinessFixture(provider,{marker:true});
  await fixture.run();
  assert.equal(fixture.replyCalls(),1);
  assert.equal(fixture.results[0].error,undefined);
  assert.ok(fixture.now()<8000);
 });

 test(`${provider} refuses a virtual-turn fallback with a manual draft, unowned user, or blank transcript`,async()=>{
  for(const options of [{owner:'a'.repeat(64),draft:true},{owner:'a'.repeat(64),blank:true},{owner:'a'.repeat(64),foreignUser:true}]){
   const fixture=readinessFixture(provider,options);
   await fixture.run();
   assert.equal(fixture.replyCalls(),0);
   assert.ok(fixture.now()>=8000);
  }
 });

 test(`${provider} refuses a different rendered URL`,async()=>{
  const fixture=readinessFixture(provider,{owner:'a'.repeat(64),wrongUrl:true});
  await fixture.run();
  assert.equal(fixture.replyCalls(),0);
  assert.ok(fixture.now()>=8000);
 });
}

test('doubao refuses a pending unverified conversation before invoking the page',async()=>{
 const fixture=readinessFixture('doubao',{owner:'a'.repeat(64),pending:true});
 await fixture.run();
 assert.equal(fixture.replyCalls(),0);
 assert.equal(fixture.results[0].error,'doubao_pending_unverified');
 assert.ok(fixture.now()<8000);
});

test('doubao refuses attached manual media when its old marker is virtualized',async()=>{
 const fixture=readinessFixture('doubao',{owner:'a'.repeat(64),attachments:true});
 await fixture.run();
 assert.equal(fixture.replyCalls(),0);
 assert.equal(fixture.results[0].error,'doubao_login_or_load');
 assert.ok(fixture.now()>=8000);
});

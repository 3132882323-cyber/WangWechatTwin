const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function fakeTime(){
 let now=0;
 class TestDate extends Date{static now(){return now;}}
 return {Date:TestDate,setTimeout(done,ms){now+=ms;done();return 1;},now:()=>now};
}
function load(name,context){vm.runInNewContext(fs.readFileSync(path.join(__dirname,name),'utf8'),context,{filename:name});}

function webFixture({wrongUrl=false,modeMissing=false,showTurn=false,reply='',modeError=''}={}){
 const time=fakeTime(),url='https://chat.deepseek.com/a/chat/s/kept',dataset={wechatDeepseekOwner:'known-contact'};
 let sent=false;
 class Textarea{
  get value(){return this._value||'';}
  set value(value){this._value=value;}
  focus(){}
  dispatchEvent(event){if(event.key==='Enter')sent=true;}
 }
 const editor=new Textarea(),answer={textContent:reply,innerText:reply};
 const user={textContent:'[wechat-turn:test-turn]',order:1,querySelector(){return null;},compareDocumentPosition(other){return other.order>1?4:2;}};
 const response={textContent:reply,order:2,querySelector(){return answer;}};
 const controls=['深度思考','智能搜索'].map(textContent=>({textContent,getAttribute(){if(modeError)throw Error(modeError);return 'false';}}));
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector.startsWith('textarea'))return editor;
   if(selector==='.ds-virtual-list-visible-items')return {children:sent&&showTurn?[user,...(reply?[response]:[])]:[]};
   return null;
  },
  querySelectorAll(selector){
   if(selector==='.ds-toggle-button')return modeMissing?[]:controls;
   if(selector==='.ds-assistant-message-main-content')return sent&&showTurn&&reply?[answer]:[];
   return [];
  }
 };
 const saved={deepseekConversations:{'known-contact':{url,name:'kept',lastTurnId:'old-turn'}}};
 const context={window:{},document,location:{href:wrongUrl?'https://chat.deepseek.com/a/chat/s/other':url},
  chrome:{storage:{local:{async get(){return structuredClone(saved);},async set(values){Object.assign(saved,structuredClone(values));}}}},
  HTMLTextAreaElement:Textarea,Event:class{},KeyboardEvent:class{constructor(_,options){this.key=options.key;}},Node:{DOCUMENT_POSITION_FOLLOWING:4},Date:time.Date,setTimeout:time.setTimeout};
 load('deepseek_web.js',context);
 return {context,url,dataset,saved,time,sent:()=>sent,run:()=>context.window.wechatDeepseekDraft('synthetic prompt','known-contact',true,url,'test-turn')};
}

for(const [options,code,stage,sent] of [
 [{wrongUrl:true},'deepseek_wrong_conversation','preflight',false],
 [{modeMissing:true},'deepseek_mode_control_missing','mode',false],
 [{},'deepseek_turn_not_visible','reply_marker',true],
 [{showTurn:true,reply:'unstructured response'},'deepseek_reply_timeout','reply',true]
]){
 test(`DeepSeek preserves ${code} and ${stage} inside its web failure`,async()=>{
  const fixture=webFixture(options),before=structuredClone(fixture.saved.deepseekConversations);
  await assert.rejects(fixture.run,error=>{
   assert.equal(error.message,code);assert.equal(error.deepseekCode,code);assert.equal(error.deepseekStage,stage);return true;
  });
  assert.equal(fixture.dataset.wechatDeepseekFailure,code);assert.equal(fixture.dataset.wechatDeepseekFailureStage,stage);
  assert.equal(fixture.sent(),sent);
  if(!options.showTurn)assert.deepEqual(fixture.saved.deepseekConversations,before);
 });
}

test('DeepSeek web diagnostics do not expose an unknown exception message',async()=>{
 const privateDetail='private prompt contact token detail',fixture=webFixture({modeError:privateDetail});
 await assert.rejects(fixture.run,error=>{
  assert.equal(error.message,'deepseek_unexpected_error');assert.equal(error.deepseekCode,'deepseek_unexpected_error');return true;
 });
 assert.equal(JSON.stringify(fixture.dataset).includes(privateDetail),false);
 assert.equal(fixture.dataset.wechatDeepseekFailureStage,'mode');assert.equal(fixture.sent(),false);
});

function backgroundFixture({ready,outcome,executeWeb,throwAt,rawError}={}){
 const time=fakeTime(),url='https://chat.deepseek.com/a/chat/s/kept',key='a'.repeat(64),calls={results:[],creates:0,updates:0,reply:0,setup:0};
 const saved={deepseekOwnedTab:208,deepseekOwnedUrl:url,deepseekConversations:{[key]:{url,name:'kept',lastTurnId:'old-turn'},other:{url:'https://chat.deepseek.com/a/chat/s/other',lastTurnId:'other-turn'}}};
 const before=structuredClone(saved.deepseekConversations);
 const state=ready||{url,ready:'complete',editor:true,answers:1,users:1,lastAnswerLength:5,hasKnownTurn:true};
 const chrome={
  storage:{local:{async get(item){return item==='token'?{token:'synthetic-token'}:structuredClone(saved);},async set(values){Object.assign(saved,structuredClone(values));}},session:{async set(){}}},
  tabs:{async get(id){assert.equal(id,208);return {id,url};},async create(){calls.creates++;throw Error('must not create');},async update(){calls.updates++;throw Error('must not navigate');}},
  runtime:{getURL:()=> 'chrome-extension://test/missing.json'},
  scripting:{async executeScript(options){
   if(options.files){calls.setup++;if(throwAt==='setup')throw Error(rawError||'script failure');return [{result:null}];}
   if(options.args?.length===6){
    calls.reply++;
    if(executeWeb){
     // Emulate Chromium losing unhandled rejected injections: only the wrapper's resolved result survives.
     try{return [{result:await vm.runInNewContext('('+options.func.toString()+')(...args)',{window:executeWeb.window,args:options.args})}];}
     catch{return [{result:undefined}];}
    }
    return [{result:outcome}];
   }
   if(throwAt==='ready')throw Error(rawError||'injection failure');
   return [{result:state}];
  }},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(endpoint,options)=>{
  if(endpoint.endsWith('/next')){if(throwAt==='queue')throw Error(rawError||'queue failure');return {ok:true,json:async()=>({job:{id:'test-turn',conversation_key:key,prompt:'synthetic prompt',images:[],created:0}})};}
  if(endpoint.endsWith('/result')){calls.results.push(JSON.parse(options.body));return {ok:true};}
  throw Error('missing local bootstrap');
 };
 const context={chrome,fetch,WebSocket:class{},URL,Date:time.Date,setTimeout:time.setTimeout,console:{warn(){}},WechatBackgroundWindow:{async ensure(){},async ensureOwnedTab(id){return chrome.tabs.get(id);},async createModelTab(){calls.creates++;throw Error('must not create');}}};
 load('deepseek_background.js',context);
 return {context,calls,saved,before,url,key,time,run:()=>context.deepseekPump()};
}

test('DeepSeek carries an injected web rejection across Chromium and records its exact fixed stage',async()=>{
 const web=webFixture({modeMissing:true}),fixture=backgroundFixture({executeWeb:web.context});
 web.dataset.wechatDeepseekOwner=fixture.key;
 await fixture.run();
 assert.equal(fixture.calls.results.length,1);
 assert.deepEqual(fixture.calls.results[0],{id:'test-turn',error:'deepseek_mode_control_missing',stage:'mode'});
 assert.equal(fixture.saved.deepseekLastFailure.code,'deepseek_mode_control_missing');assert.equal(fixture.saved.deepseekLastFailure.stage,'mode');
 assert.equal(fixture.saved.deepseekLastFailure.tab_id,208);assert.equal(fixture.saved.deepseekLastFailure.job_id,'test-turn');
 assert.deepEqual(fixture.saved.deepseekConversations,fixture.before);
 assert.equal(fixture.calls.creates+fixture.calls.updates,0);assert.equal(web.sent(),false);
 assert.equal(vm.runInNewContext('deepseekBusy',fixture.context),false);
});

for(const [change,code] of [
 [{hasKnownTurn:false},'deepseek_known_turn_not_visible'],
 [{url:'https://chat.deepseek.com/a/chat/s/other'},'deepseek_conversation_not_loaded'],
 [{ready:'loading'},'deepseek_page_loading'],
 [{editor:false},'deepseek_login_or_load_required']
]){
 test(`DeepSeek reports ${code} without changing the saved conversation or sending`,async()=>{
  const url='https://chat.deepseek.com/a/chat/s/kept',state={url,ready:'complete',editor:true,answers:1,users:1,lastAnswerLength:5,hasKnownTurn:true,...change};
  const fixture=backgroundFixture({ready:state});await fixture.run();
  assert.equal(fixture.calls.results[0].error,code);assert.equal(fixture.calls.results[0].stage,'ready');
  assert.equal(fixture.saved.deepseekLastFailure.code,code);assert.equal(fixture.saved.deepseekLastFailure.stage,'ready');
  assert.deepEqual(fixture.saved.deepseekLastFailure.ready_state,{url_matches_expected:state.url===url,document_ready:state.ready,editor:state.editor,has_known_turn:state.hasKnownTurn,visible_users:1,visible_answers:1});
  assert.deepEqual(fixture.saved.deepseekConversations,fixture.before);
  assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.reply+fixture.calls.setup,0);
  assert.ok(fixture.time.now()>=30000);
 });
}

test('DeepSeek distinguishes a script that never supplied any readiness state',async()=>{
 const fixture=backgroundFixture({throwAt:'ready',rawError:'private URL and contact'});await fixture.run();
 assert.equal(fixture.calls.results[0].error,'deepseek_ready_script_failed');assert.equal(fixture.calls.results[0].stage,'ready');
 assert.equal(JSON.stringify(fixture.saved.deepseekLastFailure).includes('private URL'),false);
 assert.equal(fixture.calls.reply,0);assert.deepEqual(fixture.saved.deepseekConversations,fixture.before);
});

test('DeepSeek distinguishes a missing injection result from an empty or wrong-conversation reply',async()=>{
 const fixture=backgroundFixture();await fixture.run();
 assert.equal(fixture.calls.results[0].error,'deepseek_reply_missing_result');assert.equal(fixture.calls.results[0].stage,'reply');
 assert.deepEqual(fixture.saved.deepseekConversations,fixture.before);
});

test('DeepSeek background diagnostics allow only fixed codes and web stages',async()=>{
 const privateDetail='private_message_contact_token',fixture=backgroundFixture({outcome:{error:'deepseek_unknown_'+privateDetail,stage:privateDetail}});
 assert.equal(fixture.context.deepseekErrorCode(Error('Error: deepseek_user_editing '+privateDetail)),'deepseek_user_editing');
 assert.equal(fixture.context.deepseekErrorCode(Error('deepseek_unknown_'+privateDetail)),'deepseek_unexpected_error');
 assert.equal(fixture.context.deepseekErrorCode(Error('background_window_manual_reveal')),'deepseek_background_window_unavailable');
 await fixture.run();
 assert.deepEqual(fixture.calls.results[0],{id:'test-turn',error:'deepseek_unexpected_error',stage:'reply'});
 assert.equal(JSON.stringify(fixture.saved).includes(privateDetail),false);assert.equal(JSON.stringify(fixture.calls.results).includes(privateDetail),false);
});

test('DeepSeek queue exceptions retain their stage without disclosing details or claiming another job',async()=>{
 const fixture=backgroundFixture({throwAt:'queue',rawError:'private token details'});await fixture.run();
 assert.equal(fixture.saved.deepseekLastFailure.code,'deepseek_unexpected_error');assert.equal(fixture.saved.deepseekLastFailure.stage,'queue');
 assert.equal(fixture.saved.deepseekLastFailure.job_id,'');assert.equal(fixture.calls.results.length,0);
 assert.equal(JSON.stringify(fixture.saved.deepseekLastFailure).includes('private token'),false);
 assert.equal(vm.runInNewContext('deepseekBusy',fixture.context),false);
});

test('DeepSeek successful replies keep their existing conversation and last-turn proof',async()=>{
 const url='https://chat.deepseek.com/a/chat/s/kept',fixture=backgroundFixture({outcome:{reply:'{"reply":"synthetic answer"}',url,reused:true,reset:false}});
 await fixture.run();
 assert.equal(fixture.calls.results[0].error,undefined);assert.equal(fixture.calls.results[0].result,'{"reply":"synthetic answer"}');
 assert.equal(fixture.saved.deepseekConversations[fixture.key].url,url);assert.equal(fixture.saved.deepseekConversations[fixture.key].lastTurnId,'test-turn');
 assert.deepEqual(fixture.saved.deepseekConversations.other,fixture.before.other);
 assert.equal(fixture.calls.creates+fixture.calls.updates,0);assert.equal(fixture.saved.deepseekLastFailure,undefined);
});

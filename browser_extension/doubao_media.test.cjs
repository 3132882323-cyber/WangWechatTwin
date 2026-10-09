const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function backgroundHarness({expectedUrl='https://www.doubao.com/chat/666',reply,missing=false}={}){
 const key='a'.repeat(64),turn='media-turn',tab={id:42,url:expectedUrl};
 const saved={doubaoOwnedTab:tab.id,doubaoOwnedUrl:tab.url,doubaoConversations:{[key]:{url:expectedUrl,lastTurnId:'previous-turn',name:'虚构备注'}}};
 const calls={results:[],acquires:[],releases:[],next:0};let now=0;
 class TestDate extends Date{static now(){return now;}}
 const chrome={
  storage:{local:{async get(item){return item==='token'?{token:'test-token'}:saved;},async set(value){Object.assign(saved,value);}}},
  runtime:{getURL:()=> 'local-bootstrap'},
  tabs:{async get(){return tab;},async update(){throw Error('must keep the owned page');}},
  scripting:{async executeScript(options){
   if(options.files)return [{result:null}];
   if(options.args?.length===7){
    assert.equal(options.args[3].length,3);
    return missing?[{}]:[{result:await options.func(...options.args)}];
   }
   return [{result:{url:tab.url,editor:true,draft:false,attachments:false,users:1,answers:1,lastAnswerLength:6,hasKnownTurn:true}}];
  }},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(url,options)=>{
  if(url==='local-bootstrap')throw Error('missing local bootstrap');
  if(url.endsWith('/next')){calls.next++;return {ok:true,json:async()=>({job:calls.next===1?{id:turn,conversation_key:key,prompt:'虚构图片提示',contact_name:'虚构备注',images:[1,2,3].map(i=>({name:`frame-${i}.png`,mime:'image/png',data:'AA=='}))}:null})};}
  if(url.endsWith('/result')){calls.results.push(JSON.parse(options.body));return {ok:true};}
  throw Error('unexpected fetch');
 };
 const context={chrome,fetch,AbortController,URL,WebSocket:class{},Date:TestDate,
  setTimeout(done,ms){if(ms<=500){now+=ms;queueMicrotask(done);return undefined;}return setTimeout(done,ms);},clearTimeout,
  console:{warn(){}},window:{async wechatDoubaoReply(...args){return reply({saved,key,turn,tab,args});}},
  WechatBackgroundWindow:{async ensure(){},async isRenderingIdle(){return true;},async ensureOwnedTab(){return tab;},
   async acquireRendering(...args){calls.acquires.push(args);return 'owned-media-lease';},async releaseRendering(lease){calls.releases.push(lease);return true;}}
 };
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'doubao_background.js'),'utf8'),context,{filename:'doubao_background.js'});
 return {context,saved,calls,key,turn,tab};
}

for(const [thrown,expected] of [
 ['doubao_image_upload_not_verified private-message-and-token','doubao_image_upload_not_verified'],
 ['doubao_prompt_or_send_not_ready private-message-and-token','doubao_prompt_or_send_not_ready'],
 ['private-message-and-token','doubao_unexpected_error'],
 ['doubao_unknown_private-message-and-token','doubao_unexpected_error']
]){
 test(`Doubao carries ${expected} through Chromium's injected reply boundary`,async()=>{
  const harness=backgroundHarness({reply:async()=>{throw Error(thrown);}});
  await harness.context.doubaoPump();
  assert.equal(harness.calls.results[0].error,expected);
  assert.equal(harness.calls.results[0].stage,'reply');
  assert.equal(harness.saved.doubaoLastFailure.code,expected);
  assert.equal(JSON.stringify(harness.saved).includes('private-message-and-token'),false);
  assert.equal(JSON.stringify(harness.calls.results).includes('private-message-and-token'),false);
  assert.deepEqual(harness.calls.releases,['owned-media-lease']);
  assert.equal(vm.runInNewContext('doubaoBusy',harness.context),false);
  await harness.context.doubaoPump();
  assert.equal(harness.calls.next,2);
 });
}

test('Doubao distinguishes a missing injection result from a conversation mismatch',async()=>{
 const harness=backgroundHarness({missing:true});
 await harness.context.doubaoPump();
 assert.equal(harness.saved.doubaoLastFailure.code,'doubao_reply_missing_result');
 assert.equal(harness.calls.results[0].error,'doubao_reply_missing_result');
 assert.deepEqual(harness.calls.releases,['owned-media-lease']);
});

for(const proof of ['current-turn','older-turn','another-url','missing']){
 test(`Doubao checks ${proof} proof for a local media conversation migration`,async()=>{
  const final='https://www.doubao.com/chat/777';
  const harness=backgroundHarness({expectedUrl:'https://www.doubao.com/chat/local_123',reply:async({saved,key,turn,tab})=>{
   tab.url=final;
   if(proof!=='missing')saved.doubaoConversations[key]={url:proof==='another-url'?'https://www.doubao.com/chat/888':final,lastTurnId:proof==='older-turn'?'previous-turn':turn,name:'虚构备注'};
   return {reply:'{"reply":"虚构图片答复"}',url:final};
  }});
  await harness.context.doubaoPump();
  if(proof==='current-turn'){
   assert.equal(harness.calls.results[0].error,undefined);
   assert.equal(harness.calls.results[0].result,'{"reply":"虚构图片答复"}');
   assert.equal(harness.saved.doubaoConversations[harness.key].url,final);
   assert.equal(harness.saved.doubaoConversations[harness.key].lastTurnId,harness.turn);
  }else assert.equal(harness.calls.results[0].error,'doubao_empty_or_wrong_conversation');
  assert.deepEqual(harness.calls.releases,['owned-media-lease']);
 });
}

test('Doubao rejects a different final conversation even with a matching saved turn',async()=>{
 const harness=backgroundHarness({reply:async({saved,key,turn,tab})=>{
  tab.url='https://www.doubao.com/chat/777';
  saved.doubaoConversations[key]={url:tab.url,lastTurnId:turn};
  return {reply:'{"reply":"不可接受的其他会话"}',url:tab.url};
 }});
 await harness.context.doubaoPump();
 assert.equal(harness.calls.results[0].error,'doubao_empty_or_wrong_conversation');
});

function mediaDomHarness({frames=3,beforeKind='image',groups=1,hydrate=true,preExisting=false}={}){
 let now=0,nextTimer=0,uploaded=false,sent=false;
 const timers=new Map(),dataset={},saved={},home='https://www.doubao.com/chat/',final=home+'777';
 class TestDate extends Date{static now(){return now;}}
 const advance=ms=>{now+=ms;for(const [id,timer] of timers){if(timer.due<=now){timers.delete(id);timer.done();}}};
 const context={window:{},Date:TestDate,
  setTimeout(done,ms){const id=++nextTimer;timers.set(id,{due:now+ms,done});return id;},clearTimeout(id){timers.delete(id);},
  MutationObserver:class{
   constructor(callback){this.callback=callback;this.active=false;}
   observe(){this.active=true;const pulse=()=>{if(!this.active)return;advance(300);this.callback();if(this.active)queueMicrotask(pulse);};queueMicrotask(pulse);}
   disconnect(){this.active=false;}
  },
  InputEvent:class{},Event:class{},Node:{DOCUMENT_POSITION_FOLLOWING:4},atob:()=> '\x00',
  DataTransfer:class{constructor(){this.files=[];this.items={add:file=>this.files.push(file)};}},File:class{},
  chrome:{storage:{local:{async get(){return saved;},async set(value){Object.assign(saved,value);}}}}
 };
 const editor={textContent:'',focus(){},dispatchEvent(){}};
 const images=Array.from({length:frames},(_,i)=>({data:'AA==',name:`frame-${i}.png`,mime:'image/png'}));
 const hydrated=()=>hydrate&&now>=1800;
 const makeNode=(order,text,role,id,image=false)=>({
  order,textContent:text,querySelector(selector){
   if(selector==='[data-message-id]')return hydrated()?{getAttribute:name=>name==='data-message-id'?String(id):null}:null;
   if(selector==='img')return image?{naturalWidth:20}:null;
   return null;
  },
  querySelectorAll(){return [];},getAttribute:name=>name==='data-message-role'?role:null,
  compareDocumentPosition(other){return other.order>order?4:2;}
 });
 const earlier=Array.from({length:groups},(_,i)=>makeNode(i+1,beforeKind==='text'?'外来文字':'','user',100+i,beforeKind==='image'));
 const user=makeNode(10,'','user',200);
 const body={innerText:'{"reply":"虚构表情答复"}',getAttribute:name=>name==='data-streaming'?'false':null};
 const answer=makeNode(11,'','assistant',300),queryAnswer=answer.querySelector;
 answer.querySelector=selector=>selector==='[data-testid="message_text_content"]'?(now>=2400?body:null):(now>=2400?queryAnswer(selector):null);
 const upload={files:null,dispatchEvent(){uploaded=true;}};
 const area={querySelectorAll:()=>uploaded&&!sent?images.map(image=>({getAttribute:()=>image.name,querySelector:()=>({naturalWidth:20})})):[],querySelector:()=>null};
 const send={disabled:false,getAttribute:()=>null,click(){sent=true;user.textContent=editor.textContent;editor.textContent='';}};
 const document={documentElement:{dataset},
  querySelector(selector){
   if(selector.includes('contenteditable'))return editor;
   if(selector==='input[data-testid="upload-file-input"]')return upload;
   if(selector==='[data-testid="chat_input"]')return area;
   if(selector==='[data-testid="chat_input_send_button"]')return send;
   if(selector==='[data-testid="send_message"]'&&preExisting)return earlier[0];
   return null;
  },
  querySelectorAll(selector){
   if(selector==='[data-testid="send_message"]')return sent?[...earlier,user]:preExisting?earlier:[];
   if(selector==='[data-testid="receive_message"]')return sent?[answer]:[];
   return [];
  },execCommand(_,__,text){editor.textContent=text;}
 };
 Object.assign(context,{document,location:{origin:'https://www.doubao.com',get href(){return sent?(hydrated()?final:home+'local_123'):home;}}});
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'doubao_web.js'),'utf8'),context,{filename:'doubao_web.js'});
 return {context,saved,dataset,images,home,final,now:()=>now,sent:()=>sent,
  run:()=>context.window.wechatDoubaoReply('虚构图片提示','synthetic-media-contact',false,images,home,'synthetic-media-turn')};
}

for(const frames of [2,3])for(const groups of [1,frames]){
 test(`Doubao waits for IDs on ${groups} owned image bubbles from ${frames} verified frames and skips the empty answer placeholder`,async()=>{
  const harness=mediaDomHarness({frames,groups});
  const outcome=await harness.run();
  assert.equal(JSON.parse(outcome.reply).reply,'虚构表情答复');
  assert.equal(outcome.url,harness.final);
  assert.ok(harness.now()>=2400);
  assert.equal(harness.saved.doubaoConversations['synthetic-media-contact'].lastTurnId,'synthetic-media-turn');
  assert.equal(harness.saved.doubaoConversations['synthetic-media-contact'].url,harness.final);
  assert.equal(JSON.parse(harness.dataset.wechatDoubaoDiagnostic).complete,true);
 });
}

for(const options of [{beforeKind:'text'},{groups:4},{preExisting:true}]){
 test(`Doubao refuses an unowned image-turn prefix ${JSON.stringify(options)}`,async()=>{
  const harness=mediaDomHarness(options);
  await assert.rejects(harness.run,/doubao_unowned_conversation/);
  assert.equal(harness.saved.doubaoConversations,undefined);
  assert.equal(harness.saved.doubaoLastDomFailure.failure,'doubao_unowned_conversation');
  if(options.preExisting)assert.equal(harness.sent(),false);
 });
}

test('Doubao times out instead of accepting an owned image bubble whose ID never becomes verifiable',async()=>{
 const harness=mediaDomHarness({hydrate:false});
 await assert.rejects(harness.run,/doubao_reply_timeout/);
 assert.equal(harness.saved.doubaoConversations,undefined);
 assert.equal(harness.saved.doubaoLastDomFailure.phase,'wait_user');
 assert.equal(harness.saved.doubaoLastDomFailure.failure,'doubao_reply_timeout');
});

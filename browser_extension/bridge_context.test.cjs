const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

function clock(){
 let now=0,nextId=0,longFired=0;
 const timers=new Map();
 class TestDate extends Date{static now(){return now;}}
 const advance=ms=>{
  now+=ms;
  for(const [id,timer] of timers){if(timer.due<=now){timers.delete(id);longFired++;timer.done();}}
 };
 return {
  Date:TestDate,
  setTimeout:(done,ms)=>{const id=++nextId;if(ms<=500){advance(ms);done();}else timers.set(id,{due:now+ms,done});return id;},
  clearTimeout:id=>timers.delete(id),
  advance,now:()=>now,longFired:()=>longFired
 };
}

function mutationObserver(time){
 return class{
  constructor(callback){this.callback=callback;this.active=false;}
  observe(){
   this.active=true;
   const pulse=()=>{if(!this.active)return;time.advance(300);this.callback();if(this.active)queueMicrotask(pulse);};
   queueMicrotask(pulse);
  }
  disconnect(){this.active=false;}
 };
}

function extensionStorage(state={}){
 return {storage:{local:{async get(){return state;},async set(values){Object.assign(state,values);}}}};
}

function turnNode(order,text,body){
 return {
  order,textContent:text,innerText:text,
  compareDocumentPosition(other){return other.order>order?4:2;},
  querySelector(){return body||null;}
 };
}

function load(name,context){
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,name),'utf8'),context,{filename:name});
}

test('Doubao observer resolves on a DOM mutation when the deadline timer never fires',async()=>{
 let ready=false,notify,disconnected=false;
 const context={
  window:{},document:{documentElement:{}},
  MutationObserver:class{constructor(callback){notify=callback;}observe(){}disconnect(){disconnected=true;}},
  setTimeout:()=>1,clearTimeout:()=>{}
 };
 load('doubao_web.js',context);
 const pending=context.window.wechatDoubaoWait(()=>ready?'ready':null,30000,'timeout');
 ready=true;notify();
 assert.equal(await pending,'ready');
 assert.equal(disconnected,true);
});

test('Doubao diagnostic attributes cannot wake their own observer',async()=>{
 let notify,options,checks=0,ready=false,guard='';
 const dataset={};
 Object.defineProperty(dataset,'wechatDoubaoGuard',{
  get(){return guard;},
  set(value){guard=value;if(options?.attributeFilter?.includes('data-wechat-doubao-guard'))queueMicrotask(()=>notify());}
 });
 const context={
  window:{},document:{documentElement:{dataset}},
  MutationObserver:class{constructor(callback){notify=callback;}observe(_,value){options=value;}disconnect(){}},
  setTimeout:()=>1,clearTimeout:()=>{}
 };
 load('doubao_web.js',context);
 const pending=context.window.wechatDoubaoWait(()=>{
  checks++;
  if(checks===1)dataset.wechatDoubaoGuard='diagnostic';
  return ready?'ready':null;
 },30000,'timeout');
 await Promise.resolve();
 assert.equal(options.attributeFilter.includes('data-wechat-doubao-guard'),false);
 assert.equal(checks,1);
 ready=true;notify();
 assert.equal(await pending,'ready');
 assert.equal(checks,2);
});

test('Doubao image load wakes upload readiness even without a DOM mutation',async()=>{
 let width=0,notify;
 const listeners=new Map();
 const document={
  documentElement:{},
  addEventListener(name,callback,capture){assert.equal(capture,true);listeners.set(name,callback);},
  removeEventListener(name){listeners.delete(name);}
 };
 const context={
  window:{},document,
  MutationObserver:class{constructor(callback){notify=callback;}observe(){}disconnect(){}},
  setTimeout:()=>1,clearTimeout:()=>{}
 };
 load('doubao_web.js',context);
 const pending=context.window.wechatDoubaoWait(()=>width>0?'image ready':null,30000,'timeout');
 width=25;listeners.get('load')({target:{tagName:'IMG'}});
 assert.equal(await pending,'image ready');
 assert.equal(listeners.size,0);
 assert.equal(typeof notify,'function');
});

test('DeepSeek retries its known chat without New Chat and ignores a late historical answer',async()=>{
 const time=clock(),dataset={wechatDeepseekFailed:'true',wechatDeepseekFailedOwner:'contact-a'},marker='[wechat-turn:turn-new]',url='https://chat.deepseek.com/a/chat/s/123';
 let sent=false,newChatClicks=0;
 class Textarea{
  get value(){return this._value||'';}
  set value(value){this._value=value;}
  focus(){}
  dispatchEvent(event){if(event.key==='Enter')sent=true;}
 }
 const editor=new Textarea();
 const user=turnNode(2,marker+' 本轮提示');
 const oldAnswer=turnNode(1,'{"reply":"旧答案"}');
 const newAnswer=turnNode(3,'{"reply":"新答案"}');
 const oldBlock=turnNode(1,'旧回复',oldAnswer),newBlock=turnNode(3,'新回复',newAnswer);
 const list={get children(){return sent&&time.now()>=1600?[oldBlock,user,newBlock]:sent&&time.now()>=800?[oldBlock,user]:sent?[user]:[];}};
 const fresh={textContent:'开启新对话',children:[],click(){newChatClicks++;}};
 const controls=['深度思考','智能搜索'].map(label=>({textContent:label,getAttribute:()=> 'false'}));
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector.startsWith('textarea'))return editor;
   if(selector==='.ds-virtual-list-visible-items')return list;
   return null;
  },
  querySelectorAll(selector){
   if(selector==='.ds-toggle-button')return controls;
   if(selector==='body *')return [fresh];
   if(selector==='.ds-assistant-message-main-content')return sent&&time.now()>=1600?[oldAnswer,newAnswer]:sent&&time.now()>=800?[oldAnswer]:[];
   return [];
  }
 };
 const context={window:{},document,location:{href:url},chrome:extensionStorage(),HTMLTextAreaElement:Textarea,Event:class{},KeyboardEvent:class{constructor(_,options){this.key=options.key;}},Node:{DOCUMENT_POSITION_FOLLOWING:4},Date:time.Date,setTimeout:time.setTimeout};
 load('deepseek_web.js',context);
 const result=await context.window.wechatDeepseekDraft('提示','contact-a',true,url,'turn-new');
 assert.equal(JSON.parse(result.reply).reply,'新答案');
 assert.equal(result.url,url);
 assert.equal(newChatClicks,0);
 assert.ok(time.now()>=2200);
});

test('Doubao rejects a different saved conversation before sending',async()=>{
 const time=clock(),actual='https://www.doubao.com/chat/111',expected='https://www.doubao.com/chat/222';
 let sent=false;
 const editor={textContent:'',focus(){},dispatchEvent(){}};
 const document={
  documentElement:{dataset:{}},
  querySelector(selector){if(selector.includes('contenteditable'))return editor;return null;},
  querySelectorAll(){return [];},
  execCommand(){throw Error('should not type');}
 };
 const context={window:{},document,location:{href:actual},Date:time.Date,setTimeout:time.setTimeout};
 load('doubao_web.js',context);
 await assert.rejects(()=>context.window.wechatDoubaoReply('提示','contact-b',true,[],expected,'turn-b'),/doubao_wrong_conversation/);
 assert.equal(sent,false);
});

test('Doubao fresh home refuses pre-existing conversation messages',async()=>{
 const home='https://www.doubao.com/chat/',dataset={};
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector.includes('contenteditable'))return {textContent:''};
   if(selector==='[data-testid="send_message"]')return {textContent:'旧消息'};
   return null;
  },
  querySelectorAll(){return [];}
 };
 const context={window:{},document,location:{href:home},Date,setTimeout};
 load('doubao_web.js',context);
 await assert.rejects(()=>context.window.wechatDoubaoReply('虚构提示','new-contact',false,[],home,'turn-foreign'),/doubao_unowned_conversation/);
});

test('Doubao ignores delayed history and returns the reply after its visible marked prompt',async()=>{
 const time=clock(),home='https://www.doubao.com/chat/',url='https://www.doubao.com/chat/333';
 let sent=false,renamed='';
 const editor={textContent:'',focus(){},dispatchEvent(){}};
 const oldBody={innerText:'{"reply":"旧答案"}',getAttribute:()=> 'false'};
 const newBody={innerText:'{"reply":"新答案"}',getAttribute:()=> 'false'};
 const oldAnswer=turnNode(1,'旧答案',oldBody),newAnswer=turnNode(3,'新答案',newBody);
 let user=turnNode(2,'');
 const send={disabled:false,getAttribute:()=>null,click(){sent=true;user=turnNode(2,editor.textContent);}};
 const document={
  documentElement:{dataset:{}},
  querySelector(selector){
   if(selector.includes('contenteditable'))return editor;
   if(selector==='[data-testid="chat_input_send_button"]')return send;
   return null;
  },
  querySelectorAll(selector){
   if(selector==='[data-testid="send_message"]')return sent?[user]:[];
   if(selector==='[data-testid="receive_message"]')return sent&&time.now()>=1600?[oldAnswer,newAnswer]:sent&&time.now()>=800?[oldAnswer]:[];
   return [];
  },
  execCommand(_,__,value){editor.textContent=value;}
 };
 const location={origin:'https://www.doubao.com',get href(){return sent&&time.now()>=600?url:home;}};
 const context={window:{},document,location,chrome:extensionStorage(),Date:time.Date,setTimeout:time.setTimeout,clearTimeout:time.clearTimeout,MutationObserver:mutationObserver(time),InputEvent:class{},Node:{DOCUMENT_POSITION_FOLLOWING:4}};
 load('doubao_web.js',context);
 context.window.wechatDoubaoRename=async name=>{renamed=name;return true;};
 const prompt='系统\n\n以下是本轮微信数据：\n{"contact_profile":{"name":"虚构备注"}}\n\n只输出符合结构';
 const result=await context.window.wechatDoubaoReply(prompt,'contact-c',false,[],home,'turn-c');
 assert.equal(JSON.parse(result.reply).reply,'新答案');
 assert.equal(result.url,url);
 assert.equal(renamed,'虚构备注');
 assert.ok(time.now()>=1600);
});

test('Doubao accepts a new message ID when a virtual list recycles the old answer element',async()=>{
 const time=clock(),url='https://www.doubao.com/chat/444',dataset={wechatDoubaoOwner:'contact-d'};
 let sent=false;
 const editor={textContent:'',focus(){},dispatchEvent(){}};
 const body={
  get innerText(){return time.now()>=1200?'{"reply":"新答案"}':'{"reply":"旧答案"}';},
  getAttribute(name){return name==='data-streaming'?'false':name==='data-message-id'?(time.now()>=1200?'new-reply-id':'old-reply-id'):null;}
 };
 const recycled={order:1,textContent:'回复',querySelector(){return body;}};
 const userContent={getAttribute(name){return name==='data-message-id'?'new-user-id':null;}};
 const user={order:2,textContent:'',querySelector(){return userContent;},compareDocumentPosition(other){return other.order>2?4:2;}};
 const send={disabled:false,getAttribute:()=>null,click(){sent=true;user.textContent=editor.textContent;}};
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector.includes('contenteditable'))return editor;
   if(selector==='[data-testid="chat_input_send_button"]')return send;
   return null;
  },
  querySelectorAll(selector){
   if(selector==='[data-testid="send_message"]')return sent?[user]:[];
   if(selector==='[data-testid="receive_message"]'){if(sent&&time.now()>=1200)recycled.order=3;return [recycled];}
   return [];
  },
  execCommand(_,__,value){editor.textContent=value;}
 };
 const context={window:{},document,location:{origin:'https://www.doubao.com',href:url},chrome:extensionStorage(),Date:time.Date,setTimeout:time.setTimeout,clearTimeout:time.clearTimeout,MutationObserver:mutationObserver(time),InputEvent:class{},Node:{DOCUMENT_POSITION_FOLLOWING:4}};
 load('doubao_web.js',context);
 const result=await context.window.wechatDoubaoReply('虚构提示','contact-d',true,[],url,'turn-d');
 assert.equal(JSON.parse(result.reply).reply,'新答案');
 assert.equal(JSON.parse(dataset.wechatDoubaoGuard).afterIds[0],'new-reply-id');
});

test('Doubao fresh image turn allows home to upload URL to final URL only for its image and marked prompt',async()=>{
 const time=clock(),home='https://www.doubao.com/chat/',temporary=home+'local_555',final=home+'666';
 let uploaded=false,sent=false;
 const dataset={},editor={textContent:'',focus(){},dispatchEvent(){}};
 const idElement=id=>({getAttribute(name){return name==='data-message-id'?id:null;}});
 const imageUser={
  textContent:'',querySelector(selector){return selector==='[data-message-id]'?idElement('image-id'):selector==='img'?{naturalWidth:10}:null;},
  compareDocumentPosition(other){return other===markedUser?4:2;}
 };
 const markedUser={
  textContent:'',querySelector(selector){return selector==='[data-message-id]'?idElement('user-id'):null;},
  compareDocumentPosition(other){return other===answer?4:2;}
 };
 const body={innerText:'{"reply":"图像答复"}',getAttribute(name){return name==='data-streaming'?'false':null;}};
 const answer={querySelector(selector){return selector==='[data-message-id]'?idElement('reply-id'):selector==='[data-testid="message_text_content"]'?body:null;}};
 const upload={files:null,dispatchEvent(){uploaded=true;}};
 const card={getAttribute:()=> 'pic.png',querySelector:()=>({naturalWidth:10})};
 const area={
  querySelectorAll(selector){return selector==='[data-testid="attachment-image-card"]'&&uploaded?[card]:[];},
  querySelector(){return null;}
 };
 const send={disabled:false,getAttribute:()=>null,click(){sent=true;markedUser.textContent=editor.textContent;}};
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector.includes('contenteditable'))return editor;
   if(selector==='input[data-testid="upload-file-input"]')return upload;
   if(selector==='[data-testid="chat_input"]')return area;
   if(selector==='[data-testid="chat_input_send_button"]')return send;
   return null;
  },
  querySelectorAll(selector){
   if(selector==='[data-testid="attachment-image-card"]')return [];
   if(selector==='[data-testid="send_message"]')return sent?[imageUser,markedUser]:uploaded?[imageUser]:[];
   if(selector==='[data-testid="receive_message"]')return sent&&time.now()>=900?[answer]:[];
   return [];
  },
  execCommand(_,__,value){editor.textContent=value;}
 };
 const location={origin:'https://www.doubao.com',get href(){return sent?final:uploaded?temporary:home;}};
 class Transfer{constructor(){this.files=[];this.items={add:file=>this.files.push(file)};}}
 const context={window:{},document,location,chrome:extensionStorage(),Date:time.Date,setTimeout:time.setTimeout,clearTimeout:time.clearTimeout,MutationObserver:mutationObserver(time),InputEvent:class{},Event:class{},DataTransfer:Transfer,File:class{},atob:()=>'\x00',Node:{DOCUMENT_POSITION_FOLLOWING:4}};
 load('doubao_web.js',context);
 context.window.wechatDoubaoRename=async()=>true;
 const result=await context.window.wechatDoubaoReply('虚构图片提示','contact-image',false,[{data:'AA==',name:'pic.png',mime:'image/png'}],home,'turn-image');
 assert.equal(JSON.parse(result.reply).reply,'图像答复');
 assert.equal(result.url,final);
 assert.equal(JSON.parse(dataset.wechatDoubaoGuard).userId,'user-id');
 assert.equal(time.longFired(),0);
});

test('Doubao keeps the verified local chat mapping when its marked turn times out',async()=>{
 const time=clock(),home='https://www.doubao.com/chat/',local=home+'local_123456';
 const state={},dataset={},editor={textContent:'',focus(){},dispatchEvent(){}};
 let sent=false;
 const idElement={getAttribute(name){return name==='data-message-id'?'marked-user-id':null;}};
 const user={textContent:'',querySelector(selector){return selector==='[data-message-id]'?idElement:null;},compareDocumentPosition(){return 2;}};
 const send={disabled:false,getAttribute:()=>null,click(){sent=true;user.textContent=editor.textContent;}};
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector.includes('contenteditable'))return editor;
   if(selector==='[data-testid="chat_input_send_button"]')return send;
   return null;
  },
  querySelectorAll(selector){return selector==='[data-testid="send_message"]'&&sent?[user]:[];},
  execCommand(_,__,value){editor.textContent=value;}
 };
 const location={origin:'https://www.doubao.com',get href(){return sent?local:home;}};
 const context={window:{},document,location,chrome:extensionStorage(state),Date:time.Date,setTimeout:time.setTimeout,clearTimeout:time.clearTimeout,MutationObserver:mutationObserver(time),InputEvent:class{},Node:{DOCUMENT_POSITION_FOLLOWING:4}};
 load('doubao_web.js',context);
 await assert.rejects(()=>context.window.wechatDoubaoReply('虚构提示','contact-local',false,[],home,'turn-local','虚构备注'),/doubao_reply_timeout/);
 assert.equal(state.doubaoConversations['contact-local'].url,local);
 assert.equal(state.doubaoConversations['contact-local'].lastTurnId,'turn-local');
});

test('Doubao fresh UI falling into an existing chat retries the same owned tab at empty home',async()=>{
 const time=clock(),home='https://www.doubao.com/chat/';
 const tab={id:148,url:'https://www.doubao.com/chat/111'};
 const saved={doubaoOwnedTab:148,doubaoOwnedUrl:tab.url,doubaoConversations:{old:{url:tab.url}}};
 const updates=[];let setupCalls=0,resultBody;
 const chrome={
  storage:{local:{
   async get(key){return key==='token'?{token:'test-token'}:saved;},
   async set(values){Object.assign(saved,values);}
  }},
  tabs:{
   async get(){return tab;},
   async update(_,change){updates.push(change.url);tab.url=change.url;return tab;}
  },
  scripting:{async executeScript(options){
   if(options.files){assert.equal(tab.url,home);return [{result:null}];}
   if(options.args?.length===7){assert.equal(tab.url,home);tab.url='https://www.doubao.com/chat/999';return [{result:{reply:'{"reply":"新答案"}',url:tab.url}}];}
   if(options.args?.length===1)return [{result:{url:tab.url,editor:true,draft:false,attachments:false,answers:tab.url===home?0:2,users:tab.url===home?0:1,lastAnswerLength:0,hasKnownTurn:true}}];
   setupCalls++;
   return [{result:setupCalls===1?false:true}];
  }},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(url,options)=>{
  if(url.endsWith('/next'))return {ok:true,json:async()=>({job:{id:'turn-fresh',conversation_key:'new',prompt:'虚构提示',images:[],contact_name:'虚构备注'}})};
  if(url.endsWith('/result')){resultBody=JSON.parse(options.body);return {ok:true};}
  throw Error('unexpected fetch');
 };
 const context={chrome,fetch,WebSocket:class{},URL,Date:time.Date,setTimeout:time.setTimeout,console};
 load('doubao_background.js',context);
 await context.doubaoPump();
 assert.deepEqual(updates,[home]);
 assert.equal(resultBody.id,'turn-fresh');
 assert.equal(resultBody.result,'{"reply":"新答案"}');
 assert.equal(saved.doubaoConversations.new.url,'https://www.doubao.com/chat/999');
});

test('Doubao refuses an invalid persisted tab instead of creating another page',async()=>{
 let creates=0,resultBody;
 const saved={doubaoOwnedTab:77,doubaoOwnedUrl:'https://www.doubao.com/chat/777',doubaoConversations:{}};
 const chrome={
  storage:{local:{async get(key){return key==='token'?{token:'test-token'}:saved;},async set(values){Object.assign(saved,values);}}},
  tabs:{async get(){return {id:77,url:'https://example.com/'};},async create(){creates++;throw Error('must not create');}},
  runtime:{getURL:()=> 'chrome-extension://test/missing.json'},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(url,options)=>{
  if(url.endsWith('/next'))return {ok:true,json:async()=>({job:{id:'unsafe',conversation_key:'new',prompt:'虚构提示',images:[]}})};
  if(url.endsWith('/result')){resultBody=JSON.parse(options.body);return {ok:true};}
  throw Error('missing local bootstrap');
 };
 const context={chrome,fetch,WebSocket:class{},URL,setTimeout,console};
 load('doubao_background.js',context);
 await context.doubaoPump();
 assert.equal(creates,0);
 assert.equal(resultBody.error,'reply');
});

test('Doubao migrates once to an exact verified seed page without creating a tab',async()=>{
 let creates=0;
 const seedUrl='https://www.doubao.com/chat/666',seedKey='a'.repeat(64);
 const saved={doubaoOwnedTab:189,doubaoOwnedUrl:'https://www.doubao.com/chat/old',doubaoConversations:{}};
 const chrome={
  storage:{local:{async get(key){return key==='token'?{token:'test-token'}:saved;},async set(values){Object.assign(saved,values);}}},
  tabs:{async query(){return [{id:195,url:seedUrl}];},async get(){return {id:189,url:'https://www.doubao.com/chat/old'};},async create(){creates++;throw Error('must not create');}},
  runtime:{getURL:()=> 'chrome-extension://test/doubao-bootstrap.local.json'},
  scripting:{async executeScript(){throw Error('doubao_user_editing');}},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(url)=>{
  if(url.endsWith('/next'))return {ok:true,json:async()=>({job:{id:'seed',conversation_key:seedKey,prompt:'虚构提示',images:[]}})};
  if(url.endsWith('doubao-bootstrap.local.json'))return {json:async()=>({owned_url:seedUrl,seed:{conversation_key:seedKey,url:seedUrl,name:'虚构B'}})};
  return {ok:true};
 };
 const context={chrome,fetch,WebSocket:class{},URL,setTimeout,console};
 load('doubao_background.js',context);
 await context.doubaoPump();
 assert.equal(creates,0);
 assert.equal(saved.doubaoOwnedTab,195);
 assert.equal(saved.doubaoSeedApplied,seedUrl);
 assert.equal(saved.doubaoConversations[seedKey].url,seedUrl);
});

test('DeepSeek refuses an invalid persisted tab instead of creating another page',async()=>{
 let creates=0,resultBody;
 const saved={deepseekOwnedTab:208,deepseekOwnedUrl:'https://chat.deepseek.com/a/chat/s/old',deepseekConversations:{}};
 const chrome={
  storage:{local:{async get(key){return key==='token'?{token:'test-token'}:saved;},async set(values){Object.assign(saved,values);}}},
  tabs:{async get(){return {id:208,url:'https://example.com/'};},async create(){creates++;throw Error('must not create');}},
  runtime:{getURL:()=> 'chrome-extension://test/missing.json'},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(url,options)=>{
  if(url.endsWith('/next'))return {ok:true,json:async()=>({job:{id:'unsafe',conversation_key:'new',prompt:'虚构提示',images:[]}})};
  if(url.endsWith('/result')){resultBody=JSON.parse(options.body);return {ok:true};}
  throw Error('missing local bootstrap');
 };
 const context={chrome,fetch,WebSocket:class{},URL,setTimeout,console:{warn(){}}};
 load('deepseek_background.js',context);
 await context.deepseekPump();
 assert.equal(creates,0);
 assert.equal(resultBody.error,'reply');
});

test('DeepSeek migrates once to the exact verified latest conversation tab',async()=>{
 let creates=0;
 const seedUrl='https://chat.deepseek.com/a/chat/s/verified-latest',seedKey='b'.repeat(64);
 const saved={deepseekOwnedTab:140,deepseekOwnedUrl:'https://chat.deepseek.com/a/chat/s/old',deepseekConversations:{}};
 const chrome={
  storage:{
   local:{async get(key){return key==='token'?{token:'test-token'}:saved;},async set(values){Object.assign(saved,values);}},
   session:{async set(){}}
  },
  tabs:{async query(){return [{id:208,url:seedUrl}];},async get(){return {id:140,url:'https://chat.deepseek.com/a/chat/s/old'};},async create(){creates++;throw Error('must not create');}},
  runtime:{getURL:()=> 'chrome-extension://test/deepseek-bootstrap.local.json'},
  scripting:{async executeScript(){throw Error('deepseek_known_conversation_missing');}},
  alarms:{onAlarm:{addListener(){}}}
 };
 const fetch=async(url)=>{
  if(url.endsWith('/next'))return {ok:true,json:async()=>({job:{id:'seed',conversation_key:seedKey,prompt:'虚构提示',images:[]}})};
  if(url.endsWith('deepseek-bootstrap.local.json'))return {json:async()=>({owned_url:seedUrl,seed:{conversation_key:seedKey,url:seedUrl,name:'虚构联系人'}})};
  return {ok:true};
 };
 const context={chrome,fetch,WebSocket:class{},URL,setTimeout,console:{warn(){}}};
 load('deepseek_background.js',context);
 await context.deepseekPump();
 assert.equal(creates,0);
 assert.equal(saved.deepseekOwnedTab,208);
 assert.equal(saved.deepseekSeedApplied,seedUrl);
 assert.equal(saved.deepseekConversations[seedKey].url,seedUrl);
});

for(const [provider,ownedUrl,otherUrl] of [
 ['deepseek','https://chat.deepseek.com/a/chat/s/recovered','https://chat.deepseek.com/a/chat/s/other'],
 ['doubao','https://www.doubao.com/chat/666','https://www.doubao.com/chat/777']
]){
 function recoveryHarness(){
  const key='c'.repeat(64),recoveryId='recover-20261007-one-time';
  const saved={
   [provider+'OwnedTab']:10,[provider+'OwnedUrl']:otherUrl,[provider+'SeedApplied']:ownedUrl,
   [provider+'Conversations']:{[key]:{url:ownedUrl,name:'虚构备注',lastTurnId:'kept-turn',used:123,pending:false},other:{url:otherUrl,lastTurnId:'other-turn'}}
  };
  const bootstrap={owned_url:ownedUrl,recovery_id:recoveryId,seed:{conversation_key:key,url:ownedUrl,name:'不可覆盖原备注'}};
  const calls={queries:0,creates:0,updates:0,scripts:0,results:[],writes:[]};
  let openTabs=[{id:30,url:ownedUrl+'?not-exact=1'},{id:31,url:otherUrl},{id:32,url:ownedUrl}],liveId=32,job=null;
  const chrome={
   storage:{
    local:{async get(item){return item==='token'?{token:'test-token'}:structuredClone(saved);},async set(values){calls.writes.push(Object.keys(values));Object.assign(saved,structuredClone(values));}},
    session:{async set(){}}
   },
   tabs:{
    async query(){calls.queries++;return openTabs;},
    async get(id){if(id===liveId)return {id,url:ownedUrl};throw Error('tab closed');},
    async create(){calls.creates++;throw Error('must not create');},
    async update(){calls.updates++;throw Error('must not navigate');}
   },
   runtime:{getURL:()=>`chrome-extension://test/${provider}-bootstrap.local.json`},
   scripting:{async executeScript(){calls.scripts++;throw Error('must not generate a reply');}},
   alarms:{onAlarm:{addListener(){}}}
  };
  const fetch=async(url,options)=>{
   if(url.endsWith('-bootstrap.local.json'))return {json:async()=>structuredClone(bootstrap)};
   if(url.endsWith('/next'))return {ok:true,json:async()=>({job})};
   if(url.endsWith('/result')){calls.results.push(JSON.parse(options.body));return {ok:true};}
   throw Error('unexpected fetch');
  };
  const makeContext=()=>{
   const context={chrome,fetch,WebSocket:class{},URL,setTimeout,console:{warn(){}}};
   load(provider+'_background.js',context);return context;
  };
  return {saved,bootstrap,calls,key,recoveryId,makeContext,setTabs(tabs){openTabs=tabs;},closeTab(){liveId=null;},queueJob(){job={id:'after-close',conversation_key:key,prompt:'虚构提示',images:[]};}};
 }

 test(`${provider} recovers one exact reopened tab without a queued job and preserves every conversation`,async()=>{
  const fixture=recoveryHarness(),before=structuredClone(fixture.saved[provider+'Conversations']);
  const context=fixture.makeContext();
  await context[provider+'Pump']();
  assert.equal(fixture.saved[provider+'OwnedTab'],32);
  assert.equal(fixture.saved[provider+'OwnedUrl'],ownedUrl);
  assert.equal(fixture.saved[provider+'RecoveryApplied'],fixture.recoveryId);
  assert.deepEqual(fixture.saved[provider+'Conversations'],before);
  assert.equal(fixture.calls.writes.some(keys=>keys.includes(provider+'Conversations')),false);
  assert.equal(fixture.calls.queries,1);
  assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.scripts,0);
  assert.equal(fixture.calls.results.length,0);
 });

 test(`${provider} cannot reuse a consumed recovery nonce after closure or a worker restart`,async()=>{
  const fixture=recoveryHarness(),before=structuredClone(fixture.saved[provider+'Conversations']);
  await fixture.makeContext()[provider+'Pump']();
  delete fixture.saved[provider+'SeedApplied'];
  fixture.closeTab();fixture.setTabs([{id:99,url:ownedUrl}]);fixture.queueJob();
  await fixture.makeContext()[provider+'Pump']();
  assert.equal(fixture.saved[provider+'OwnedTab'],32);
  assert.equal(fixture.saved[provider+'RecoveryApplied'],fixture.recoveryId);
  assert.equal(fixture.calls.queries,1);
  assert.equal(fixture.calls.results.at(-1).error,'reply');
  delete fixture.saved[provider+'OwnedTab'];
  await fixture.makeContext()[provider+'Pump']();
  assert.equal(fixture.calls.queries,1);
  assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.scripts,0);
  assert.deepEqual(fixture.saved[provider+'Conversations'],before);
 });

 test(`${provider} leaves recovery unused until exactly one known conversation tab is present`,async()=>{
  const fixture=recoveryHarness(),context=fixture.makeContext();
  fixture.setTabs([{id:33,url:ownedUrl+'?not-exact=1'}]);
  await context[provider+'Pump']();
  assert.equal(fixture.saved[provider+'RecoveryApplied'],undefined);
  assert.equal(fixture.saved[provider+'OwnedTab'],10);
  fixture.setTabs([{id:34,url:ownedUrl},{id:35,url:ownedUrl}]);
  await context[provider+'Pump']();
  assert.equal(fixture.saved[provider+'RecoveryApplied'],undefined);
  fixture.setTabs([{id:32,url:ownedUrl}]);
  await context[provider+'Pump']();
  assert.equal(fixture.saved[provider+'OwnedTab'],32);
  assert.equal(fixture.saved[provider+'RecoveryApplied'],fixture.recoveryId);
  assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.scripts,0);
 });

 test(`${provider} refuses recovery without ownership proof or a valid nonce`,async()=>{
  const fixture=recoveryHarness(),before=structuredClone(fixture.saved);
  fixture.bootstrap.owned_url=ownedUrl+'-unknown';
  fixture.setTabs([{id:32,url:fixture.bootstrap.owned_url}]);
  const context=fixture.makeContext();await context[provider+'Pump']();
  fixture.bootstrap.owned_url=ownedUrl;fixture.bootstrap.recovery_id='short';
  fixture.setTabs([{id:32,url:ownedUrl}]);await context[provider+'Pump']();
  assert.deepEqual(fixture.saved,before);
  assert.equal(fixture.calls.queries,0);
  assert.equal(fixture.calls.creates+fixture.calls.updates+fixture.calls.scripts,0);
 });
}

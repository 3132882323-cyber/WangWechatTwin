const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const editorSelector='textarea[placeholder="给 DeepSeek 发送消息 "]';
const marker='[wechat-turn:submission-test]';

function fixture({replaceOnInput=false,losePromptOnReplace=false,requireLegacyEnter=false,withControl=false,sendDisabled=false,sendIcon='arrow',wrongSize=false,duplicateControl=false,enableThought=false,initialUrl='https://chat.deepseek.com/a/chat/s/kept',reuse=true,onTick,onSend,onFocus}={}){
 let now=0,editorNumber=0;
 const url=initialUrl,dataset={wechatDeepseekOwner:'contact'};
 const state={calls:[],editors:[],inputEditors:[],blocks:[],answerNodes:[],focusCalls:0,attachmentClicks:0,sendDisabled,sendIcon,wrongSize,thinkingPressed:enableThought,location:{href:url},dataset};
 class TestDate extends Date{static now(){return now;}}
 class Textarea{
  constructor(){this.id=++editorNumber;this.isConnected=true;this._value='';this.parentElement={parentElement:state.scope};state.editors.push(this);}
  get value(){return this._value;}
  set value(value){this._value=value;}
  focus(){state.focusCalls++;if(onFocus)onFocus(state);}
  dispatchEvent(event){
   if(event.type==='input')state.inputEditors.push(this.id);
   if(event.type==='input'&&replaceOnInput&&this.id===1){
    this.isConnected=false;
    state.editor=new Textarea();state.editor._value=losePromptOnReplace?'':this._value;
   }
   if(event.key==='Enter'){
    state.calls.push({kind:'enter',editor:this.id,connected:this.isConnected,at:now,key:event.key,keyCode:event.keyCode,which:event.which});
    if(this.isConnected&&(!requireLegacyEnter||event.keyCode===13&&event.which===13))(onSend||((s)=>s.accept()))(state);
   }
   return true;
  }
 }
 const node=(order,textContent,answer=null)=>({order,textContent,querySelector(){return answer;},compareDocumentPosition(other){return other.order>order?4:2;}});
 const oldAnswer={textContent:'old reply',innerText:'old reply'};
 state.blocks=reuse?[node(1,'old user'),node(2,'old reply',oldAnswer)]:[];state.answerNodes=reuse?[oldAnswer]:[];
 const makeButton=()=>({
  get isConnected(){return !state.controlDisconnected;},
  get disabled(){return !!state.nativeDisabled;},
  getAttribute(name){if(name==='class')return 'ds-button ds-button--primary ds-button--filled ds-button--circle'+(state.sendDisabled?' ds-button--disabled':'');if(name==='aria-disabled')return state.ariaDisabled?'true':null;return null;},
  querySelectorAll(selector){if(selector==='svg[width="16"][height="16"] path'&&!state.wrongSize)return [{getAttribute(){return state.sendIcon==='arrow'?'M8.3125 0.980206 L9 3.95579V15.0417H7V3.95579L2.70703 8.24876':'M4 4H12V12H4Z';}}];return [];},
  click(){state.calls.push({kind:'button',editor:state.editor.id,connected:state.editor.isConnected,at:now});(onSend||((s)=>s.accept()))(state);}
 });
 state.primaryControls=withControl?[makeButton(),...(duplicateControl?[makeButton()]:[])]:[];
 state.createControl=makeButton;
 const attachment={click(){state.attachmentClicks++;}};
 state.scope={
  querySelector(selector){return selector===editorSelector?state.editor:null;},
  querySelectorAll(selector){
   if(selector==='[role="button"].ds-button--primary.ds-button--filled.ds-button--circle')return state.primaryControls;
   if(selector==='[role="button"]')return [attachment,...state.primaryControls];
   return [];
  }
 };
 state.editor=new Textarea();
 state.replaceEditor=(value='')=>{state.editor.isConnected=false;state.editor=new Textarea();state.editor._value=value;};
 state.accept=({showMarker=true,reply='{"reply":"submission accepted"}',clearEditor=true}={})=>{
  if(clearEditor&&state.editor)state.editor._value='';
  if(showMarker)state.blocks.push(node(3,marker+' own submitted prompt'));
  if(reply){const answer={textContent:reply,innerText:reply};state.blocks.push(node(4,reply,answer));state.answerNodes.push(answer);}
 };
 state.addForeignTurn=(order=3)=>state.blocks.push(node(order,'foreign user turn'));
 const controls=['深度思考','智能搜索'].map((textContent,index)=>({textContent,getAttribute(){return index===0&&state.thinkingPressed?'true':'false';},click(){state.thinkingPressed=false;}}));
 const document={
  documentElement:{dataset},
  querySelector(selector){
   if(selector===editorSelector)return state.editor;
   if(selector==='.ds-virtual-list-visible-items')return {children:state.blocks};
   return null;
  },
  querySelectorAll(selector){
   if(selector==='.ds-toggle-button')return controls;
   if(selector==='.ds-assistant-message-main-content')return state.answerNodes;
   return [];
  }
 };
 const saved={deepseekConversations:{contact:{url,name:'contact',lastTurnId:'old'}}};
 const context={window:{},document,location:state.location,HTMLTextAreaElement:Textarea,
  Event:class{constructor(type,options){this.type=type;Object.assign(this,options);}},
  KeyboardEvent:class{constructor(type,options){this.type=type;Object.assign(this,options);}},
  Node:{DOCUMENT_POSITION_FOLLOWING:4},Date:TestDate,
  setTimeout(done,ms){now+=ms;if(onTick)onTick(state,now,ms);done();return 1;},
  chrome:{storage:{local:{async get(){return structuredClone(saved);},async set(values){Object.assign(saved,structuredClone(values));}}}}
 };
 const reload=()=>vm.runInNewContext(fs.readFileSync(path.join(__dirname,'deepseek_web.js'),'utf8'),context,{filename:'deepseek_web.js'});
 reload();
 return {state,saved,reload,now:()=>now,run:(turnId='submission-test')=>context.window.wechatDeepseekDraft('synthetic prompt','contact',reuse,url,turnId)};
}

test('DeepSeek submits on the current textarea after React replaces the input node',async()=>{
 const f=fixture({replaceOnInput:true});
 const result=await f.run();
 assert.equal(JSON.parse(result.reply).reply,'submission accepted');
 assert.equal(f.state.editors[0].isConnected,false);
 assert.deepEqual(f.state.calls.map(c=>[c.editor,c.connected]),[[2,true]]);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'submission-test');
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'reply_received');
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
});

test('DeepSeek never submits an old detached input when the replacement loses its prompt',async()=>{
 const f=fixture({replaceOnInput:true,losePromptOnReplace:true});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
 assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.editor.value,'');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek records unconfirmed submission without exposing or reinserting the prompt',async()=>{
 const f=fixture({onSend(){}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
 assert.equal(f.state.dataset.wechatDeepseekSubmitEvidence,'none');
 assert.ok(f.state.editor.value.includes('synthetic prompt'));
 assert.equal(JSON.stringify(f.state.dataset).includes('synthetic prompt'),false);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek never resubmits when the composer clears then restores the same prompt',async()=>{
 let ownPrompt='';
 const f=fixture({onSend(s){ownPrompt=s.editor.value;s.editor._value='';},onTick(s,now){if(now===800)s.editor._value=ownPrompt;}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.editor.value,ownPrompt);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.ok(f.state.dataset.wechatDeepseekSubmitEvidence.split(',').includes('editor_cleared'));
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek waits for a late marker after acceptance evidence and returns only its answer',async()=>{
 let accepted=false;
 const f=fixture({onSend(s){s.editor._value='';},onTick(s,now){if(now>=4000&&!accepted){accepted=true;s.accept();}}});
 const result=await f.run();
 assert.equal(JSON.parse(result.reply).reply,'submission accepted');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'reply_received');
 assert.ok(f.now()>=4800);
});

test('DeepSeek leaves a foreign user turn before its marker unconfirmed and never resubmits',async()=>{
 const f=fixture({onSend(s){s.addForeignTurn();}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.ok(f.state.dataset.wechatDeepseekSubmitEvidence.split(',').includes('history_changed'));
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek stops after submission when the contact owner changes',async()=>{
 const f=fixture({onSend(){},onTick(s,now){if(now===800)s.dataset.wechatDeepseekOwner='other';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_ownership_lost');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek stops after submission when the conversation URL changes',async()=>{
 const f=fixture({onSend(){},onTick(s,now){if(now===800)s.location.href='https://chat.deepseek.com/a/chat/s/other';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_conversation_changed');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.ok(f.state.dataset.wechatDeepseekSubmitEvidence.split(',').includes('url_changed'));
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek never submits after its current textarea disappears during the input settle',async()=>{
 const f=fixture({onTick(s,now){if(now===400){s.editor.isConnected=false;s.editor=null;}}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
 assert.deepEqual(f.state.calls,[]);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek never overwrites a foreign composer edit caused by focus before submission',async()=>{
 const f=fixture({onFocus(s){if(s.focusCalls===2)s.editor._value='human edit';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
 assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.editor.value,'human edit');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek submits a single compatible Enter event for legacy keyboard handlers',async()=>{
 const f=fixture({requireLegacyEnter:true});
 const result=await f.run();
 assert.equal(JSON.parse(result.reply).reply,'submission accepted');
 assert.equal(f.state.calls.length,1);
 assert.equal(f.state.calls[0].key,'Enter');
 assert.equal(f.state.calls[0].keyCode,13);assert.equal(f.state.calls[0].which,13);
});

test('DeepSeek clicks its verified composer send arrow once without touching attachment or Enter',async()=>{
 const f=fixture({withControl:true});
 const result=await f.run();
 assert.equal(JSON.parse(result.reply).reply,'submission accepted');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.state.attachmentClicks,0);
 assert.equal(f.state.dataset.wechatDeepseekSubmitMethod,'button');
 assert.equal(f.state.dataset.wechatDeepseekSubmitTurnId,'submission-test');
});

test('DeepSeek waits for a verified disabled send control before its first and only dispatch',async()=>{
 const f=fixture({withControl:true,sendDisabled:true,onTick(s,now){if(now===800)s.sendDisabled=false;}});
 await f.run();
 assert.deepEqual(f.state.calls.map(c=>[c.kind,c.at]),[['button',800]]);
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
});

for(const [name,options] of [
 ['disabled send control',{withControl:true,sendDisabled:true}],
 ['generation Stop icon',{withControl:true,sendIcon:'stop'}],
 ['unverified SVG size',{withControl:true,wrongSize:true}],
 ['multiple canonical controls',{withControl:true,duplicateControl:true}]
]){
 test(`DeepSeek never bypasses a ${name} with Enter or another click`,async()=>{
  const f=fixture(options);
  await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
  assert.deepEqual(f.state.calls,[]);
  assert.equal(f.state.attachmentClicks,0);
  assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'not_ready');
  assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'0');
  assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
  assert.ok(f.now()>=2400);
 });
}

for(const field of ['ariaDisabled','nativeDisabled','controlDisconnected']){
 test(`DeepSeek respects ${field} on a matching send control`,async()=>{
  const f=fixture({withControl:true});f.state[field]=true;
  await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
  assert.deepEqual(f.state.calls,[]);
  assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'0');
 });
}

test('DeepSeek does not choose Enter when an observed canonical control disappears while waiting',async()=>{
 const f=fixture({withControl:true,sendDisabled:true,onTick(s,now){if(now===800)s.primaryControls=[];}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
 assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'not_ready');
});

test('DeepSeek does not choose Enter when its initial canonical control disappears during input settle',async()=>{
 const f=fixture({withControl:true,onTick(s,now){if(now===400)s.primaryControls=[];}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
 assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'not_ready');
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'0');
});

test('DeepSeek latches a canonical control first observed after mode settle',async()=>{
 const f=fixture({enableThought:true,onTick(s,now){if(now===300)s.primaryControls=[s.createControl()];if(now===700)s.primaryControls=[];}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_prompt_not_inserted');
 assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'not_ready');
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'0');
});

test('DeepSeek can wait for its disappeared canonical send control to return before the first dispatch',async()=>{
 let keptControl;
 const f=fixture({withControl:true,onTick(s,now){if(now===400){keptControl=s.primaryControls[0];s.primaryControls=[];}if(now===800)s.primaryControls=[keptControl];}});
 await f.run();
 assert.deepEqual(f.state.calls.map(c=>[c.kind,c.at]),[['button',800]]);
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
});

test('DeepSeek reacquires an empty textarea replaced while a mode toggle settles',async()=>{
 const f=fixture({withControl:true,enableThought:true,onTick(s,now){if(now===300)s.replaceEditor();}});
 const result=await f.run();
 assert.equal(JSON.parse(result.reply).reply,'submission accepted');
 assert.deepEqual(f.state.inputEditors,[2]);
 assert.equal(f.state.editors[0].value,'');
 assert.deepEqual(f.state.calls.map(c=>[c.kind,c.editor]),[['button',2]]);
});

for(const humanDraft of ['human draft',' ','\n']){
 test(`DeepSeek preserves a human draft (${JSON.stringify(humanDraft)}) typed while a mode toggle settles`,async()=>{
  const f=fixture({enableThought:true,onTick(s,now){if(now===300)s.editor._value=humanDraft;}});
  await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_user_editing_or_not_logged_in');
  assert.deepEqual(f.state.inputEditors,[]);assert.deepEqual(f.state.calls,[]);
  assert.equal(f.state.editor.value,humanDraft);
  assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'0');
 });
}

test('DeepSeek stops before inserting on a foreign user turn during mode settle',async()=>{
 const f=fixture({enableThought:true,onTick(s,now){if(now===300)s.addForeignTurn();}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_newer_user_turn');
 assert.deepEqual(f.state.inputEditors,[]);assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.editor.value,'');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek stops before dispatch on a foreign user turn during input settle',async()=>{
 const f=fixture({withControl:true,onTick(s,now){if(now===400)s.addForeignTurn();}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_newer_user_turn');
 assert.deepEqual(f.state.calls,[]);
 assert.ok(f.state.editor.value.includes('synthetic prompt'));
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'0');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek stops before inserting when the owner changes during mode settle',async()=>{
 const f=fixture({enableThought:true,onTick(s,now){if(now===300)s.dataset.wechatDeepseekOwner='other';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_wrong_contact');
 assert.deepEqual(f.state.inputEditors,[]);assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.editor.value,'');
});

test('DeepSeek stops before inserting when the conversation changes during mode settle',async()=>{
 const f=fixture({enableThought:true,onTick(s,now){if(now===300)s.location.href='https://chat.deepseek.com/a/chat/s/other';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_conversation_changed');
 assert.deepEqual(f.state.inputEditors,[]);assert.deepEqual(f.state.calls,[]);
 assert.equal(f.state.editor.value,'');
});

test('DeepSeek rejects a newer foreign turn after its own accepted marker without dispatching again',async()=>{
 const f=fixture({withControl:true,onSend(s){s.accept();s.addForeignTurn(5);}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_newer_user_turn');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek latches a short composer busy pulse without retrying after it becomes ready',async()=>{
 const f=fixture({withControl:true,onSend(s){s.sendIcon='stop';},onTick(s,now){if(now===800)s.sendIcon='arrow';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(f.state.dataset.wechatDeepseekSubmitEvidence,'composer_busy');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek preserves a manual draft entered after its one dispatch',async()=>{
 const humanDraft='manual follow-up after submission';
 const f=fixture({withControl:true,onSend(){},onTick(s,now){if(now===800)s.editor._value=humanDraft;}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.deepEqual(f.state.inputEditors,[1]);
 assert.equal(f.state.editor.value,humanDraft);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(f.state.dataset.wechatDeepseekSubmitEvidence,'editor_changed');
 assert.equal(JSON.stringify(f.state.dataset).includes(humanDraft),false);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek leaves an allowed home-to-chat transition unconfirmed until its own marker appears',async()=>{
 const f=fixture({initialUrl:'https://chat.deepseek.com/',reuse:false,withControl:true,onSend(s){s.editor._value='';s.location.href='https://chat.deepseek.com/a/chat/s/new';}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(f.state.dataset.wechatDeepseekSubmitEvidence,'editor_cleared,url_changed');
 assert.equal(f.saved.deepseekConversations.contact.url,'https://chat.deepseek.com/');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek accepts a home-to-chat answer only after its own late marker appears',async()=>{
 let accepted=false;
 const f=fixture({initialUrl:'https://chat.deepseek.com/',reuse:false,withControl:true,onSend(s){s.editor._value='';s.location.href='https://chat.deepseek.com/a/chat/s/new';},onTick(s,now){if(now>=4000&&!accepted){accepted=true;s.accept();}}});
 const result=await f.run();
 assert.equal(JSON.parse(result.reply).reply,'submission accepted');
 assert.equal(result.url,'https://chat.deepseek.com/a/chat/s/new');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.saved.deepseekConversations.contact.url,result.url);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'submission-test');
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'reply_received');
 assert.ok(f.state.dataset.wechatDeepseekSubmitEvidence.split(',').includes('url_changed'));
});

test('DeepSeek remembers a visible marked turn after the virtual list hides it and rejects unrelated answers',async()=>{
 const f=fixture({withControl:true,onSend(s){s.accept({reply:''});},onTick(s,now){if(now===800){s.blocks=s.blocks.filter(node=>!node.textContent.includes(marker));s.accept({showMarker:false,reply:'{"reply":"unproven answer"}'});}}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_reply_timeout');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'marker_seen');
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'submission-test');
});

test('DeepSeek never repeats an unconfirmed turn after reinjection even when the composer is empty',async()=>{
 const f=fixture({withControl:true,onSend(){}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 const receipt=structuredClone(f.state.dataset);
 f.state.editor._value='';f.reload();
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.deepEqual(f.state.inputEditors,[1]);
 assert.equal(f.state.editor.value,'');
 for(const name of ['wechatDeepseekSubmitAttempts','wechatDeepseekSubmitStatus','wechatDeepseekSubmitTurnId','wechatDeepseekSubmitMethod','wechatDeepseekSubmitEvidence'])assert.equal(f.state.dataset[name],receipt[name]);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

test('DeepSeek preserves a manual draft and receipt when an unconfirmed turn is invoked again',async()=>{
 const f=fixture({withControl:true,onSend(){}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 f.state.editor._value='manual draft';
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_turn_not_visible');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.deepEqual(f.state.inputEditors,[1]);
 assert.equal(f.state.editor.value,'manual draft');
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(JSON.stringify(f.state.dataset).includes('manual draft'),false);
});

test('DeepSeek records a throwing dispatch as unconfirmed using only redacted diagnostics',async()=>{
 const privateDetail='private prompt detail from send handler';
 const f=fixture({withControl:true,onSend(){throw Error(privateDetail);}});
 await assert.rejects(f.run(),error=>error.deepseekCode==='deepseek_unexpected_error');
 assert.deepEqual(f.state.calls.map(c=>c.kind),['button']);
 assert.equal(f.state.dataset.wechatDeepseekSubmitStatus,'unconfirmed');
 assert.equal(f.state.dataset.wechatDeepseekSubmitAttempts,'1');
 assert.equal(JSON.stringify(f.state.dataset).includes(privateDetail),false);
 assert.equal(JSON.stringify(f.state.dataset).includes('synthetic prompt'),false);
 assert.equal(f.saved.deepseekConversations.contact.lastTurnId,'old');
});

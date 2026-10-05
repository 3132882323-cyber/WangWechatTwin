document.documentElement.dataset.wechatBridgeVersion='3';
window.wechatWebReplyInner=async function(prompt){
 const stage=s=>document.documentElement.dataset.wechatBridgeStage=s;
 stage('personalization');
 const deadline=Date.now()+140000;
 const sleep=()=>new Promise(r=>setTimeout(r,300));
 const click=e=>{
  e.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,cancelable:true,composed:true,pointerType:'mouse',pointerId:1,isPrimary:true,button:0,buttons:1}));
  e.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,cancelable:true,composed:true,pointerType:'mouse',pointerId:1,isPrimary:true,button:0,buttons:0}));
  e.click();
 };
 const visible=e=>e&&!e.closest('[aria-hidden="true"]')&&getComputedStyle(e).visibility!=='hidden'&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0;
 const find=(selector,text)=>Array.from(document.querySelectorAll(selector)).filter(visible).find(e=>(e.getAttribute('aria-label')||e.textContent||'').trim()===text);
 const wait=async(fn)=>{while(Date.now()<deadline){const v=fn();if(v)return v;await sleep();}throw Error('ui timeout');};
 // Never type before these isolation checks have passed.
 if(!location.search.includes('temporary-chat=true'))throw Error('not temporary');
 let personalized=await wait(()=>find('button','个性化')||find('button','不个性化'));
 if((personalized.textContent||'').trim()==='个性化'){
  click(personalized);click(await wait(()=>find('[role="menuitemradio"]','不个性化 此聊天不会使用记忆、插件和自定义指令')));
 }
 await wait(()=>find('button','不个性化'));
 stage('model-menu');
 const work=Array.from(document.querySelectorAll('[role="radio"],input[type="radio"]')).filter(visible).find(e=>(e.getAttribute('aria-label')||e.textContent||'').trim()==='Work');
 if(work&& (work.getAttribute('aria-checked')==='true'||work.checked))throw Error('work mode');
 click(await wait(()=>find('button','选择 ChatGPT 模型')));
 const modelView=await wait(()=>find('[role="menuitem"]','选择模型'));click(modelView);
 stage('model-choice');
 click(await wait(()=>find('[role="menuitemradio"]','GPT-5.6 Sol')));
 // Select Instant through the menu's documented keyboard interaction.
 const effort=await wait(()=>find('[role="menuitem"]','强度'));effort.focus();
 stage('effort');
 for(let i=0;i<5;i++){effort.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',code:'ArrowLeft',bubbles:true}));await sleep();}
 if(!Array.from(document.querySelectorAll('[role="menu"]')).some(e=>visible(e)&&e.textContent.includes('Instant')))throw Error('Instant not verified');
 click(await wait(()=>find('button','选择 ChatGPT 模型')));
 stage('composer');
 const composer=await wait(()=>Array.from(document.querySelectorAll('[role="textbox"][contenteditable="true"][aria-label="询问 ChatGPT"]')).find(visible));
 composer.focus();document.execCommand('insertText',false,prompt);
 const normalized=s=>s.replace(/[\s\u200B]+/g,'');
 if(normalized(composer.textContent)!==normalized(prompt))throw Error('prompt not inserted');
 click(await wait(()=>{const e=find('button','发送');return e&&!e.disabled?e:null;}));
 stage('response');
 // Only new assistant DOM in this freshly created tab; no historic conversation read.
 const response=await wait(()=>{if(find('button','停止'))return null;const a=Array.from(document.querySelectorAll('[data-message-author-role="assistant"],[data-content-search-unit-key$=":assistant"]')).filter(visible);if(!a.length)return null;const e=a[a.length-1];return e.querySelector(':scope > div')?.innerText||e.innerText;});
 const cleaned=response.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');JSON.parse(cleaned);return cleaned;
};
window.wechatWebReply=async function(prompt){
 try{return await window.wechatWebReplyInner(prompt);}catch(e){
  console.error('WeChatWebBridge: '+e.message);
  await new Promise(r=>setTimeout(r,45000));
  throw e;
 }
};
void 0;

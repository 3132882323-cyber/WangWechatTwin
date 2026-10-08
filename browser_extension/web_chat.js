document.documentElement.dataset.wechatBridgeVersion='4';
window.wechatWebReplyInner=async function(prompt,key,reused=false,images=[]){
 const {bridgeWorkerVersion}=await chrome.storage.local.get('bridgeWorkerVersion');
 if(bridgeWorkerVersion!==4)throw Error('update_extension_before_input');
 const stage=s=>document.documentElement.dataset.wechatBridgeStage=s;
 stage('personalization');
 const deadline=Date.now()+200000;
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
 const root=document.documentElement;
 const assistants=()=>Array.from(document.querySelectorAll('[data-message-author-role="assistant"],[data-content-search-unit-key$=":assistant"]')).filter(visible);
 const identity=e=>e.getAttribute('data-chatgpt-search-message-ids')||e.getAttribute('data-message-id')||e.getAttribute('data-content-search-unit-key');
 if(reused){
  if(!root.dataset.wechatBridgeOwner||!root.dataset.wechatBridgeIsolation)throw Error('context_marker_lost_before_input');
  if(root.dataset.wechatBridgeOwner!==key||root.dataset.wechatBridgeIsolation!=='unpersonalized')throw Error('ownership_lost_before_input');
  const last=assistants().at(-1);
  if(!last||identity(last)!==root.dataset.wechatBridgeLastReply)throw Error('outside_turn_before_input');
 }else{
  if(assistants().length)throw Error('unexpected_existing_chat');
  let personalized=await wait(()=>find('button','个性化')||find('button','不个性化'));
  if((personalized.textContent||'').trim()==='个性化'){
   click(personalized);click(await wait(()=>find('[role="menuitemradio"]','不个性化 此聊天不会使用记忆、插件和自定义指令')));
  }
  await wait(()=>find('button','不个性化'));
  root.dataset.wechatBridgeOwner=key;root.dataset.wechatBridgeIsolation='unpersonalized';
 }
 stage('model-menu');
 const work=Array.from(document.querySelectorAll('[role="radio"],input[type="radio"]')).filter(visible).find(e=>(e.getAttribute('aria-label')||e.textContent||'').trim()==='Work');
 if(work&& (work.getAttribute('aria-checked')==='true'||work.checked))throw Error('work mode');
 const modelButton=find('button','选择 ChatGPT 模型');
 const cachedModel=reused&&root.dataset.wechatBridgeModel==='gpt6-maximum'&&modelButton?.textContent.includes('Pro');
 if(!cachedModel){
 let modelView=null;
 for(let attempt=0;attempt<8&&!modelView;attempt++){
  const button=await wait(()=>find('button','选择 ChatGPT 模型'));click(button);
  for(let tick=0;tick<5;tick++){await sleep();modelView=find('[role="menuitem"]','选择模型');if(modelView)break;}
 }
 if(!modelView)throw Error('model_menu_not_ready_before_input');
 click(modelView);
 stage('model-choice');
 click(await wait(()=>Array.from(document.querySelectorAll('[role="menuitemradio"]')).find(e=>visible(e)&&e.textContent.trim()==='GPT-6')));
 // Select the highest available effort through documented keyboard interaction.
 const effort=await wait(()=>find('[role="menuitem"]','强度'));effort.focus();
 const effortSlider=()=>effort.querySelector('[role="slider"]')||effort.closest('[role="menu"]')?.querySelector('[role="slider"]');
 stage('effort');
 for(let i=0;i<10;i++){
  const slider=effortSlider();
  if(slider&&Number(slider.getAttribute('aria-valuenow'))===Number(slider.getAttribute('aria-valuemax')))break;
  effort.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',code:'ArrowRight',bubbles:true}));await sleep();
 }
 const slider=effortSlider();
 if(!slider||Number(slider.getAttribute('aria-valuenow'))!==Number(slider.getAttribute('aria-valuemax'))||!Array.from(document.querySelectorAll('[role="menu"]')).some(e=>visible(e)&&e.textContent.includes('Pro')))throw Error('Maximum reasoning not verified');
 click(await wait(()=>find('button','选择 ChatGPT 模型')));
 root.dataset.wechatBridgeModel='gpt6-maximum';
 if(!window.wechatModelWatch){
  window.wechatModelWatch=true;
  document.addEventListener('pointerdown',e=>{if(e.isTrusted&&e.target.closest('button[aria-label="选择 ChatGPT 模型"]'))root.dataset.wechatBridgeModel='';},true);
 }
 }
 stage('composer');
 const composer=await wait(()=>Array.from(document.querySelectorAll('[role="textbox"][contenteditable="true"][aria-label="询问 ChatGPT"]')).find(visible));
 if(composer.textContent.trim()||find('button','停止'))throw Error('user_editing_before_input');
 const previousReplies=new Set(assistants().map(identity));
 if(images.length){
  const input=await wait(()=>Array.from(document.querySelectorAll('input[type="file"]')).find(e=>!e.accept||e.accept.includes('image')||e.accept.includes('.png')));
  const transfer=new DataTransfer();
  for(const img of images){const bytes=Uint8Array.from(atob(img.data),c=>c.charCodeAt(0));transfer.items.add(new File([bytes],img.name,{type:img.mime}));}
  input.files=transfer.files;input.dispatchEvent(new Event('change',{bubbles:true}));
  await wait(()=>{const labels=document.body.textContent+' '+Array.from(document.querySelectorAll('[aria-label],[title],[alt]')).map(e=>e.getAttribute('aria-label')||e.getAttribute('title')||e.getAttribute('alt')).join(' ');return images.every(img=>labels.includes(img.name));});
 }
 composer.focus();document.execCommand('insertText',false,prompt);
 composer.dispatchEvent(new InputEvent('input',{bubbles:true,composed:true,inputType:'insertText',data:prompt}));
 // Attachments enable Send before React/ProseMirror has synchronized the text.
 // Let both DOM observation and UI state commit before clicking an enabled button.
 await sleep();
 const normalized=s=>s.replace(/[\s\u200B]+/g,'');
 if(normalized(composer.textContent)!==normalized(prompt))throw Error('prompt not inserted');
 click(await wait(()=>{const e=find('button','发送');return e&&!e.disabled?e:null;}));
 stage('response');
 // Never return the previous turn's answer, including identical wording.
 const node=await wait(()=>{if(find('button','停止'))return null;const e=assistants().at(-1);const id=e&&identity(e);return id&&!previousReplies.has(id)&&(e.innerText||'').trim()?e:null;});
 const response=node.querySelector(':scope > div')?.innerText||node.innerText;
 const cleaned=response.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');JSON.parse(cleaned);
 root.dataset.wechatBridgeLastReply=identity(node);return cleaned;
};
window.wechatWebReply=async function(prompt,key,reused,images){
 try{const result=await window.wechatWebReplyInner(prompt,key,reused,images);
  return result;
 }catch(e){
  console.error('WeChatWebBridge: '+e.message);
  throw e;
 }
};
void 0;

window.wechatDeepseekRename=async function(name){
 if(location.origin!=='https://chat.deepseek.com'||!location.pathname.startsWith('/a/chat/s/'))return false;
 const link=Array.from(document.querySelectorAll('a[href]')).find(a=>a.getAttribute('href')===location.pathname);
 if(!link||!name)return false;if(link.textContent.trim()===name)return true;
 const menu=link.querySelector('[role="button"]');if(!menu)return false;menu.click();
 await new Promise(r=>setTimeout(r,150));
 const item=Array.from(document.querySelectorAll('[role="menu"] *')).find(e=>e.textContent.trim()==='重命名'&&e.children.length===0);
 if(!item)return false;item.click();await new Promise(r=>setTimeout(r,150));
 const input=link.querySelector('input.ds-input__input')||document.querySelector('input.ds-input__input');if(!input)return false;
 Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,name);
 input.dispatchEvent(new Event('input',{bubbles:true}));input.dispatchEvent(new Event('change',{bubbles:true}));
 input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
 await new Promise(r=>setTimeout(r,200));return link.textContent.trim()===name;
};

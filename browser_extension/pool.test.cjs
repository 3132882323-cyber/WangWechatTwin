const test=require('node:test');const assert=require('node:assert/strict');
const {choose,validUrl}=require('./pool_core.js');
test('same contact continues same conversation',()=>{
 const a={slotId:0,tabId:11,key:'a',turns:2,used:1};
 const selected=choose([a],'a',10);assert.equal(selected.slot,a);assert.equal(selected.reset,false);assert.equal(selected.reused,true);
});
test('fourth contact resets oldest owned slot instead of creating fourth',()=>{
 const slots=[{key:'a',used:4},{key:'b',used:2},{key:'c',used:3}];
 const selected=choose(slots,'d',10);assert.equal(selected.slot,slots[1]);assert.equal(selected.reset,true);assert.equal(selected.reused,false);
});
test('same-contact long or failed conversation resets in place',()=>{
 for(const a of [{key:'a',turns:24},{key:'a',turns:1,failed:true}]){
  const r=choose([a],'a',10);assert.equal(r.slot,a);assert.equal(r.reset,true);assert.equal(r.reused,false);
 }
});
test('pool is bounded across many contacts',()=>{
 const slots=[];let tabs=0;
 for(let i=0;i<100;i++){
  const key='peer'+i%7;const r=choose(slots,key,i);let slot=r.slot;
  if(!slot){slot={};slots.push(slot);tabs++;}
  slot.key=key;slot.used=i;slot.turns=1;
  assert.ok(slots.length<=3);
 }
 assert.equal(tabs,3);
});
test('only temporary ChatGPT URLs qualify as owned pool tabs',()=>{
 assert.equal(validUrl('https://chatgpt.com/c/example?temporary-chat=true'),true);
 for(const url of ['https://chatgpt.com/c/example','https://chatgpt.com.evil.test/?temporary-chat=true','https://example.com/','chrome://extensions/'])assert.equal(validUrl(url),false);
});

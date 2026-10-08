// Shared pure allocation policy, also tested under Node. No conversation content.
(function(root){
 const choose=(slots,key,now,maxTurns=24)=>{
  const same=slots.find(s=>s.key===key);
  if(same)return {slot:same,reset:!!same.failed||same.turns>=maxTurns,reused:!same.failed&&same.turns<maxTurns};
  if(slots.length<3)return {slot:null,reset:true,reused:false};
  const oldest=[...slots].sort((a,b)=>a.used-b.used)[0];
  return {slot:oldest,reset:true,reused:false};
 };
 const validUrl=url=>typeof url==='string'&&url.startsWith('https://chatgpt.com/')&&new URL(url).searchParams.get('temporary-chat')==='true';
 const checkPool=slots=>{if(!Array.isArray(slots)||slots.length>3)throw Error('chatgpt_pool_invalid');return slots;};
 // Session storage is cleared on extension reload. LOCAL is authoritative,
 // including an intentionally empty pool; only migrate a legacy session once.
 const readPool=async(storage)=>{
  const local=await storage.local.get('bridgePool');
  if(local.bridgePool!==undefined)return checkPool(local.bridgePool);
  const session=await storage.session.get('bridgePool');
  if(session.bridgePool===undefined)return [];
  const slots=checkPool(session.bridgePool);await storage.local.set({bridgePool:slots});return slots;
 };
 const writePool=async(storage,slots)=>storage.local.set({bridgePool:checkPool(slots)});
 const api={choose,validUrl,readPool,writePool};root.WechatPool=api;
 if(typeof module!=='undefined')module.exports=api;
})(globalThis);

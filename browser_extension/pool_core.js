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
 const api={choose,validUrl};root.WechatPool=api;
 if(typeof module!=='undefined')module.exports=api;
})(globalThis);

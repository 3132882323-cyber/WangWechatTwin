"""Prepare optional offline media support without changing user configs or DBs."""
from pathlib import Path
import sys,argparse,json,hashlib,collections,struct,urllib.request,psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.config import load_config
from app.adapters.history_reader import HistoryReadOnlyAdapter


def prepare_keys(reader):
    import wcdb_readonly as tool
    roots={p for info in reader.files.values() for p in Path(info['source']).parents if p.name=='db_storage'}
    if len(roots)!=1:raise RuntimeError('Select one authenticated account first')
    db_dir=next(iter(roots));original=tool.collect_db_files;original_pids=tool._get_pids_windows
    current_user=psutil.Process().username().lower();saved={}
    def collect(directory):
        files,_=original(directory);chosen=[];salts={}
        for rel,path,size,salt,page in files:
            if not Path(path).name.startswith('media_'):continue
            chosen.append((rel,path,size,salt,page));salts.setdefault(salt,[]).append(rel)
        return chosen,salts
    def pids():
        result=[]
        for pid,size in original_pids():
            try:
                if psutil.Process(pid).username().lower()==current_user:result.append((pid,size))
            except psutil.Error:pass
        return result
    def save(files,salts,key_map,directory,filename):
        data={rel:{'enc_key':key_map[salt],'salt':salt,'source':path,'bytes':size} for rel,path,size,salt,_ in files if salt in key_map}
        Path(filename).write_text(json.dumps(data),encoding='utf-8');saved.update(data)
    tool.collect_db_files=collect;tool._get_pids_windows=pids;tool._save_results=save;tool._print=lambda *a,**kw:None
    tool._scan_memory_raw_key(str(db_dir),str(reader.root/'media_keys.json'))
    from Crypto.Cipher import AES
    account=db_dir.parent;votes=collections.Counter();samples=[]
    thumbnails=sorted((account/'msg/attach').rglob('*_t.dat'),key=lambda p:p.stat().st_mtime,reverse=True)[:32]
    for path in thumbnails:
        data=path.read_bytes()
        if data[:6]==b'\x07\x08V2\x08\x07' and len(data)>32 and data[-2]^255==data[-1]^217:
            votes[data[-2]^255]+=1;samples.append(data)
    found=None
    if len(samples)>=2 and votes:
        xor=votes.most_common(1)[0][0];suffix=account.name.rsplit('_',1)[-1]
        if len(suffix)==4:
            for high in range(1<<24):
                number=str((high<<8)|xor)
                if hashlib.md5(number.encode()).hexdigest()[:4]!=suffix:continue
                key=hashlib.md5((number+reader.self_username).encode()).hexdigest()[:16].encode()
                verified=True
                for data in samples[:2]:
                    length,_=struct.unpack('<II',data[6:14]);size=length+16-length%16
                    plain=AES.new(key,AES.MODE_ECB).decrypt(data[15:15+size]);padding=plain[-1]
                    if not plain.startswith((b'\xff\xd8\xff',b'\x89PNG',b'wxgf',b'GIF8')) or not 1<=padding<=16 or plain[-padding:]!=bytes([padding])*padding:
                        verified=False;break
                if verified:found=key;break
        if found:
            (reader.root/'image_keys.json').write_text(json.dumps({'aes_key_hex':found.hex(),'xor_key':xor,'verified_probes':2}))
    return {'authenticated_media_keys':len(saved),'image_key_verified':found is not None}


def prepare_model(config):
    destination=config.resolve(config.media.voice_model);destination.mkdir(parents=True,exist_ok=True)
    metadata=json.loads(urllib.request.urlopen('https://huggingface.co/api/models/Systran/faster-whisper-small?blobs=true',timeout=20).read())
    revision=metadata['sha'];manifest=next(x for x in metadata['siblings'] if x['rfilename']=='model.bin')
    path=destination/'model.bin';expected=manifest['lfs']['sha256']
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        temporary=destination/'model.bin.download';digest=hashlib.sha256();size=0
        url=f'https://huggingface.co/Systran/faster-whisper-small/resolve/{revision}/model.bin?download=true'
        with urllib.request.urlopen(url,timeout=30) as response,temporary.open('wb') as output:
            while block:=response.read(1024*1024):output.write(block);digest.update(block);size+=len(block)
        if size!=manifest['lfs']['size'] or digest.hexdigest()!=expected:raise RuntimeError('Official model checksum mismatch')
        temporary.replace(path)
    for name in ['config.json','tokenizer.json','vocabulary.txt']:
        url=f'https://huggingface.co/Systran/faster-whisper-small/resolve/{revision}/{name}'
        (destination/name).write_bytes(urllib.request.urlopen(url,timeout=20).read())
    from faster_whisper import WhisperModel
    WhisperModel(str(destination),device='cpu',compute_type='int8',cpu_threads=4,local_files_only=True)
    return {'offline_model_ready':True,'official_model_checksum_verified':True}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='config.http-api.yaml');parser.add_argument('--model-only',action='store_true')
    args=parser.parse_args();config=load_config(args.config)
    report=prepare_model(config)
    if not args.model_only:report.update(prepare_keys(HistoryReadOnlyAdapter(config)))
    print(json.dumps(report))

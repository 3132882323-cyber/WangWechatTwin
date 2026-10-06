# Adapted from ikevss/wechat-ai-memory, MIT. See licenses/WECHAT_AI_MEMORY_LICENSE.txt.
from pathlib import Path
import struct,subprocess,os,shutil,io,hashlib,json,re
from PIL import Image,ImageOps
V2_MAGIC=b"\x07\x08V2\x08\x07"
WXGF_MAGIC=b"wxgf"
def decrypt_v2_image(source: Path, aes_key: bytes, xor_key: int) -> bytes:
    from Crypto.Cipher import AES
    if len(aes_key) != 16:
        raise ValueError("The WeChat image AES key must be 16 bytes")
    data = source.read_bytes()
    if len(data) < 31 or data[:6] != V2_MAGIC:
        raise ValueError(f"Unsupported WeChat image format: {source.name}")
    aes_size, xor_size = struct.unpack("<II", data[6:14])
    # WeChat applies PKCS#7 to this segment, so an already aligned plaintext
    # still receives a complete 16-byte padding block.
    encrypted_size = aes_size + (16 - (aes_size % 16))
    aes_start = 15
    aes_end = aes_start + encrypted_size
    xor_start = len(data) - xor_size
    if aes_end > xor_start or xor_start < aes_start:
        raise ValueError(f"Invalid WeChat image layout: {source.name}")
    decrypted_padded = AES.new(aes_key, AES.MODE_ECB).decrypt(data[aes_start:aes_end])
    padding = decrypted_padded[-1] if decrypted_padded else 0
    if not 1 <= padding <= 16 or decrypted_padded[-padding:] != bytes([padding]) * padding:
        raise ValueError(f"Invalid WeChat image padding: {source.name}")
    decrypted_head = decrypted_padded[:-padding]
    if len(decrypted_head) != aes_size:
        raise ValueError(f"Invalid WeChat image AES length: {source.name}")
    middle = data[aes_end:xor_start]
    tail = bytes(byte ^ xor_key for byte in data[xor_start:])
    return decrypted_head + middle + tail


def image_extension(data: bytes) -> str | None:
    signatures = (
        (b"\xff\xd8\xff", ".jpg"),
        (b"\x89PNG\r\n\x1a\n", ".png"),
        (b"GIF87a", ".gif"),
        (b"GIF89a", ".gif"),
        (b"RIFF", ".webp"),
    )
    for signature, extension in signatures:
        if data.startswith(signature):
            return extension
    return None


def decode_wxgf_image(data: bytes, ffmpeg_path: str | Path | None = None) -> bytes:
    """Decode the largest HEVC image partition from WeChat's WXGF container."""
    if not data.startswith(WXGF_MAGIC):
        raise ValueError("Invalid WeChat WXGF image")
    partitions = _wxgf_partitions(data)
    if not partitions:
        raise ValueError("No HEVC image partition was found in the WeChat WXGF file")
    offset, size = max(partitions, key=lambda item: item[1])
    ffmpeg = str(ffmpeg_path) if ffmpeg_path else _find_ffmpeg()
    if not ffmpeg:
        raise ValueError("WXGF original images require the bundled FFmpeg decoder")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "hevc",
        "-i",
        "pipe:0",
        "-frames:v",
        "1",
        "-c:v",
        "png",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            command,
            input=data[offset : offset + size],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("Failed to start the WXGF image decoder") from exc
    if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Failed to decode the WeChat WXGF image: {detail or 'empty FFmpeg output'}")
    return result.stdout


def _wxgf_partitions(data: bytes) -> list[tuple[int, int]]:
    if len(data) < 15 or not data.startswith(WXGF_MAGIC):
        return []
    header_length = data[4]
    if header_length >= len(data):
        return []
    for marker in (b"\x00\x00\x00\x01", b"\x00\x00\x01"):
        partitions: list[tuple[int, int]] = []
        cursor = header_length
        while cursor < len(data):
            offset = data.find(marker, cursor)
            if offset < 0:
                break
            if offset >= 4:
                size = int.from_bytes(data[offset - 4 : offset], "big")
                if size > 0 and offset + size <= len(data):
                    partitions.append((offset, size))
                    cursor = offset + size
                    continue
            cursor = offset + 1
        if partitions:
            return partitions
    return []


def _find_ffmpeg() -> str | None:
    configured = os.environ.get("FFMPEG_PATH")
    if configured and Path(configured).is_file():
        return configured
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            return bundled
    except (ImportError, OSError, RuntimeError):
        pass
    return shutil.which("ffmpeg")


def image_refs(*values):
    found=[]
    for value in values:
        if isinstance(value,str):value=value.encode('utf-8')
        if not isinstance(value,bytes):continue
        for match in re.finditer(rb'(?i)(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])',value):
            ref=match[0].decode().lower()
            if ref not in found:found.append(ref)
    return found[:16]


def prepare_image(data,root):
    """Keep screenshot text legible with overlapping crops, rather than a tiny overview."""
    digest=hashlib.sha256(data).hexdigest();root=Path(root);root.mkdir(exist_ok=True)
    with Image.open(io.BytesIO(data)) as image:
        if image.width*image.height>4096*4096:raise ValueError('图片尺寸超出限制')
        image=ImageOps.exif_transpose(image).convert('RGBA')
        background=Image.new('RGBA',image.size,'white');background.alpha_composite(image);image=background.convert('RGB')
        width,height=image.size;partial=False
        if height>1800 and height/width>1.8:
            span=min(height,max(width,1000));starts=sorted({0,(height-span)//2,height-span})
            pictures=[image.crop((0,start,width,start+span)) for start in starts]
            partial=height>3*span
        elif width>1800 and width/height>1.8:
            span=min(width,max(height,1000));starts=sorted({0,(width-span)//2,width-span})
            pictures=[image.crop((start,0,start+span,height)) for start in starts];partial=width>3*span
        else:pictures=[image]
        paths=[]
        for index,picture in enumerate(pictures):
            picture.thumbnail((1600,1600));buffer=io.BytesIO();picture.save(buffer,format='PNG',optimize=True)
            if buffer.tell()>2*1024*1024:
                picture.thumbnail((1024,1024));buffer=io.BytesIO();picture.save(buffer,format='PNG',optimize=True)
            if buffer.tell()>2*1024*1024:raise ValueError('图片预览超出模型输入限制')
            target=root/(digest+f'-{index}.png')
            from app.stickers import _atomic
            _atomic(target,buffer.getvalue());paths.append(str(target))
        return paths,{'image_verified':True,'image_sha256':digest,'image_width':width,'image_height':height,
                      'image_partial':partial,'image_crop_count':len(paths)}


class LocalImageReader:
    def __init__(self,config,reader):
        self.config,self.reader=config,reader
        self.accounts={p.parent for info in reader.files.values() for p in Path(info['source']).parents if p.name=='db_storage'}
        self.root=reader.root/'image_assets';self.root.mkdir(exist_ok=True)
        self.keys=json.loads((reader.root/'image_keys.json').read_text()) if (reader.root/'image_keys.json').exists() else {}

    def resolve(self,message):
        refs=json.loads(message.raw_summary or '{}').get('image_refs',[])
        refs=[ref for ref in refs if isinstance(ref,str) and re.fullmatch('[0-9a-f]{32}',ref)]
        contact_hash=hashlib.md5(message.contact.encode()).hexdigest()
        candidates=[]
        for account in self.accounts:
            folder=account/'msg'/'attach'/contact_hash
            for ref in refs:
                for suffix in ['.dat','_h.dat','_t.dat']:
                    candidates.extend(folder.glob('*/Img/'+ref+suffix))
        candidates.sort(key=lambda p:('_t' in p.stem,'_h' in p.stem,-p.stat().st_mtime))
        for path in candidates:
            try:
                if path.stat().st_size>20*1024*1024:continue
                data=path.read_bytes()
                if data.startswith(V2_MAGIC):
                    data=decrypt_v2_image(path,bytes.fromhex(self.keys['aes_key_hex']),self.keys['xor_key'])
                elif not image_extension(data):
                    # Legacy single-byte XOR is verified by full Pillow decoding.
                    trial=[]
                    for signature in [b'\xff\xd8\xff',b'\x89PNG',b'GIF8']:
                        key=data[0]^signature[0]
                        if bytes(b^key for b in data[:len(signature)])==signature:trial.append(bytes(b^key for b in data))
                    if len(trial)!=1:continue
                    data=trial[0]
                if data.startswith(WXGF_MAGIC):data=decode_wxgf_image(data)
                paths,meta=prepare_image(data,self.root);meta['image_thumbnail_only']=path.stem.endswith('_t')
                return paths,meta
            except (OSError,ValueError,KeyError):continue
        return [],{'image_error':'local_asset_unavailable_or_unverified'}

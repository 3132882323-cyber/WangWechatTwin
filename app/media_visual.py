"""Decode/download visual assets off the normal text polling path."""
import json
import time
from pathlib import Path
from app.media_voice import OfflineVoiceProcessor
from app.media_images import LocalImageReader
from app.stickers import acquire


class VisualMediaProcessor(OfflineVoiceProcessor):
    kind='visual'
    enable_field='images_enabled'
    workers=2

    def __init__(self,config,reader):
        self.image_reader=LocalImageReader(config,reader)
        super().__init__(config,reader)

    def _accept(self,message):
        return message.message_type in {'image','sticker'} and self.config.media.images_enabled

    def _transcribe(self,message):
        origin=json.loads(message.raw_summary or '{}')
        if message.message_type=='image':
            deadline=time.monotonic()+10
            while True:
                paths,meta=self.image_reader.resolve(message)
                if paths or time.monotonic()>=deadline:break
                time.sleep(.4)
            origin.update(meta)
            content='对方发来图片，请只根据已附图片与当前聊天理解，不猜测缺失信息。' if paths else message.content
        else:
            source=origin.pop('sticker_source',None)
            if not source:raise ValueError('缺少已校验的表情元数据')
            accounts={p.parent for info in self.reader.files.values() for p in Path(info['source']).parents if p.name=='db_storage'}
            digest,paths,label=acquire(source,self.reader.root,accounts)
            origin.update(sticker_md5=digest,asset_verified=True)
            content='[表情包]'+('\n'+label if label else '')
        return message.model_copy(update={'content':content,'media_paths':paths,'raw_summary':json.dumps(origin)})

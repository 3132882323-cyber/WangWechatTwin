# 语音、图片与表情识别

## 当前实现

- 语音按当前联系人及服务器消息编号，唯一关联认证媒体数据库中的 `VoiceInfo`，提取 SILK。离线 Whisper small/int8 在 CPU 上转写，音频不发送到外部模型；回复草稿仍使用用户选择的模型连接。
- 转写、图片读取和表情下载使用独立后台队列。文字读取不等待音频解码或媒体下载；队列结果由正常回复管线处理。重启恢复最近十分钟内未处理任务，已有回复及过时内容仍受发送保护。
- 同一批语音与文字合并时保留转写不确定标记，原消息编号一并记为已处理，避免重复回复。
- 普通图片只从当前联系人对应的附件目录取图，支持 V2 AES/XOR、旧单字节 XOR 与 WXGF 转码。长截图采用重叠裁剪保留文字清晰度；超长图片无法覆盖全部内容时明确标记不完整。
- 表情保留经 MD5 校验的原图，校验缓存及预览，透明底合成白底，按播放时间抽取不同画面。限制文件大小、像素、动画帧数与总解码量。
- 网页连接在插入文字后等待编辑器状态同步，避免上传图片后只发出了附件，遗漏结构化回复提示。
- 普通图片回复先审核，识别不清楚的表情和含重要数字或模糊内容的语音也留审核。视觉描述不直接写入已确认的个人事实摘要；图片中的文字不能覆盖系统规则。

## 安装与准备

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-media.txt
.venv\Scripts\python.exe scripts\prepare_media.py --config config.http-api.yaml
```

准备脚本仅操作当前配置已连接的账号，原数据库与原 `keys.json` 不变。新增媒体密钥和图像参数只保存在私有运行目录，不打印其值。语音模型约 484 MB，下载后核对官方 SHA-256；以后转写使用本机文件。PyAV 被限制到已核验的兼容版本，避免其新版本移除接口参数导致转写失败。

准备完成后在已有配置追加，不覆盖原配置：

```yaml
media:
  voice_enabled: true
  images_enabled: true
  voice_model: .runtime/models/whisper-small-local
  max_voice_seconds: 120
  max_pending_voice: 20
```

WXGF 原图转码需要本机 FFmpeg；普通图片路径不使用微信界面点击。语音与视觉队列及缓存不随源码发布。

## 验证及限制

Windows 本机已验证一条真实本人语音与此前微信内置转写规范化后完全一致；真实加密截图已解码并以三段清晰图片生成网页草稿；真实表情也生成了结构化视觉草稿。草稿验收均使用明确的自检联系人，没有向真实联系人新增测试发送。

这些验收证明素材读取、识别与草稿显示，不代表已完成一条新的入站语音或图片自动发送闭环。默认中文转写；方言、噪音、人名和数字仍可能识别错误。模型给出的视觉置信度是自评，不能当作准确率；超长图片、未下载素材、无声或超过时长的语音转审核。

参考实现及许可：[wechat-ai-memory](https://github.com/ikevss/wechat-ai-memory)（MIT，保留许可证）、[wechatapi](https://github.com/jiatj/wechatapi)（Apache-2.0，参考图片参数派生）、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)（MIT，依赖方式使用）。

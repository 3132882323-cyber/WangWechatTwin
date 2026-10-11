# 豆包续聊参数修复：本机验收中

上游：[2951461586/Doubao2API](https://github.com/2951461586/Doubao2API)，固定源码 `cc14a647f9dd4b769eb78b822bcb18e0387b7b45`，Apache-2.0。本项目的隔离试验仍保留单账号、单并发、一次请求、回环认证、禁用桌面凭据导入及仅 dataURL 图片输入。

## 实际发现

原网关第一次图片请求可以生成严格结构化回复，但续轮即使传入旧 `conversation_id`，响应仍给了新编号，且没有记住本轮虚构标记。适配器报 `local_proxy_conversation_changed`，保留未知状态，没有清库或自动重发。

源码 `internal/doubao/client.go` 的 `BuildPayload` 已把旧 ID 放入 `client_meta.conversation_id`，但同时对每次请求设置 `option.need_create_conversation=true`。续轮仍要求创建新会话，与复用目的冲突。

## 最小候选改动

```diff
- "need_create_conversation": true,
+ "need_create_conversation": convID == "0",
```

该函数已将空 ID 归一为 `"0"`。因此首轮创建会话，提供旧 ID 的续轮不再请求重建。不要放宽适配器的会话归属检查。

已增加 Go 用例验证空 ID、显式 `0` 和旧 ID 三种情况，`go test ./...` 及新程序构建通过。新隔离程序 SHA256 为 `3BFF30CA16195656145AC40A6F6F6863D59432A42A14996F9E41DA9923E1E93C`。

随后已核对实际运行程序哈希，以虚构 A 图片 → B 文字 → A 回忆完成三次本机验收。三次均返回完整结构化草稿，耗时 12.250 / 16.156 / 8.344 秒；A 会话编号不变且续轮确实发送原 ID，B 独立。A 的回忆输入没有重复图中信息，仍正确识别蓝色方块、黄色圆形与 TEST42，未混入 B 的虚构暗号。旧未知状态未删除，也未重试。

当前 Windows 部署已启用 DeepSeek 和豆包两个本机 HTTP 通道，GPT 保留原网页。草稿验收没有发送微信；这不是新真实表情消息的微信发送证明，也不是连续 24 小时稳定证明。`last_section_id/index` 保持原值在本轮三次测试中未阻止续聊，长期多轮仍需观察。

隔离程序的状态路径保护要求 exe 位于 `trial/`，状态位于相邻 `state/`。最初把新构建产物放在 `runtime/bin/`，手工启动被路径保护拒绝；已把相同哈希的文件放回合法目录，未降低保护或更改登录数据。构建成功与正确安装目录必须分别核对。

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

**上述测试尚不证明上游续聊已修复**。仍须核对实际运行程序哈希，再以虚构 A 图片 → B 文字 → A 回忆验证 A 的会话编号不变、B 独立及图形颜色绑定；`last_section_id/index` 是否另需保存仍待实际验证。旧未知状态不删除。

当前部署先启用已通过会话隔离和复用验收的 DeepSeek 本机 HTTP 通道。豆包仍保持原网页，GPT 仍保持原网页。切换前后的模型生成、微信发送及 24 小时观察分别报告。

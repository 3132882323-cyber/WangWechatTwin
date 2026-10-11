# 本机 HTTP 模型通道

此功能保留 `hybrid_web` 的分工和发送审核，按通道显式替换 DeepSeek/豆包的网页生成步骤。GPT 仍使用原网页桥接，不读取已有 GPT Cookie，也不使用 Codex 兜底。默认不启用，升级不会自动改变旧配置。

```yaml
openai:
  provider: hybrid_web
  local_proxies:
    deepseek:
      enabled: true
      base_url: http://127.0.0.1:19655/v1
      api_key_file: /private/deepseek-proxy-key.txt
      session_db: .runtime/local_proxy/sessions.sqlite3
      timeout_seconds: 45
    doubao:
      enabled: true
      base_url: http://127.0.0.1:19656/v1
      api_key_file: /private/doubao-proxy-key.txt
      session_db: .runtime/local_proxy/sessions.sqlite3
      timeout_seconds: 60
```

只有上述两个固定回环地址被允许，不能用远程共享反代。网关 API key 与上游登录会话分离，配置只引用私有文件，不把 key 写入公开配置或日志。上游服务须自行保持登录有效；不可保证免重新登录。

## 接入验收

先备份原配置与改动源码，在单独配置中生成结构化草稿，核对 A/B/C、本机风险审核、联系人隔离和图片真实输入。六次隔离模型短测仅证明基础回复，**不证明这个结构化适配器已经通过生产验收**；见 [短测记录](WEB_PROXY_TRIAL.md)。

输入必须携带稳定 `conversation_key` 及 `incoming.external_id`。本机状态库按通道/联系人保存会话和消息请求；结果未知时不得向原会话再次提交，下一轮也保守进入审核，待人工核对。不得删除状态库来强行重试。已有微信发送 claim、本人已回复与最新入站校验仍由原流水线负责。

仅 TCP 连接失败、尚未开始提交 HTTP 请求时，记录 `not_submitted`，保留该消息的去重记录，但允许同一联系人后续的新消息继续。请求开始后的断开仍是未知结果；两种情况都不会重发旧消息或自动换模型。

图片只使用本项目已解码的 PNG 资产目录，最多三张，每张不超过 2 MiB、合计不超过 4 MiB，并在提交前检查整体请求大小；不让网关自行抓取远程图片网址。识别不清楚仍须审核。

`/health` 对本机反代显示 `transport=local_http_proxy` 与 `transport_reachable`。仅端口可连接时是 `reachable_unverified`、`inference_verified=false`；只有状态库记录最近 120 秒已校验完整结构化回复且端口可达时，才报告最近推理已验证。无近期请求时不能从端口猜测模型就绪，也不因此停止后台。所有启用通道必须共用同一个状态库，以保留跨通道的未知结果阻断。

## 运行边界

所有私聊及群聊 @ 范围沿用原配置；主动聊天、重要承诺及不确定媒体不改变审核规则。后台持续运行依赖本机开机、网络、微信登录和模型会话；启动策略和连续 24 小时的实测证据分别报告。

面向已有私聊与新好友自动发现的配置如下。新好友首条入站消息会刷新联系人与既有会话名单，再采用未配置联系人的默认模式，不要求手工维护发送白名单。已有联系人若有显式 `off`/`shadow` 配置仍应先核对，不因接入代理自动覆盖。

```yaml
mode: low_risk_auto
wechat:
  sender_all_existing_chats: true
  allow_unknown_contacts: true
  unknown_contact_mode: low_risk_auto
  allow_groups: true
  group_only_mentions: true
```

群消息优先按协议中的精确 recipient ID 判断；不能把另一个 ID 或昵称的前缀当成本人，明确的协议名单也不能被正文昵称覆盖。单独 `@所有人` 不作为 `@本人` 自动回复。

Windows supervisor 可看护本项目已有的私有 `deepseek_watchdog.py`：启动前及等待超时后核验命令行与进程创建时间，确认缺失才补拉；检查异常只记固定码，不重启微信后台。当前本机已实测单独终止看护进程后，由 supervisor 恢复看护，模型 Node 与后台未被停止。这不等同于整机重启或 24 小时运行验收。该机制不启动豆包。

本机豆包试验可执行文件启动曾被自动审批返回 `blocked by policy`；用户手动启动成功。不得通过其他启动器、计划任务或后台启动包装绕过这次拒绝；需由用户再次启动该服务后再完成接入。

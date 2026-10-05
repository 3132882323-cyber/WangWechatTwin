# WangWechatTwin

Windows 本机微信回复助手：读取本人账号的新消息，参考本人写作习惯生成回复，按风险策略自动发送或留在本地审核台。

项目源码使用 **Apache-2.0** 开源。包括消息读取、数据库解密、界面兼容、模型调用、会话隔离、风格检索、发送验证和测试源码。第三方代码保留原许可证，见 [NOTICE](NOTICE) 和 [第三方说明](THIRD_PARTY_NOTICES.md)。微信客户端、托管模型及可选外部 GUI SDK 不属于本项目源码。

## 当前能力与边界

- 本人已登录账号的本地数据库读取；每页认证、WAL 提交帧校验、副本完整性检查。
- 启动时建立历史基线，不向旧消息批量补发回复；过滤本人消息，检测手动答复和已保存的人工草稿。
- 每联系人独立上下文和风格样例；不把甲联系人的历史提供给乙联系人。
- 支持 OpenAI Responses API 和已登录的 Codex CLI。后者使用账号计划额度，**不是无限或免费额度**。
- 默认 `shadow`：只生成草稿。日常自动发送需要显式选择 `low_risk_auto` 和发送范围。
- 报价、工期、付款、合同、凭证、不确定承诺及未解析的语音图片转审核。
- 发送前核验本人账号、唯一联系人和当前会话；发送后从本机数据库读回新的本人消息及服务器编号。状态不确定不自动重试。
- 群聊默认关闭；开启时默认只处理 @ 本人的消息。系统通知和公众号不自动回复。
- 本机审核台提供暂停、改稿和确认发送；审核 POST 使用本机页面令牌。

不能保证回复与本人逐句完全一致，也不保证所有微信小版本、缩放、窗口状态或消息类型都可用。未知版本或不明确的收件人应停止发送。

## 环境

Windows 10/11，Python 3.10–3.12（本机验证使用 3.12）。微信界面兼容路径目前严格校验 **4.1.15.13**。纯离线/mock 测试不要求登录微信；带原生 GUI 的测试要求 Windows 依赖。

## 安装与只生成草稿

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install wxauto4==41.1.7
Copy-Item config.example.yaml config.yaml
Copy-Item .env.example .env
```

先检查已有文件，**不要覆盖现有 `.env`、`config.yaml` 或 `.runtime`**。也可双击 `INSTALL_AND_CONFIGURE.bat`，安装入口会保留现有配置。

模型连接任选一种：

1. 在本机 `.env` 填写自己的 `OPENAI_API_KEY`；不要提交它。
2. 安装官方 Codex CLI 并由本人完成 `codex login`，设置 `openai.provider: account` 或使用默认 `auto`。CLI 使用已保存登录，不读取或复制认证文件。

仅选择**本人有权访问的已登录账号**的数据目录，例如其 `db_storage` 文件夹：

```powershell
.venv\Scripts\python.exe scripts\connect_local_history.py --db-dir "<本人账号的db_storage绝对路径>" --i-own-this-account
.venv\Scripts\python.exe -m app --config config.history.yaml run --mode shadow
```

连接脚本只读同一 Windows 用户的 Weixin 进程、校验选定数据库的密钥，不写入原始聊天文件，不附加调试器、不捕获登录、不注入代码。生成的密钥只存放在受限的 `.runtime/history_reader`，连接后仍是只生成草稿。

本机审核台默认为 [http://127.0.0.1:8765/](http://127.0.0.1:8765/)。端口被占用时修改本地 `web.port`。网页只监听本机，不应直接公开到互联网。

## 开启自动回复

先在自己的测试联系人上验证：新消息读取 → 模型草稿 → 本人确认的测试发送 → 数据库读回。再从 `config.history.yaml` 创建本地 `config.takeover.yaml`。

```yaml
adapter: history_verified_sender
mode: low_risk_auto
wechat:
  sender_all_existing_chats: false
  sender_allowed_contacts: ["<已授权联系人的内部username>"]
  allow_unknown_contacts: false
  send_holding_on_review: false
  max_auto_sends_per_hour: 30
```

该片段须与完整配置合并。联系人内部标识与显示名不同，不能凭昵称猜测标识。

只有本人明确授权全部已有聊天后，才设置 `sender_all_existing_chats: true`、`allow_unknown_contacts: true`、`unknown_contact_mode: low_risk_auto`。群聊单独设置 `allow_groups`，保留 `group_only_mentions: true`。

```powershell
.venv\Scripts\python.exe -m app --config config.takeover.yaml run --mode low_risk_auto
```

界面兼容辅助代码会校验当前 DLL 版本与静态模式，再临时更新运行中客户端的既有 Qt accessibility 布尔状态；识别失败恢复原值。它不修改微信文件、不启动讲述人，也不是付费 SDK 的激活绕过。相关源代码和许可证均包含在仓库中。

## 风格数据

`data/persona.md`、`data/business_rules.md`、`data/reply_samples.csv` 都是通用模板。用户自己的语气概要放在本机 `data/learned_style_summary.json`；按联系人隔离的问答样例放在 `.runtime/history_reader/style_samples.sqlite3`。

样例数据库格式为 `samples(contact TEXT, incoming TEXT, reply TEXT, created_at INTEGER)`。应从本人拥有的记录中本地生成，过滤凭证和敏感标识，仅用于表达风格；历史价格、日期、进度不等于当前事实。任何实际样例、联系人、数据库或日志都不要提交到公共仓库。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
```

本轮 Windows 源码测试通过 32 项。另有一次真实历史读取到草稿、一次明确授权的测试发送，以及一次从真实待回复消息恢复处理并发送的闭环验证。**这不代表每位用户、所有微信版本或全部场景均已通过**。公开仓库不包含这些私聊截图、内容或发送对象。

## 项目结构

`app/` 运行代码，`scripts/` 本地配置与连接工具，`tests/` 回归测试，`data/` 通用模板，`.runtime/` 用户本机运行数据（不发布）。

报告问题前阅读 [隐私说明](PRIVACY.md)，请使用合成或脱敏样例。欢迎改进版本兼容、媒体解析、风格评估和可复现测试；不要提交用户真实聊天或密钥。

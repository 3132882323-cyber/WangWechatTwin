# 本机 HTTP 微信连接

`history_http_sender` 在项目中提供另一种收发连接：使用已认证的本机微信数据库读取新消息，通过本机 HTTP 端口提交回复，并再次读取微信数据库核对新的服务器确认记录。它不打开微信窗口，不搜索会话，不使用键盘、鼠标或 GUI SDK 发送，也不会回退到 GUI 连接。

这不是完整的微信 Hook 实现。**本仓库目前不提供已经适配微信 4.1.15.13 的 Hook DLL，尚未通过该版本真实 HTTP 收发验收。** 仅有本地 HTTP 测试通过，不能视为微信发送成功。

## 外部服务协议

服务器必须监听 `127.0.0.1`，验证请求头 `Authorization: Bearer <token>`，并提供以下接口：

- `POST /GetSelfProfile`：返回当前登录账号的 `wxid`。必须与本机已认证数据库的本人账号完全相同。
- `POST /SendTextMsg`：接收 `wxidorgid`（目标微信ID）、`msg`（回复文字）、`expected_wxid`（已经核验的本人账号）和 `request_id`（64 字符十六进制发送尝试编号）。服务端应在同一发送锁内再次核对账号并拒绝重复编号。返回整数 `ret: 0` 仅表示请求接受，不能代替送达核验。

此接口形式参考 https://github.com/aixed/WeChat-Hook 。上游当前公开源码面向微信 4.1.10.27；不能将旧偏移直接用于新版本。

## 原生只读账号探针

`app/native/account_probe.cpp` 与 `account_snapshot.h` 是本项目独立实现的 Windows 文件句柄探针，使用微软公开的进程快照和文件路径查询 API。它将微信进程实际打开的联系人数据库所属账号与本机已认证数据库的本人 ID 比较，不调用微信 GUI，不发送消息，不写入微信进程内存。输出只有匹配状态和统计数字，不打印账号 ID 或聊天正文。

这不是官方微信登录状态接口；当前未找到唯一账号时拒绝绑定。基于 `xwechat_files/<账号>_<四位十六进制后缀>/db_storage/contact/contact.db` 的路径结构，目前仅有 Windows 微信 4.1.15.13 的本机核验。账号目录匹配必须与已有数据库认证一起使用，不能单独作为登录成功证明。

在已配置 MSVC 和 Windows SDK 的开发终端可编译：

```powershell
cl /nologo /EHsc /std:c++17 /utf-8 /D_WIN32_WINNT=0x0A00 /DWINVER=0x0A00 app/native/account_probe.cpp /Fe:account_probe.exe
```

运行参数为微信进程 PID 和仅保存本人 ID 的本机文本文件路径。此文件及探针输出中的实际运行日志均不应提交到 Git。

参考：[PssCaptureSnapshot](https://learn.microsoft.com/en-us/windows/win32/api/processsnapshot/nf-processsnapshot-psscapturesnapshot)、[PSS_HANDLE_ENTRY](https://learn.microsoft.com/en-us/windows/win32/api/processsnapshot/ns-processsnapshot-pss_handle_entry)。原生只读探针通过不代表 Hook 发送 ABI 已经适配。

## 配置

先保留既有配置，另建本机配置。示例：

```yaml
adapter: history_http_sender
mode: shadow
local_api:
  port: 30001
  token_file: .runtime/local_api/token.txt
  timeout_seconds: 8
  receipt_timeout_seconds: 12
```

令牌仅保存在本机，32–511 字符 ASCII，无空白；同一个令牌应由外部服务验证。令牌、微信数据库、联系人配置、聊天记录和实际风格档案不能提交到 Git。首次排错保持 `shadow`。

外部服务版本兼容与账号验证通过后，真实发送仍应先使用授权测试联系人和明确内容验收。

## 发送保护

发送继承暂停、联系人范围、内容审核和人工草稿保护，使用原始微信 ID 路由，避免同名联系人误投。每次发送前核对接口账号；当前上下文缺失或本人已经回复时取消自动发送。

发送前独占创建尝试记录。接口接受后，必须出现此前水位之后、本人发送、文本匹配且具有服务器 ID 的新记录，才标记 `verified_sent`。超时、网络失败和结果不明记录为 `unknown_do_not_retry`，禁止自动再次提交。服务器确认不是联系人已读证明。

## 已执行的验证

`tests/test_http_sender.py` 包括真实本机回环 HTTP 服务的协议测试；送达记录为受控测试数据。测试涵盖账号不匹配、认证失败、接口拒绝、人工草稿、暂停、本人后续回复、上下文缺失、重要承诺、未知结果不重试，以及旧记录不作为新发送证据。

这组测试没有向真实微信联系人发送消息。

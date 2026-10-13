# Apple 芯片 Mac 草稿版

Mac 版可运行本地审核台、导入消息 JSON，通过模型通道生成草稿，再保存你的修改。默认 `adapter: mock`、`mode: shadow`，没有接入本机微信，不读取微信数据库，也不向微信发送消息。`mock` 是消息适配器名称，不是离线模型；实际起草仍需要本机模型网关及 GPT 网页桥接。没有连接模型时会留下明确的失败或审核占位，不能把它当作生成成功。

本版保留 WorkBuddy/Codex 的队列维护、故障恢复、联系人隔离与未知结果不盲重试规则。开发与本机回归在 Windows 完成；[此前的 Apple 芯片 Mac CI](https://github.com/3132882323-cyber/WangWechatTwin/actions/runs/38143161160) 在提交 `30b5117` 的 arm64、Python 3.10 和 3.12 环境各通过 632 项测试。这是该提交的核心回归证据，不能替代后续版本的 CI。当前工作流要求原生 arm64 Python，并检查实际后台启动与停止及启动脚本语法。**尚未在你的 Mac 上验收模型生成、登录自启或微信实际收发**。CI 没有登录微信或模型账号，Windows 微信的成功记录不适用于 Mac。

## 首次启动

需要原生 **arm64 Python 3.10–3.12**，推荐 [Python 3.12 的 macOS universal2 安装包](https://www.python.org/downloads/macos/)。启动器会检查 Python 自身的运行架构，查找 PATH、Apple 芯片 Homebrew 和 Python.org 的常见安装位置。Intel/Rosetta 环境、系统 Python 3.9、Python 3.13 及以上均会被拒绝，已有虚拟环境保留。网页桥接需要 Chrome 116 及以上和模型自己的登录账号。

将仓库保存在会长期保留、可由当前用户访问的目录，例如 `~/Applications/WangWechatTwin`。打开终端，进入该目录，运行：

```bash
bash START_HERE.command
```

启动器创建 `.venv`，按 `pyproject.toml` 安装核心依赖，并且只在不存在时将样例复制成 `config.macos.local.yaml`。没有安装 `wxauto4`、`pywin32` 或 Windows DLL，也不改写 `.env`、已有配置和数据库。首次安装依赖需要网络。

审核台地址为 [http://127.0.0.1:18769/](http://127.0.0.1:18769/)，就绪后会打开浏览器。保留终端窗口，按 `Ctrl+C` 停止。如果文件可执行，也可双击 `START_HERE.command`；ZIP 下载没有保留可执行标记时，使用上面的 `bash` 命令即可。

默认数据在 `.runtime/macos/`；这与 Windows 示例数据路径分开。`config.macos.local.yaml`、`.env`、`.runtime/` 和 `.venv/` 已在忽略规则中。不要将配对码、私聊导出或个人配置发布到仓库。

若提示已有 `.venv` 是 Intel/Rosetta 环境，先停止该目录的后台；已安装自启时先用原环境卸载。保留原 `.venv` 的备份，将其移到另一个尚不存在的本地目录，再在未启用 Rosetta 的终端中运行 `bash START_HERE.command setup` 创建原生环境。不要删除 `.runtime`、个人配置或网关状态库。启动器不自动执行环境替换。

## 连接模型通道

新建的 Mac 配置使用 GLM 日常文字、豆包媒体、GPT 专业与明显激动情绪；DeepSeek 已停用。现有 `config.macos.local.yaml` 不会被升级覆盖，需要按新样例核对分工。Mac 消息导入目前仅支持文字，语音转写和图片导入仍需后续适配。

GLM 和豆包连接的是**同一台 Mac** 上已独立部署的本机 HTTP 网关。公开包提供客户端配置和草稿处理，不包含这两个服务、账号登录凭据或启动脚本，也不把 Windows 上的进程当作 Mac 服务。

| 通道 | 固定地址 | 私有网关 key 文件 |
| --- | --- | --- |
| GLM | `http://127.0.0.1:19659/v1` | `.runtime/macos/local_proxy/glm-api-key.txt` |
| 豆包 | `http://127.0.0.1:19656/v1` | `.runtime/macos/local_proxy/doubao-api-key.txt` |

key 文件只保存本机网关的 API key，上游登录信息留在网关自己的私有目录。两路共用 `.runtime/macos/local_proxy/sessions.sqlite3`，保留联系人会话归属、旧请求和未知结果记录；不要删库重试。未提供网关或 key 时，审核台仍可启动，但对应草稿不能完成生成。启动器不会自动拉起网关或尝试恢复 DeepSeek。

GPT 网页桥接的配对步骤：

1. 在 Chrome 扩展管理页打开开发者模式，选择“加载已解压的扩展程序”，选择本仓库的 `browser_extension/` 目录。
2. 在扩展设置页填入本机 `.runtime/macos/browser_bridge/pairing_token.txt` 中的配对码。首次启动审核台后才会生成此文件；不要发送或上传配对码。
3. 确认 ChatGPT 网页已登录，再从审核台执行模型网页恢复。GPT 的模型与思考档位仍需在网页核对；配置名称不证明网页实际采用了指定档位。各模型自己的权限和额度仍然适用。

Mac 的网页桥接使用扩展创建的独立最小化窗口；这不等同于 Windows 的 Win32 隐藏窗口。只管理已登记的专用窗口，不隐藏其他窗口。专用窗口混入个人标签时会停住保护，需要先把个人标签移出去，再从审核台恢复。

源代码更新后，在 `chrome://extensions` 重新加载本扩展，保留原配对码。只有后台和扩展心跳就绪并不能证明本轮模型已生成，仍需检查草稿正文及任务结果。更多说明见 [网页扩展说明](../browser_extension/README.md)。

审核台只监听 `127.0.0.1`。默认端口 `18769` 与扩展桥接地址一致；不要只修改后台端口而让扩展仍连接旧地址。Mac 启动器会拒绝公开监听地址、真实微信适配器或自动发送模式。

## 导入消息并生成草稿

后台保持运行时，在另一个终端进入仓库目录，导入仓库里的**虚构示例**：

```bash
bash START_HERE.command draft --input examples/macos_messages.json
```

等价的应用命令为：

```bash
.venv/bin/python -m app --config config.macos.local.yaml draft --input examples/macos_messages.json
```

输入可以是一个消息对象或对象数组，每条包含 `contact`、`content`，可选 `sender`、`chat_type`。文件最多 1 MiB、100 条。将自己有权使用的文字消息放在本地 JSON 文件中，以同样命令导入。没有模拟真实入站或发送回执；CLI 会通过草稿处理流程把结果保存到审核台。当前示例不演示语音、图片或表情识别。

审核台中的草稿可以修改、保存或取消；Mac 草稿版不发送微信。确认内容后，可自行复制到微信。网页执行结果未知时先核对任务和专用页，避免把重复导入当成安全重试。

诊断命令：

```bash
bash START_HERE.command doctor
```

诊断会检查配置、适配器和模型通道状态，不主动生成回复。`/health` 将本机端口可达与近期完整回复分别显示；`reachable_unverified` 表示端口可达、近期推理未验证。没有网关、未配对或未登录时，相应检查失败是预期行为，不能据此声称 Mac 微信已断线。

## 显式启用登录自启

默认启动不会安装自启。确认前台审核台可用后，先在其终端按 `Ctrl+C` 停止，再运行：

```bash
bash START_HERE.command install
```

这一命令创建并加载当前用户的 `~/Library/LaunchAgents/io.github.wangwechattwin.drafts.plist`，立即运行草稿后台，并在用户下次登录时启动。无需 `sudo`；登录自启不是系统启动前的服务，不替你登录模型、微信或 VPN。配置在每次启动时重新检查，仍须 `mock + shadow`。异常退出由 launchd 延迟重启，正常退出保持停止。

```bash
bash START_HERE.command status
bash START_HERE.command dashboard
bash START_HERE.command stop
bash START_HERE.command uninstall
```

`status` 只报告 LaunchAgent 是否加载；完整运行状态需看审核台 `/health`，生成成功需看实际草稿。`stop` 停止本次后台，但保留下次登录自启；`uninstall` 停止并移除自启文件。二者都保留配置、配对码、密钥、草稿、数据库和人工暂停状态。已安装的自启来自另一仓库目录、另一配置或符号链接时，工具会停止并保留它，不覆盖现有设置。

日志位于 `.runtime/macos/launchd.stdout.log` 和 `.runtime/macos/launchd.stderr.log`。自启使用绝对路径；移动仓库前先从原目录卸载，移到新目录后重新安装。若安装失败，工具保留可检查的 plist 并报告失败，不把保存文件当成运行成功。

只安装依赖和配置、暂不运行后台时可使用：

```bash
bash START_HERE.command setup
```

指定已有配置时，将参数放在子命令之前，例如 `bash START_HERE.command --config config.macos.local.yaml start`。配置始终由用户保有；不覆盖、不迁移 Windows 的个人文件。

## 现成方案检索与边界

登录自启参考了 [watchnet 的 LaunchAgent 实现](https://github.com/zachsnow/watchnet/blob/bc84bd0491403665aa18edd7b5cf85cfa471639c/watchnet)，其 [许可证为 MIT](https://github.com/zachsnow/watchnet/blob/bc84bd0491403665aa18edd7b5cf85cfa471639c/LICENSE)。这里只采用 `plistlib`、`launchctl bootstrap/bootout` 和用户级 LaunchAgents 的标准做法，没有复制源码或增加上游依赖；项目依赖仍按 Python 3.10–3.12 验证。

架构检查参考 [actions/setup-python 的架构讨论](https://github.com/actions/setup-python/issues/971) 及其 [v6.1.0 文档](https://github.com/actions/setup-python/blob/v6.1.0/docs/advanced-usage.md)，[许可证为 MIT](https://github.com/actions/setup-python/blob/v6.1.0/LICENSE)。本项目用 Python 标准库核对实际解释器，不复制上游实现或新增依赖；CI 显式请求 arm64，避免只看 runner 名称就误判架构。

已检索 [wechat-use](https://github.com/leeguooooo/wechat-use)，其说明面向 Apple Silicon、微信 4.1.9，并使用非商业研究类许可，因此没有并入本 Apache-2.0 仓库。[Cybing521/wechat-mcp](https://github.com/Cybing521/wechat-mcp) 与 MIT 的 [WeChat-MCP](https://github.com/BiboyQG/WeChat-MCP) 使用 Accessibility 或截图等方式，与本项目独立本机端口收发目标不同。

没有找到并完成验收的 Mac 原生微信端口方案。本版不分发注入模块，不调用 Windows 接口，也不宣称自动接管 Mac 微信。未来接入真实客户端，须另行完成本机版本兼容、许可、账号与联系人核对及发送回执验证。

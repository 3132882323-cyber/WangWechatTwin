# 智谱网页本机网关

此网关默认保持隔离草稿模式；本人授权后可作为正式日常文字通道启用。正式模式把当前联系人消息和必要的本机上下文交给 GLM，由主程序继续执行风险、审核、入站去重和微信发送检查。这里说明的是实现和部署方法，不代表已验收真实微信送达或 24 小时运行。

日常文字及本机语音转写可选 GLM；图片和表情包保留豆包，专业问题与明显激动情绪保留 GPT。主动消息、重要承诺及争执升级等审核规则不变。

## 请求与会话边界

固定监听 `127.0.0.1:19659`，所有接口要求本机 Bearer key。模型别名为 `chatglm-web`，实际网页模型版本尚未核实，响应标为 `web_assistant_unverified`；不能凭别名声称运行某个 GLM 版本。

主程序发送 `user=SHA256(持久化 session_key)` 和 `request_id=SHA256(外部消息编号)`。正常轮次沿用同一 session_key 和远端会话。只有主程序确认是新入站、没有处理中请求、且恢复所需本机文字/媒体/转写上下文完整时，才可另开隔离的恢复代；旧消息编号不会因此改变或补发，旧远端会话仍保留归属和未知结果。网关本身不会旋转 user 或自动新建替代会话。

请求、远端归属及 `pending/done/unknown` 持久化到 SQLite。同一编号不重复生成，未知联系人代的新编号也会被拒绝。单并发，忙时领取前返回 429；每个已领取请求至多一次正常刷新和一次生成，没有账号池、请求重试、代理轮换或验证码绕过。

成功必须有真实上游 SSE `status=finish`、一致的远端会话编号、完整文字及严格 ReplyDecision/A/B/C。字面 `tool_calls` 字典仅被动识别：不执行、转发或保存工具参数；出现它的本轮决策强制 `review`、置信度 0、无 holding reply，不能自动发送。只有工具信息、截断、无效 JSON 或其他未支持格式保留未知。网关不接收上传、不操作网页工具、不发送微信。

## 私有运行目录

正式模式只接受项目下的 `.runtime/glm_proxy`，与旧隔离试验的文件和账本分开：

```text
.runtime/glm_proxy/
  auth/refresh.json        # 本人已授权当前智谱页的必要刷新凭据
  local-key.txt            # 仅本机调用使用；不存在时才在启动时生成
  state/gateway.sqlite3    # 网关请求与远端归属账本
  gateway.lock            # OS 单实例锁
  gateway-process.json    # PID、创建时间及固定启动信息，无凭据
  watchdog.lock
  watchdog-process.json   # 看护身份和心跳，无凭据
  watchdog-status.json    # 固定状态码，无请求内容
```

`auth/refresh.json` 字段为 `refresh_token` 和 `source=authorized_single_glm_tab`。来源字段不能替代用户授权。部署时可以复制已有明确授权的试验刷新文件和本机 key，保留原文件；不合并或清空旧试验账本。源码不采集浏览器资料，不附带真实账号、聊天记录或本机 key。上述私有目录不应提交 Git。

使用项目现有 Python 环境，正式单服务命令为：

```text
python experiments/glm_web_trial/gateway.py --runtime-dir <项目绝对路径>/.runtime/glm_proxy --mode production
```

正式模式直接使用传入的当前联系人上下文，不再强加“虚构联系人”的试验提示。无参数启动仍是旧试验模式，文件写在 `experiments/glm_web_trial` 下；两种模式占用同一端口，应由部署者确认旧试验进程归属和无处理中请求后再切换。

## 主程序配置与看护

把以下内容合并到现有配置，保留豆包、GPT 和其他本机设置：

```yaml
openai:
  provider: hybrid_web
  everyday_lane: glm
  disabled_lanes: [deepseek]
  local_proxies:
    glm:
      enabled: true
      base_url: http://127.0.0.1:19659/v1
      api_key_file: .runtime/glm_proxy/local-key.txt
      session_db: .runtime/local_proxy/sessions.sqlite3
      timeout_seconds: 100
```

`session_db` 必须与现有启用的本机豆包等通道使用同一个主程序会话库；若现有值不同，沿用现有值。它与网关的 `state/gateway.sqlite3` 用途不同：前者维护主程序上下文和跨通道外部消息去重，后者维护网页请求及远端归属。客户端的 100 秒等待覆盖网关 90 秒总时限。

Windows 原有启动入口继续运行 `app.supervisor`。它仅在 `hybrid_web`、GLM 已启用且未禁用时启动 `scripts/glm_watchdog.py`，看护再启动正式网关。全部使用已有 Python 和隐藏窗口，不新增计划任务或外部服务程序，不恢复已停用的 DeepSeek，也不接管豆包。

看护用 PID、精确创建时间、Python 可执行文件、脚本、完整参数和本机监听地址共同确认归属。身份不明、权限不足或端口被其他进程占用时不终止对方。看护只读本机健康接口，不做模型探针；没有近期推理、认证失效或上游报错本身不会触发重启。

连续三次进程健康异常或已领取请求超过时限后，看护只终止已核实归属的网关，再按退避间隔恢复。新网关持有 OS 锁后把崩溃遗留的 pending 标记为 unknown，保留请求编号、远端归属及诊断，不补发。健康接口分别报告运行、授权、近期严格推理结果，以及遗留未知计数和 pending 年龄。看护自身心跳停滞也由 supervisor 按相同身份校验恢复。

`.runtime/startup/STOP_GLM_PROXY` 是显式停止标记：存在时阻止拉起，已运行看护会退出并仅停止自己确认归属的网关。不要顺带清除已有暂停标记、旧未知记录或其他通道的停止标记。

## 部署与回退

1. 备份配置、将覆盖的源码和启动入口，并保存一致的 SQLite 快照。
2. 部署网关、看护、supervisor 改动，以及同版本的主程序 GLM 路由和去重适配；只复制源码，私有运行文件独立保留。
3. 建立 `.runtime/glm_proxy/auth`，复制已获授权的必要刷新文件和本机 key；保持旧试验文件与账本原样。
4. 确认 19659 的旧试验进程身份以及 pending 为 0 后，只停止该进程。合并配置并通过原启动入口恢复主程序，看护自动拉起正式网关。
5. 核对健康接口的 `mode=production`、`runtime_ok=true` 及进程身份。只有实际严格完成的响应才使近期推理健康变为已验证；端口监听不能证明可用推理。
6. 按本次已授权范围验收草稿与自然入站；生成完成、审核/发送决策、发送领取及微信原生回执分别核实，不重放旧消息验收。

回退时恢复原 `everyday_lane` 和路由配置、设置 GLM 停止标记并只停止已确认归属的 GLM 进程。保留全部数据库和未知请求，不以清库、改旧消息编号或重置联系人方式重试。

## 验证与来源

离线测试使用 MockTransport、假进程和假健康响应，不访问真实账号或发送微信：

```text
python -m pytest tests/test_glm_web_trial.py tests/test_glm_watchdog.py tests/test_supervisor.py -q
```

2026-10-12 的隔离网页短测中，两个虚构联系人各两轮全部返回严格三候选，约 7.8/7.2/6.0/6.0 秒；各自远端编号保持一致、联系人间不同，两轮带工具元数据并强制审核。回忆核对依据是本机注入的虚构记录，不能声称网页纯历史记忆已独立验证，没有发送微信。之前的格式失败保留在私有账本，没有重提。后续速度试验 GLM 完成三轮，千问在网页验证页停止，不能据此宣称 GLM 比千问更快。这些结果不证明长期稳定、生产送达或无限免费使用。

网页协议参考：[glm2api 固定源码](https://github.com/forsakenkraken/glm2api/tree/4dd06ee61b107d6990ae60e41d445f84afcde6c6)。该上游为 GPL-3.0；本项目只以普通网页协议为参考，独立实现鉴权、账本、严格解析和看护，没有执行或复制上游账号池、重试及调试日志实现。网页接口和默认模型映射可能变化。

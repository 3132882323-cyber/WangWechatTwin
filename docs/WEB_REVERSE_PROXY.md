# DeepSeek 与豆包网页反代候选（2026-10-11）

最初研究阶段只核对公开仓库、源码、许可证和 Issues，没有安装或读取凭据。随后经用户明确授权完成独立新登录和6次接口短测，结果见 [真实短测记录](WEB_PROXY_TRIAL.md)。网页转 API 不等于官方 API，也不能据代码存在证明本机可用或长期稳定。

## DeepSeek

[ForgetMeAI/FreeDeepseekAPI](https://github.com/ForgetMeAI/FreeDeepseekAPI) 为 MIT、本机 Node 服务；运行推理使用 HTTP fetch，独立 Chrome 登录后保存本机会话，可减少持续依赖 DOM 的环节。最新代码提交为 2026-10-07 的 648e708；deepseek-chat 默认关闭思考与搜索。

[Issue 36](https://github.com/ForgetMeAI/FreeDeepseekAPI/issues/36) 在 10-10 报告会话混入且仍未关闭。源码未提供 user/x-agent-session 时使用共用 dev-agent，因此接入前必须按联系人提供稳定唯一键，并用两个虚构联系人做并发与重试隔离验证，不能直接替换生产通道。

[NIyueeE/ds-free-api](https://github.com/NIyueeE/ds-free-api) 虽近期维护，但 GPL-3.0 且有明确账号处罚反馈。[Issue 112](https://github.com/NIyueeE/ds-free-api/issues/112#issuecomment-6030358910) 的维护者对照尚未证明新修补解决问题。不直接复制其源码进本仓库，也不建议用当前生产账号验证。

## 豆包

[2951461586/Doubao2API](https://github.com/2951461586/Doubao2API) 为 Apache-2.0、Go 1.24、本机独立服务；源码确有 image_url 上传与附件块构造。仓库 10-06 创建、10-07 最后提交 cc14a64，未见仓内测试或 CI，图片上游真实接受及长期运行未验收。

默认启动会导入 DoubaoWork 配置中的凭据。实验应明确禁用自动导入（-no-import）、使用独立数据文件与用户自己的扫码登录，并核对实际监听地址仅回环；不能默认运行后读取其他已有登录态。

[wangchuxiaoji-oss/doubao2api](https://github.com/wangchuxiaoji-oss/doubao2api) 为 Apache-2.0，但仍使用 Playwright/Chromium 页面 fetch，源码标准 image_url 未接通，与 README 部分示例不一致；[Issue 3](https://github.com/wangchuxiaoji-oss/doubao2api/issues/3) 仍有验证码反馈，故不优先替换现有视觉通道。

## 实验边界

优先把 MIT DeepSeek 与新 Go 豆包作为隔离候选：回环监听、单自有账号、单并发、虚构文字和非敏感静态图片。先验收联系人隔离、视觉输入确实上传、错误归类、超时未知结果不重提；只形成草稿，不接微信发送。验证码或限流交人工处理，不能承诺通过换 IP 或请求头避免账号处罚。所有候选尚未接入当前部署；动态表情理解也尚未验收。

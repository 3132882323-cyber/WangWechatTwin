# Third-party notices

The complete source of this project is published. This does not relicense the
WeChat client, hosted AI models, or external SDKs.

## Included source

| Source | License | Included files |
| --- | --- | --- |
| [wcdb-key-tool](https://github.com/TANGandXUE/wcdb-key-tool) | MIT | `scripts/wcdb_readonly.py`, `app/adapters/history_crypto.py` |
| [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) | Apache-2.0 | selected compatibility helpers in `app/adapters/accessibility_bridge.py` |
| [cpp-httplib 0.12.6](https://github.com/yhirose/cpp-httplib) | MIT | `native_transport/deps/httplib.h` and license text |
| [JSON for Modern C++ 3.11.3](https://github.com/nlohmann/json) | MIT | `native_transport/deps/json.hpp` and license text |

Original license texts and modification notes are included. See `NOTICE`.

`native_transport/bridge.cpp` is this project's original native transport using
authorized runtime layout observations and Windows public APIs. It does not
redistribute the researched WeChat-Hook implementation or its binary. The
transport does not use MinHook, GUI automation, recall patches or license bypass.

## External dependencies

Python packages are installed from their publishers rather than bundled as
binaries. The optional `wxauto4==41.1.7` GUI driver is distributed separately
under its publisher's terms; this repository does not grant a license to that
driver. The project does not depend on paid `wxautox4` activation and does not
include an activation bypass. OpenAI usage remains subject to the chosen account
or API plan. WeChat is a third-party client, not supplied or endorsed here.
# 媒体识别新增来源

`app/media_images.py` 和 `app/media_voice.py` 的图片解码、媒体关联及 SILK 解码部分改编自 `ikevss/wechat-ai-memory`，MIT 许可证保存在 `licenses/WECHAT_AI_MEMORY_LICENSE.txt`。图片参数派生参考 `jiatj/wechatapi` 的 Apache-2.0 实现；本项目的准备脚本独立实现派生与双样本验证。

可选依赖 `faster-whisper`、`silk-python`、`pycryptodome`、PyAV 及其依赖通过包管理器安装，不随仓库打包。下载的语音模型与用户音频、图片、密钥不进入公开源码。

# 启动实例锁与恢复

后台和 supervisor 使用 `SingleInstanceLock`。固定 `.lock.oslock` 文件承载生命周期内核锁：Windows 使用固定偏移字节锁，macOS/POSIX 使用 flock。`.lock` 是原子发布的诊断 JSON，包含 PID、创建时间和角色；PID 存活本身不作为新协议的独占依据。

进程异常退出时内核会释放句柄，下一次启动可获得锁。首次创建 `.oslock` 后崩溃、写诊断前崩溃和 JSON 临时文件写半后崩溃均有隔离子进程测试。释放只操作自己持有的句柄，失败的竞争者不能删除另一个实例的锁。

旧版纯 PID 文件迁移时，先核对实际进程命令、角色及项目归属。PID 对应无关程序或原进程已退出时可接管；活的旧版实例、权限不足或身份不确定时保留文件并明确报告原因。旧版自身遗留的空、乱码或无法识别文件仍需要在确认旧进程退出后处理；新协议不会发布这种半成品诊断文件。

不要手动删除运行中的锁文件，也不要让旧版 PID-only 启动器和新版同时工作。尤其 POSIX flock 依赖所有新版启动器使用同一稳定文件身份，删除路径会破坏这一协作协议。停止新版后再进行版本回退。

未取得 supervisor 锁时，固定诊断写入 `.runtime/startup/startup.jsonl` 并输出 stderr，正常重复启动不会成为崩溃重启风暴；CLI 同时记录 `runtime_lock_rejected`。日志出现锁拒绝不等于实际后台已退出，需结合进程归属及健康心跳核对。

参考了 [filelock 的 Windows 实现](https://github.com/tox-dev/filelock/blob/main/src/filelock/_windows.py)（MIT）与 [psutil 的 PID/创建时间身份实现](https://github.com/giampaolo/psutil/blob/master/psutil/__init__.py)（BSD）；没有复制上游代码或增加依赖。

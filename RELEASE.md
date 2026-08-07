# v1.1.0

## 流式传输 & 断点续传

- **ACK 流控**：PC ↔ 设备双向流控，在途字节超限自动等待 ACK。解决 ArkTS 单线程阻塞时客户端灌爆接收缓冲区的根因（实测 64MB 在 33MB 处 Broken pipe）
- **断点续传**：v2 协议首 u32 魔数（`BRN2`）分流，设备侧 `.part` 保留 + 头部探针比对 + 偏移协商。中断后 `br push` 同一文件自动从断点继续，不重传已完成部分
- **流式发送**：客户端不再一次加载整个文件（`payload = f.read()`），改为分块 `read(n)` + `sendall`。内存占用与文件大小无关
- **流式落盘**：PushServer 引入 `RecvState` 状态机，HEADER 解析后立即 `openSync`，BODY 阶段边收边 `writeSync`。数据不积压在内存，支持至 1GiB 单文件

## 新命令

- **`br pull`**：从设备拉取文件到本地。复用 8888 端口，`PULL` 魔数（`0x4C4C5550`）分流，设备侧 `handlePull()` 分块回传。支持进度条、文件不存在的错误提示

## 修复

- **`extractModel` 主线程阻塞**：14MB 模型同步写入会阻塞 ArkTS 事件循环，导致 PushServer 接收停摆。改为分块 `setTimeout` 让出主线程
- **文本解码 deprecation**：`TextDecoder.decode()` → `decodeToString()`
- **HAP 精简**：移除 `libmindspore-lite.so`、`libbenchmark.so`、`mobilenetv2.ms`。HAP 仅保留 ELF loader + PushServer + hello（~1.6MB）

## 工程

- **单文件 CLI 拆分**：`__main__.py` 从 474 行降至 11 行。按依赖方向分为 8 个模块（config / hilog / hdc / push / pull / runner / provision / cli），无循环依赖
- **202 个单测**：按模块重组（test_hilog / test_push / test_hdc / test_provision），含流控、续传协商、PULL 协议专项覆盖
- **CI**：ubuntu-22.04 + Docker SDK 镜像 + `build.sh` 一键构建。`v*` tag push 触发 GitHub Release，wheel 作为 release asset

## 文档

- `docs/push-spec.md` → `docs/transfer-spec.md`，覆盖 push + pull 双协议
- README 增加 `br pull`、`br rm`、多终端并发、短别名 `br`、前置依赖章节
- CLI 参考文档：10 个子命令完整参数、退出码、环境变量、自动行为表
- 新增 `docs/release-packaging.md`：pip wheel 打包方案

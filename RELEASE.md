# v1.4.1

## 修复

- **`br run` 在设备已结束、报告已写入 hilog 的情况下仍空等到超时**（#11，真机复现）：
  四个原因叠加
  - 每轮轮询都执行不带过滤的 `hilog -x`，等于每轮重读整个 hilog 缓冲区。真机实测全量
    22.8MB / 164,598 行 / 69.3s，远超单轮 5s 上限 → 每轮都在半途被强杀
  - 被强杀时已收到的部分输出被整批丢弃（`except TimeoutExpired: continue`），于是每轮都白跑，
    解析器一行都看不到（报告者 `--timeout 3600` → 空等 3630s）
  - 解析器要求先看到 `>>> exec` 启动行，而该行会在数分钟的运行中被环形缓冲淘汰 →
    设备已写好的完整报告被整体忽略
  - 历史运行留在缓冲区里的行会拖慢之后**每一次**轮询：实测跑过一次 1.8MB 输出后，
    连只有 2 行的 run 也会等到 40s 后失败（40s vs 修复后 2.1s）

- 修复内容：
  - 轮询改为 `hilog -x -T BinRunner -e '\[run_id\]'`：只读本 App tag 下**本次执行**的行，
    单轮 dump 代价与本 run 的数据量同阶（`-T` 去系统噪声，`-e` 去历史执行）；
    设备 hilog 不支持这些参数时自动退回全量 dump
  - 轮询超时保留已收到的部分输出（只丢弃可能被截断的末行），慢链路下仍能逐步推进解析
  - 带 `[run_id]` 前缀的行本身即归属依据，不再要求启动行存在（旧版 App 的完整报告同样受益）
  - 缺块不再空等：确认设备已结束（必要时用 `-e streamChunks` 探测，代价恒定 ~0.6s）且长时间
    无新数据，立即报「输出数据不完整（收到连续 X/Y 块）：设备已结束」并返回失败；
    设备仍在执行时继续等到 `--timeout` + 30s
  - `br logs` 不再因轮询超时抛出未捕获异常而崩溃（积压大时原先直接 traceback 退出）

- 单测 163 个全绿（新增 8 个：run 级过滤、探测后快速失败、设备仍在跑时不误判、
  部分输出仍可完成报告、启动行丢失仍接受报告、hilog 拒绝过滤参数时退回全量等）

## 文档

- README §5/已知限制、`docs/cli-reference.md` §流式输出协议、`docs/concurrency-spec.md`
  同步新的轮询方式与失败语义，并记录真机实测的通道上限

## 已知限制

- 单次输出 ≥ 约 1MB 时 hilog 回传带宽不足（真机 USB 实测 ≈ 325KB/s，数据块十六进制后有
  2.2 倍膨胀），无法完整送达：现在会明确失败并保留已显示输出，不再空等到 `--timeout`，
  也不会影响后续运行；建议拆小单次输出量，彻底解决需改为文件/TCP 回传
- `hilog -x -z <n>`（尾部窗口）在真机上与 `-x` 组合无效，无法用窗口化读取规避上述上限

# v1.4.0

## 新特性

- **流式输出手机端日志**（#12，jzh18）：`br run` 不再等进程退出才回传，执行期间持续显示
  stdout/stderr
  - 设备侧：native 工作线程每从管道读到数据即发 `[run_id] STREAM <seq> <stdout|stderr> <hex>`，
    每块最多 400 原始字节、十六进制编码（避免换行/控制字符/日志截断破坏协议），块间 2ms 节流
  - Host 侧：启动等待后每 0.5 秒 `hilog -x` 轮询（实际延迟另加 hdc 耗时），按序号重组去重、
    UTF-8 增量解码，立即写入对应 stdout/stderr 并 flush；无换行片段也会显示
  - 结束时仅回传 `exit=… streamChunks=N` 状态行（写入 Host stderr），不重复打印已显示的日志；
    收齐总块数与最终状态即可结束，允许 `<<< END` 丢失
  - 缺块时继续轮询至 `--timeout` 加 30s 报告预留期，提示「输出数据不完整」并返回失败，
    已显示的输出保留
  - `br run` 不再执行全局 `hilog -r`，避免清掉其他并发会话的日志
  - 兼容：新版 CLI × 旧 App 回退完整报告；旧 CLI 未传 `stream=1` 时新版 App 也发完整报告；
    `ls` / `rm` / probe 保留原报告形式。目标程序自身缓冲仍需自刷
    （C `fflush(stdout)`、Python `-u`）
- **单文件推送上限 1GiB → 4GiB**（#5）：`br push` / `br pull` 的策略护栏放宽到 4GiB
  （双端常量一致：`binrunner/config.py` 与 `PushServer.ets`，含等值放行）。拒绝语义不变
  （`size > MAX_FILE_SIZE` 才拒绝），设备空间不足仍走 ENOSPC 失败路径清理 `.part`。
  决策记录见 [docs/adr/0001](docs/adr/0001-single-file-size-cap-4gib.md)

## 工程

- 单测 **155 个全绿**：新增 `tests/test_streaming.py` 10 个（流式时序、乱序重组、缺块失败、
  `<<< END` 丢失、UTF-8 跨块、run_id 隔离），`test_runner` 补流式下的超时与报告宽限覆盖
- `release.yml` Release 正文只取 RELEASE.md 顶部当前版本一节，不再把全部历史贴进每个 Release

## 文档

- README §5 增补「流式输出」、§7.2 标注 4GiB 上限；`docs/cli-reference.md` 新增
  [流式输出协议](docs/cli-reference.md#流式输出协议)；`docs/concurrency-spec.md` 补流式补充章节
- `docs/transfer-spec.md` 单文件上限同步为 4GiB；新增 `docs/transfer-glossary.md`
  与 ADR-0001

# v1.3.0

## 新特性

- **可配置设备端执行超时**（#3/#6，jzh18）：`br run --timeout` 把秒数下传设备
  （`timeout_sec`），设备侧 `BinRunner.run` 按 int32 校验后设执行期限，不再固定 30s；
  主机端在超时外另留 30s 报告回传宽限。规避“设备已超时被杀但 CLI 空等/误判超时”
  的错位
- **CI 签名迁往 Secrets + nightly 归档**：
  - `.github/docker/certs/` 自签材料移出仓库，私钥/Profile/证书（`BINRUNNER_*`）
    全部改经 GitHub Secrets 注入，仓库不落可安装私钥明文
  - 新增 `hap-sign.yml`：main push / `v*` tag / 定时 / 手动触发构建并签名 HAP，
    main 与定时构建滚动更新 `nightly` prerelease（signed + unsigned 两个下载包）
  - 新增 `build-sdk-image.yml`：一次性构建预装 Command Line Tools + SDK 的容器镜像
  - `release.yml` 打通签名 Secret，`v*` tag 时 wheel 内置签名 HAP
  - `build.sh` 支持 Secrets base64 还原签名材料；新增 `scripts/sign-hap.sh`
    自助签名入口

## 工程

- 单测 143 个全绿（test_runner 新增设备执行超时/参数校验覆盖）

# v1.2.0


## 新特性

- **`br push` 保活与自愈**（对抗熄屏挂起与 fport 隧道回收）：
  - 屏幕熄灭后 EntryAbility 进后台，PushServer 的 8888 监听被系统挂起，长传表现为
    连接建立但无响应 / 中途 Connection refused → CLI 保活线程（`keepalive.py`）
    每 10s `power-shell wakeup` 点亮屏幕；App 前台 `setWindowKeepScreenOn(true)`
    常亮，双保险
  - hdc fport 隧道可能被系统回收，但本地端口仍被残留进程占用 → 每 5s
    `hdc fport ls` 巡检确认真实规则，丢失即 `ensure_forward(force=True)`
    删旧重建
  - 失败重试改「自愈组合拳」：重建隧道 + 唤醒屏幕 + **首连**失败才重启 App
    （后续重试保留续传状态不打断）；`_read_ack` 超时改抛 `TimeoutError` 供续传
  - PushServer 活跃连接上限 4：拒绝 CLI 重试遗留的未关闭连接，防单线程事件循环被拖垮
- **App 版本与 Python 包版本联动**：
  - 新增 `scripts/sync_app_version.py`，`build.sh` 构建 HAP 前把 `__version__`
    同步到 `app/AppScope/app.json5`（`versionName` + `versionCode` 单调递增映射）
  - 修复「包 1.1.2 但 App 报 1.0.0」的版本错位，本次 App 版本 1.2.0 / 1002000

## 修复

- **GPU/NPU 限制归因修正**：BinRunner 不限制 GPU/NPU 驱动访问（沙箱内二进制可 dlopen
  系统驱动库），实测只能走 CPU 是 MindSpore Lite 尚未适配鸿蒙 OS 的 GPU/NPU 驱动

## 工程

- 单测 **125 个全绿**（test_hdc / test_push 覆盖保活、自愈组合拳新逻辑）

## 文档

- README §7.2 记录「熄屏挂起 PushServer」已知坑与双保险缓解
- `docs/transfer-spec.md` 新增「传输可靠性（保活与自愈）」章节
- `docs/release-packaging.md` 版本联动说明；GPU/NPU 限制修正同步至 README 与 cli-reference

# v1.1.2

## 工程

- **签名材料轮换**：debug 签名换用基于新手机重新自动签名的证书/密钥/profile
  （`default_app_*`，含新设备 UDID 白名单），仓库 `.github/docker/certs/` 与
  `app/build-profile.json5` 同步更新。新手机可直接 `br setup` 安装 HAP 并调试

## 文档

- 新增 `docs/device-onboarding.md`：换新手机接入指南（设备发现 vs 签名白名单两层 UDID、
  换手机 5 步流程、故障排查表）
- README 第 1 节签名说明修正：debug profile 内嵌设备 UDID 白名单，预编译 HAP
  同样受白名单限制，换新手机需重新自动签名 + 重建

# v1.1.1

## 修复

- **发布版本号**：v1.1.0 发布时忘升 `__version__`，wheel 文件名与 `br version` / `pip show` 均误报 1.0.0，导致已装 1.0.0 的用户 `pip install -U` 无法升级（#1）。内容与 v1.1.0 完全一致，仅修正版本号

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

# `br` CLI 参数参考

## 全局选项

适用于所有子命令，放在子命令之前。

| 选项 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| `-t` | `UDID` | 自动检测 | 目标设备 UDID。多台设备时必须指定；仅一台时自动选用。也可用环境变量 `BINRUNNER_DEVICE` |
| `-p` | `PORT` | 见 `protocol/protocol.json` | PushServer TCP 转发端口，`push` / `pull` / `forward` 共用 |

hdc 不在 PATH 时自动尝试 DevEco Studio 默认路径：
`/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc`

> 设备选择只解决“跟哪台设备通信”；换新手机能否**安装** HAP 取决于签名 profile 白名单，
> 详见 [docs/device-onboarding.md](device-onboarding.md)。

## 子命令

### `br devices`

列出已连接设备。

```bash
br devices
# 输出:
# 0123456789ABCDEF

br -t 0123456789ABCDEF devices  # 指定设备（效果同上）
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | — |

---

### `br forward`

建立 `hdc fport` TCP 转发，将 PC 端口映射到设备端口。

```bash
br forward
# 输出:
# OK: 127.0.0.1:<port> -> device:<port> (0123456789ABCDEF)

br -p 9999 forward              # 指定非默认端口
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | — |

**幂等**：已建立转发时直接返回，不重复创建。

---

### `br setup`

手动安装或升级设备上的 BinRunner HAP。无需单独执行 —— `br run`/`push`/`ls`/`rm`/`logs`
在首次使用时自动调用。

```bash
br setup                         # 安装（已安装则提示跳过）
br setup --reinstall             # 覆盖升级（保留推送文件）
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `--reinstall` | 否 | 覆盖安装，用于升级 HAP |

`br setup` 只接受 `--reinstall`；设备选择一律用全局 `-t UDID` 或 `BINRUNNER_DEVICE`（见「全局选项」）。

**版本不一致不会自动升级**：`br setup` 会显示版本差异，升级必须显式 `--reinstall`。
业务命令只在没装 HAP 时自动安装；CLI/HAP 主次版本不同时直接拒绝执行，防止 ≤1.4.x / 1.5.x
路径根不同导致误读写。同一主次版本的补丁版本差异保留 stderr 警告。`br version` 可查看两侧版本。

HAP 来源：pip 包内 `binrunner/data/binrunner.hap`，开发模式回退到工程构建产物。

---

### `br version`

显示 CLI 和设备版本。

```bash
br version                       # CLI 版本 + 设备版本
br version                       # 无设备时仅显示 CLI 版本（不触发安装）
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | 不触发 HAP 自动安装 |

---

### `br push`

自 1.5.0 起路径统一以 files 根目录为起点，支持 `@/`；详见 [路径规格](path-spec.md)。

推送文件或目录到设备 `filesDir/`。自动建立转发（如需）、自动拉 App（如未运行）。

#### 推送文件

```bash
br push ./benchmark                             # 推送到 filesDir/benchmark
br push ./libfoo.so libbar.so                   # 推送到 filesDir/libbar.so
br push ./config.json                           # 推送到 filesDir/config.json
```

#### 推送目录

```bash
br push ./mylibs/                               # 递归推送，保持子目录结构
# mylibs/benchmark      → filesDir/benchmark
# mylibs/models/net.ms  → filesDir/models/net.ms
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `local` | ✅ | 本地文件或目录路径 |
| `remote` | 文件时可选 | 远端名（目录模式不支持）。默认取本地文件名 |

**限制**：`remote` 名不超过 `MAX_REMOTE_NAME_BYTES` 字节（见 `protocol/protocol.json`）；拒绝 `..`、绝对路径、`\` 分隔符。

**协议**：TCP 连接到 `127.0.0.1:<port>`，单连接单文件，小端字节序。
详见 [docs/transfer-spec.md](transfer-spec.md)。

---

### `br run`

在设备上执行二进制，捕获 stdout/stderr 并打印到终端，退出码透传。

```bash
br run "hello"                                  # 执行 hello，无参数
br run "hello foo bar"                          # 执行 hello，传入 ['foo', 'bar']
br run "benchmark --modelFile=@/mobilenetv2.ms --loopCount=5"
br run "@/custom-binary --flag"             # 绝对路径执行

# 特殊：列出设备目录
br ls                                           # files 根目录
br ls "@"                                   # 推送文件目录
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `cmdline` | ✅ | 命令行字符串。第一个词是二进制名或绝对路径，其余为参数 |
| `--timeout` | 否（默认 60s） | 设备端执行期限，1–2147483647 整数秒；主机额外等待 30 秒供启动、调度和报告回传 |

例如 `br run --timeout 1800 "benchmark --modelFile=@/mobilenetv2.ms --loopCount=1000"`
将 `timeout_sec=1800` 随启动参数传至设备，native 执行超时后以 SIGKILL 终止子进程，
报告汇总行给出 `exit=-1 timedOut=true timeoutSec=1800`（正常完成也带 `timeoutSec`）。
设备侧的 `exit=-1` 由 CLI 映射为退出码 1；主机从启动请求返回后使用单调时钟计算等待上限
（本例为 1830 秒），等待报告超时同样返回 1，并分别显示设备执行期限和额外预留时间；
这不表示设备一定已被终止，也不会主动取消设备任务。

CLI 和设备 HAP 必须同时更新。旧 v1.1.5.0 HAP 会忽略新参数，仍在 30 秒后终止任务；
构建后安装修复后的 HAP，使用打包 CLI 时可通过 `br setup --reinstall` 覆盖安装。
手工启动可用 `aa start ... --ps timeout_sec 1800 --ps cmd ...`。
设备未收到 `timeout_sec` 时默认 30 秒，以兼容旧 CLI 和页面调用；非法值会输出 `exit=1`
错误报告且不执行命令。超时参数按每次请求独立保存，并发执行互不覆盖。

**名字解析顺序**：绝对路径直通 → `filesDir/<name>`（推送目录） → `libs/arm64/lib<name>.so`（打包目录）

**`@` 展开**：词首 `@/` 或等号后 `=@/` 自动展开为 `filesDir` 路径：
- `@/mobilenetv2.ms` → `/data/storage/el2/base/haps/entry/files/mobilenetv2.ms`
- `--modelFile=@/m.ms` → `--modelFile=/data/storage/.../m.ms`
- 参数中间的 `@`（如 `user@host`、`@fd`）不展开

**并发隔离**：每次执行自动生成 8 位随机 run_id，hilog 输出带 `[run_id]` 前缀，多终端互不干扰。详见 [docs/concurrency-spec.md](concurrency-spec.md)。


#### 流式输出协议

新版 CLI 启动任务时传入 `--ps stream 1`。App 将本次 `run_id` 传给 native
工作线程；每次从 stdout/stderr 管道读到数据就发送日志，无需等待进程退出。
Host 经过约 1 秒启动等待后，每隔 0.5 秒执行
`hilog -x -T BinRunner -e '\[run_id\]'`，即**只读本 App tag 下本次执行的行**
（`-T` 去系统噪声，`-e` 去历史执行留在缓冲区里的行；实测真机全量 `hilog -x` 是
22.8MB/69s，加过滤后与本 run 的数据量同阶；设备 hilog 不支持这些参数时自动退回全量 dump。
实际延迟另加 hdc 耗时），收到数据立即写入对应的 stdout/stderr 并 flush。
无换行片段也会显示；UTF-8 跨块增量解码。
两个管道各自保持顺序，跨管道顺序取决于设备读取顺序。

```text
[run_id] >>> exec hello args=[]
[run_id] STREAM 0 stdout 68656c6c6f0a
[run_id] STREAM 1 stderr 7761726e0a
[run_id] <<< exit=0 timedOut=false timeoutSec=60 streamChunks=2
[run_id] <<< END
```

- 数据块使用从 0 开始、stdout/stderr 共用的递增序号，每块最多 400 原始字节，
  十六进制编码避免换行、控制字符和日志截断破坏协议。Host 按序重组并按序号去重，
  相同内容的不同数据块不会被误删。
- 收齐总块数与最终状态即可结束，允许 END 丢失。结束状态写入 Host stderr，
  不重复输出已显示的 stdout/stderr；手机 UI 仍保留完整报告。
- hilog 不保证无损，且回传带宽有限（实测 USB 链路约 325KB/s，而数据块是 2.2 倍膨胀的十六进制，
  程序持续高速输出时会超过该上限）。缺块时不会静默成功，也不会空等：确认设备已结束且长时间
  无新数据后，立即报「输出数据不完整（收到连续 X/Y 块）：设备已结束」并返回失败，
  已经显示的输出保留。设备仍在执行时最多等待 `--timeout` 加 30 秒（Host 超时不会取消设备任务；
  native 执行时限由 `--timeout` 传入，额外 30 秒用于启动和报告回传）。
- 单次轮询超时（hdc 被强杀）不会丢弃已收到的数据，只丢弃可能被截断的末行；因此
  慢链路下仍能逐步推进解析。同理，报告不依赖 `>>> exec` 启动行是否还在缓冲区里：
  带 `[run_id]` 前缀的行本身已能确定归属。
- 单次输出很大（≥ 约 1MB）时 hilog 回传跟不上，属于通道能力上限而非死等；
  建议拆小单次输出量，或等后续版本改为文件/TCP 回传。
- 不再清空全局 hilog 缓冲区，以免破坏其他并发会话。
- 目标程序自身的 stdio 缓冲仍需由程序刷新：C 可用 `fflush(stdout)`，Python 可用 `-u`。
- 需要更新并安装包含此功能的 App 才能流式输出。新版 CLI 兼容旧版 App 的完整报告；
  旧版 CLI 未请求 `stream=1` 时，新版 App 也继续发送完整报告。`ls`、`rm` 和探测命令保留原报告形式。

---

### `br ls`

列出设备目录内容（大小、修改时间、名称）。

```bash
br ls                                           # files 根目录
br ls "@"                                   # 推送文件目录
br ls "@/models"                            # 子目录
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `path` | 否（默认 files 根目录） | 设备侧路径，支持 `@` 展开 |

**注意**：这是 ArkTS 内置命令，不走 native 二进制执行路径，不属于 NAPI fork 流程。

---

### `br rm`

删除已推送的文件或目录（递归）。

```bash
br rm "hello"                                    # 删除 filesDir/hello
br rm "subdir"                                   # 递归删除 filesDir/subdir/
br rm "@/models/net.ms"                      # 绝对路径删除
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `path` | ✅ | 设备侧路径。相对路径默认基于 `filesDir/`（推送目录），`@/` 或 `/` 开头按实际路径 |

**路径解析**：
- `"hello"` → `filesDir/hello`（推送目录默认）
- `"@/hello"` → `filesDir/hello`（显式指定）
- `"/data/..."` → 原样使用（绝对路径）

**安全检查**：拒绝删除 `filesDir` 根目录、根目录别名（如 `@/`、`.`）或 files/ 之外的路径；拒绝 `..`。

---

### `br pull`

从设备拉取文件到本地。

```bash
br pull "hello"                                  # 下载 filesDir/hello → ./hello
br pull "models/net.ms"                          # 下载 → ./net.ms
br pull "hello" ./my-hello                       # 指定本地路径
br pull "models/net.ms" ./models/                # local 是目录 → 存到该目录下同名文件
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `remote` | ✅ | 设备侧文件名（默认基于 filesDir/） |
| `local` | 否（默认当前目录） | 本地保存路径；是目录时存为其下同名文件 |

**续传协商**：拉取前先看本地 `<local>.part` 残片。有残片就用 PULL v2：把残片长度作为
`offset`、残片头部作为探针发给设备，设备比对**源文件**头部后回 `resumeAck`（可复用字节数）。
不符或设备要求整传时 `resumeAck = 0`，本地残片作废、从头重传。

**落盘**：数据写入 `<local>.part`，收满后 `os.replace` 原子改名为正式名 —— 正式名下永不
出现半成品，中断的 `.part` 供下次续传。

**保活与自愈**：拉取期间与 push 一样保活（屏幕唤醒 + fport 巡检）；中断按
`RESUME_MAX_ATTEMPTS` 重试，重试前 `ensure_forward(force=True)` 重建隧道并唤醒屏幕，
首连失败才重启 App，后续重试保留续传状态。

**v1 降级**：设备 HAP 较旧、不认 PULL v2 时既不响应也**不断连**（旧 HAP 的失败路径只关自己的
fd）。CLI 因此用专门的短协商超时（`PULL_NEGOTIATE_TIMEOUT`，3 秒）判定「不支持续传」，再退回
v1 整传 —— 降级后不按本地长度盲续，一律整传。该判断在单次 CLI 进程内缓存。
实测：旧 HAP + 本地 `.part` 的白等时间从 61 秒降到 3 秒。

**已知限制**：远端 0 字节文件与「不存在」不可区分（两者 `totalSize` 均为 0），拉取空文件
会报「设备上不存在」。

**协议**：复用 PushServer 的 `PORT` 端口，请求/响应布局见
[docs/transfer-spec.md](transfer-spec.md) 的「PULL 协议」。

---

### `br logs`

持续跟踪设备 BinRunner hilog 输出，Ctrl+C 退出。

```bash
br logs                                         # 实时跟踪，显示所有 BinRunner 日志
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | — |

**实现**：`_dump_hilog` 轮询（`hilog -x -T BinRunner`）+ seen 集合去重，1s 间隔。不传 run_id
过滤，显示本 App 所有会话的输出；设备 hilog 不认 `-T` 时退回全量 `hilog -x`。

---

## 退出码

| 场景 | 退出码 |
|---|---|
| 目标二进制正常退出 | 其自身退出码（如 `hello` 返回 42） |
| 目标二进制被信号杀死 | `128 + signal` |
| 设备侧执行超时（SIGKILL） | 1 |
| 命令未找到（ResolveExecPath 失败） | 255 |
| 线程/进程创建失败（pipe/fork） | 255 |
| CLI 侧等待报告超时 / 输出不完整 | 1 |

设备侧报告用 `exit=` 携带代码；`exit=-1` 是超时或未执行等元数据，CLI 把负值一律映射为 1
（若不映射，负值经 shell 会变成 255）。正常退出码原样透传。

## 环境变量

| 变量 | 说明 |
|---|---|
| `BINRUNNER_DEVICE` | 目标设备 UDID，优先级低于 `-t`，高于自动检测 |
| `DEVECO_SDK_HOME` | DevEco SDK 根目录（如 `/Applications/DevEco-Studio.app/Contents/sdk`） |

## 自动行为

| 场景 | 行为 |
|---|---|
| hdc 不在 PATH | 自动查找 DevEco Studio 默认路径 |
| 仅一台设备 | 自动选用，无需 `-t` |
| `push` 时转发未建立 | 自动 `hdc fport`（幂等） |
| `push` 时 App 未运行 | 自动 `aa start` 拉 App 后重试 |
| `run` 时 App 未运行 | App 自动冷启动（通过 `aa start`） |
| 旧版 App 无 run_id | 兼容 — 不过滤，处理所有 BinRunner 行 |

## 限制

| 项 | 说明 |
|---|---|
| hilog 带宽 | stdout/stderr 通过 hilog 回传，单条约 1000 字符上限；大输出可能丢块，TCP 回传尚属扩展方向 |
| 推送并发 | 同名文件后写覆盖，无锁 |
| 执行环境 | 二进制以 App uid 运行，受沙箱约束；seccomp 限制部分 syscall |
| GPU/NPU | MindSpore Lite 未适配鸿蒙 GPU/NPU 驱动，实测走 CPU；BinRunner 不限制驱动访问（沙箱内二进制可 dlopen 系统驱动库） |
| CLI 依赖 | 仅需 Python 3 + hdc（零第三方包），不支持 Windows（hdc 路径硬编码 macOS） |
| 设备端依赖 | debug 签名（jit prctl），零售机无 root |

## 实现文件

CLI 是 `binrunner/` 包（入口 `br` 或 `python3 -m binrunner`），不再有单文件实现；模块职责
与依赖关系见 [README 的「代码结构」章节](../README.md#代码结构)。

| 文件 | 职责 |
|---|---|
| [`binrunner/cli.py`](../binrunner/cli.py) | argparse 定义与子命令分发 |
| [`binrunner/__main__.py`](../binrunner/__main__.py) | `python -m binrunner` 入口，转发到 `cli.main` |
| [`binrunner/config.py`](../binrunner/config.py) | host-only 常量（设备标识、超时、退避、阈值） |
| [`binrunner/protocol.py`](../binrunner/protocol.py) | 生成：跨语言协议常量（勿手改；来源 `protocol/protocol.json`） |
| [`binrunner/hdc.py`](../binrunner/hdc.py) | hdc 封装与设备选择 |
| [`binrunner/transfer.py`](../binrunner/transfer.py) | 传输层公共件（建连、u64、远端名校验） |
| [`binrunner/push.py`](../binrunner/push.py) | push 策略与目录递归 |
| [`binrunner/pull.py`](../binrunner/pull.py) | pull 策略与续传协商 |
| [`binrunner/runner.py`](../binrunner/runner.py) | 执行与 hilog 轮询 |
| [`binrunner/hilog.py`](../binrunner/hilog.py) | `HilogParser`：单次遍历解析 |
| [`binrunner/keepalive.py`](../binrunner/keepalive.py) | 屏幕保活 + fport 巡检 |
| [`binrunner/provision.py`](../binrunner/provision.py) | App 安装、版本、全链路验证 |

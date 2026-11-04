# 文件传输协议规格说明

## 概述

`br push` / `br pull` 通过 `hdc fport` TCP 转发通道复用同一端口（`PORT`），实现
PC ↔ 设备双向文件传输。首 4 字节魔数分流 push/pull 协议，支持单文件、递归目录推送、
大文件流式传输和断点续传。

本文只写常量名、不复述取值：跨语言协议常量的唯一来源是
[`protocol/protocol.json`](../protocol/protocol.json)，由 `scripts/gen_protocol.py` 生成到
`binrunner/protocol.py` 与 `app/entry/src/main/ets/common/Protocol.ets`（生成物入库，勿手改）；
host-only 策略常量在 `binrunner/config.py`。

## 命令形式

```bash
# 单文件
br push <FILE> [NAME]
# 文件推送到 filesDir/<NAME>，省略 NAME 时用本地文件名

# 目录（递归）
br push <DIR>/
# 遍历目录树，以相对路径逐文件推送，保持子目录结构
# 目录模式不支持指定 NAME
```

开发时用 `python3 -m binrunner <子命令>`（工程根目录）等价于 `br`；不要用「把 `binrunner/`
目录当脚本执行」的写法 —— 它会把该目录放进 `sys.path[0]`，`binrunner` 包无法解析。

## 协议

TCP 连接到设备 `127.0.0.1:<PORT>`（经 `hdc fport` 转发至 App 内 PushServer）。

单连接单文件，小端字节序。协议有两个版本，设备侧**同时支持**（靠首个 u32 区分）：

### v1（简单模式，小文件）

```
┌──────────┬──────────────┬──────────────┬──────────────────┐
│ u32      │ nameLen 字节  │ u64          │ payloadSize 字节   │
│ nameLen  │ name (UTF-8) │ payloadSize  │ payload           │
└──────────┴──────────────┴──────────────┴──────────────────┘
```

### v2（断点续传模式，≥ `RESUME_MIN_SIZE` 的文件）

```
┌──────────┬──────────┬──────────┬──────────────┬─────────────┬──────────┬──────────────┐
│ u32      │ u32      │ u32      │ nameLen 字节  │ u64         │ u32      │ probeLen 字节 │
│ MAGIC    │ flags    │ nameLen  │ name (UTF-8) │ payloadSize │ probeLen │ probe        │
└──────────┴──────────┴──────────┴──────────────┴─────────────┴──────────┴──────────────┘
                              ↓ 设备回应
                    ┌──────────────────────┐
                    │ u64 resumeFrom       │  已落盘可复用字节数（0 = 从头传）
                    └──────────────────────┘
                              ↓ 客户端从 resumeFrom 起发剩余 payload
```

`probeLen | probe` 仅在 `flags & FLAG_RESUME` 时出现。

| 字段 | 类型 | 说明 |
|---|---|---|
| MAGIC | u32 LE | `RESUME_MAGIC`（见 `protocol/protocol.json`），取值大于 `MAX_REMOTE_NAME_BYTES`，故不会与 v1 的 nameLen 混淆 |
| flags | u32 LE | `FLAG_RESUME` 置位表示附带续传探针；无探针时为 0 |
| nameLen | u32 LE | name 的**字节数**，≤ `MAX_REMOTE_NAME_BYTES` |
| name | bytes | 远端相对路径（如 `benchmark`、`models/net.ms`） |
| payloadSize | u64 LE | 文件**完整**内容的**字节数**（不是剩余量） |
| probeLen | u32 LE | 探针**字节数** |
| probe | bytes | 文件**头部** `RESUME_PROBE_BYTES` 字节，用于确认 `.part` 属于同一文件 |
| resumeFrom | u64 LE | 设备回应：可复用的已落盘字节数 |

**版本协商**：v1 的首个 u32 是 `nameLen ∈ [1, MAX_REMOTE_NAME_BYTES]`；v2 的首个 u32 是
`RESUME_MAGIC`。设备读到首 4 字节即可判定，无需额外握手。旧客户端与新设备、新客户端与
小文件都走 v1。

payload 双端均为**流式处理**，不在内存中拼装完整报文 —— 故支持至 `MAX_FILE_SIZE` 的大文件：

- 客户端：先发协议头，再按 `TRANSFER_CHUNK_SIZE` 分块 `sendall` 读一块发一块
- 服务端：头部收齐即打开目标文件，后续 `message` 事件直接写盘

客户端发完即关闭连接；服务端在收满 `payloadSize` 字节时判定完成。

### ACK 流控

服务端每落盘一批就回一个 `u64` 累计已写字节数（**含** `resumeFrom`，即文件绝对进度）。
客户端据此限制在途未确认数据量（`MAX_INFLIGHT_BYTES`），避免 `hdc fport` 转发链路
缓冲区被灌满导致设备侧 OOM 或长时间卡死。

## 递归目录推送流程

### CLI 侧（`binrunner/push.py`）

```
push_tree(local_dir)
  ├── os.walk(local_dir)  遍历目录树
  ├── 对每个文件计算 relpath = os.path.relpath(full, local_dir)
  │   例: mylibs/models/net.ms → "models/net.ms"
  └── 逐文件流式发送（不读入整个文件）
       ├── 转发已建立则复用，未建立则 hdc fport（幂等）
       ├── 校验远端名与大小 → 发协议头 → 按 `TRANSFER_CHUNK_SIZE` 分块 sendall
       └── 连接失败（App 未运行）→ 自动 aa start 拉 App 后重试一次
```

### PushServer 侧 (`PushServer.ets`)

流式状态机，header 到齐即开文件，payload 边收边写盘：

```
HEADER ──头部收齐&校验通过──> BODY ──收满 payloadSize──> DONE
   │                            │
   └── 校验失败 ──> FAILED <─────┘ 写盘失败/超出声明大小

message 事件:
  HEADER: v1 攒够 `MIN_HEADER_BYTES` / v2 攒够完整头 → 解析 → 安全校验
          v2 额外：比对 .part 头部探针
            ├── 探针一致且 .part 长度 < payloadSize → resumeFrom = 长度，APPEND 打开
            └── 探针不符/无 .part                   → resumeFrom = 0，TRUNC 打开
          → 回 u64 resumeFrom → mkdir 父目录 → 转 BODY
          → 同包内的 payload 部分立即写盘
  BODY:   writeSync(fd) 直接落盘，累加 written，回 ACK(resumeFrom + written)
  DONE:   rename(.part → 目标名)；再收到数据 = 客户端发多了 → FAILED

close 事件:
  DONE   → 记录成功（此时目标文件已是完整的正式名）
  其他   → 连接提前断开，保留 .part 供下次续传（不再 unlink）
```

**落盘用临时名**：传输期间写入 `<name>.part`，收满后才 `rename` 为 `<name>`。
这保证了正式文件名**永远不会**指向不完整内容 —— 半成品二进制被 `br run` 执行会
因段数据不全而 SIGSEGV，排查方向极易被误导。

## 安全校验

CLI 侧和 PushServer 侧**双重校验**，规则一致：

| 规则 | 拒绝的 name | 原因 |
|---|---|---|
| 空名 | `""` | 无效 |
| 绝对路径 | `"/etc/passwd"` | 禁止脱离 recvDir |
| Windows 分隔符 | `"a\\b"` | 平台不一致 |
| 当前目录 | `"."` | 无效 |
| 上级引用 | `".."` | 目录穿越 |
| 上级穿越 | `"a/../b"`, `"a/.."` | 目录穿越（含 `/../` 或以 `/..` 结尾） |

**允许**的 name 格式：
- `"benchmark"` — 纯文件名
- `"models/net.ms"` — 带子路径
- `"a/b/c/data.bin"` — 多层嵌套

## 路径约定（1.5.0 不兼容变更）

`recvDir = filesDir`。CLI 先将 `@/x` 归一化为 `x`；协议只接收规范相对路径，
不允许空段、`.`、`..`、独立 `@` 段、绝对路径、反斜杠或 NUL。
双端路径校验一致。报文布局不变，但路径根语义已变，≤1.4.x / 1.5.x 不得混用。
新 CLI 的主次版本检查在发起业务请求之前阻止混用。详见 [路径规格](path-spec.md)。

## 设备端文件布局

```
filesDir/               ← recvDir，推送根目录
├── benchmark               ← 单文件推送
├── libdep.so               ← .so 依赖（必须放根层级）
├── config.json             ← 数据文件（放根层级或子目录均可）
└── models/                 ← 子目录（目录推送自动创建）
    ├── net.ms
    └── vocab.bin
```

## `LD_LIBRARY_PATH` 与子目录

动态链接器搜索路径：

```
LD_LIBRARY_PATH = filesDir : <bundle libs 目录>
```

**仅搜索 `filesDir/` 根目录**，不递归搜索子目录。

这意味着 `.so` 依赖必须直接推送到根层级：

```bash
binrunner push ./libfoo.so        # ✅ 动态链接可解析
binrunner push ./libs/libfoo.so   # ❌ 动态链接找不到
```

**例外**：通过绝对路径 `@/libs/libfoo.so` 显式指定的 .so 不受此限制，但一般场景不涉及。

数据文件（模型、配置等）可自由使用子目录组织：

```bash
binrunner push ./my-models/
# → @/my-models/net.ms
# → @/my-models/vocab.bin
```

## name 长度限制

- 协议 `nameLen` 字段为 u32（宽度上界 4GB），但代码限制 ≤ `MAX_REMOTE_NAME_BYTES` **字节**
- 限制的是 UTF-8 编码后的字节数，不是字符数
- 子路径增大了超限风险（如 `very-long-directory-name/very-long-filename.so`），但 `MAX_REMOTE_NAME_BYTES` 字节对正常文件名绰绰有余

## 文件大小限制

单文件上限 `MAX_FILE_SIZE`（见 `protocol/protocol.json`），双端校验：

| 位置 | 时机 | 行为 |
|---|---|---|
| CLI | `os.path.getsize` 后、打开文件前 | 超限即 `sys.exit`，不读取内容 |
| PushServer | 解析头部时 | 超限即 `FAILED`，不创建文件 |

协议 `payloadSize` 为 u64（Python `struct "<Q"` / ArkTS 双 u32 拼接），此限制是**策略性**
的 —— 用于挡住误操作（如误推整个镜像），而非技术瓶颈。双端流式处理，
内存占用与文件大小无关；设备剩余空间不足时在写盘中 ENOSPC 失败并清理 `.part`。

上限由 `protocol/protocol.json` 的 `MAX_FILE_SIZE` 单点定义，生成到双端；调整时改 JSON
后重新生成（`python3 scripts/gen_protocol.py`），不要再手改 `binrunner/config.py` 或
`PushServer.ets`。

### 流式传输参数

| 参数 | 值 | 位置 | 说明 |
|---|---|---|---|
| `TRANSFER_CHUNK_SIZE` | 见 `protocol/protocol.json` | 双端 | 单次 `sendall` / 设备侧单次读发的分块，**push 与 pull 共用** |
| `ACK_INTERVAL` | 见 `protocol/protocol.json` | 双端 | 设备每落盘这么多字节回一个 u64 累计 ACK |
| `PROGRESS_THRESHOLD` | 4 MiB | config.py | 超过则在 stderr 打印传输进度 |
| `MAX_INFLIGHT_BYTES` | 8 MiB（2 × `ACK_INTERVAL`） | config.py | 在途未确认上限，超出则等 ACK（流控） |
| `RESUME_MIN_SIZE` | 4 MiB | config.py | push 达到此大小才用 v2 协议协商续传 |
| `RESUME_PROBE_BYTES` | 见 `protocol/protocol.json` | 双端 | 头部探针长度，**必须双端一致** |
| `RESUME_MAX_ATTEMPTS` | 5 | config.py | 单个文件的最大尝试次数（首传 + 续传重试） |
| `RESUME_BACKOFF` | 1.0 s | config.py | 第 n 次重试前等待 `RESUME_BACKOFF * n` |
| `ACK_TIMEOUT` | 60.0 s | config.py | 收发超时（`transfer.py` 用它同时约束 send 与等 ACK） |

### 断点续传

两个方向都支持续传：`br push` 对 ≥ `RESUME_MIN_SIZE`（4 MiB）的文件自动走 v2 协议；
`br pull` 只要本地存在 `<local>.part` 残片就用 v2 协商（哪怕只省一次往返）。
传输中断后重新执行同一条命令，会从已落盘处继续，而非从头重传。

```bash
br push ./big-model.bin        # 传到 60% 时网线被拔 / Ctrl-C
br push ./big-model.bin        # 自动续传：「续传自 19.2 MiB」，只发剩余 40%
```

**同一文件的判定**靠三重条件，任一不符即从头重传（宁可多传，不可传错）：

| 条件 | 作用 |
|---|---|
| `.part` 存在 | 有可续的基础 |
| 头部 `RESUME_PROBE_BYTES` 探针逐字节一致 | 确认是同一文件，而非同名的不同内容 |
| `.part` 长度 < `payloadSize`（pull 为 `offset < totalSize`） | 长度已达标说明是陈旧残留，不可信 |

单次命令内部还会**自动重试** `RESUME_MAX_ATTEMPTS`（5）次，退避间隔 `RESUME_BACKOFF`
（第 n 次等 `RESUME_BACKOFF * n`）。每次重试都重新协商偏移，因此瞬时网络抖动对用户是
透明的。push 与 pull 的重试/自愈策略同构，pull 侧协商细节见文末「PULL 协议」。

探针长度 `RESUME_PROBE_BYTES` 与其它协议常量一样由 `protocol/protocol.json` 单点定义、
生成到双端；改错一处会导致探针长度不匹配、续传永远失败（表现为「每次都从头传」，
不报错但白费流量）。

### 残留清理

中断后 `.part` 会保留（这是续传的前提）：push 侧在设备 `filesDir/` 下，pull 侧在
本地落盘路径旁。若确定不再需要：

```bash
br ls "@"                  # 设备侧 .part 后缀的即为未完成 push
br rm big-model.bin.part
ls ./big-model.bin.part        # 本地未完成 pull 的残片
```

### 传输可靠性（保活与自愈）

真机实测的**已知坑**（README §7.2）：屏幕熄灭后 EntryAbility 进后台，PushServer 的
`PORT` 监听被系统挂起；hdc fport 隧道也可能被系统回收，但本地端口仍被残留进程占用。
表现为 `br push`「连接建立但无响应 / 中途 Connection refused」。

对抗手段（CLI + App 双保险）：

| 手段 | 位置 | 机制 |
|---|---|---|
| 屏幕保活 | `binrunner/keepalive.py` | push / pull 期间后台线程每 10s `power-shell wakeup` 点亮屏幕 |
| fport 巡检 | `binrunner/keepalive.py` | 每 5s `hdc fport ls` 确认真实规则存在，丢失即 force 重建 |
| 自愈组合拳 | `binrunner/push.py`、`binrunner/pull.py` | 重试前先 `ensure_forward(force=True)` 重建隧道 + 唤醒屏幕；**首连**失败才重启 App（`aa force-stop` + `aa start`），后续重试保留续传状态不打断 |
| 前台常亮 | `app/.../EntryAbility.ets` | `setWindowKeepScreenOn(true)`，熄屏兜底 |
| 连接防泄漏 | `app/.../PushServer.ets` | 活跃连接上限 `MAX_ACTIVE_CONNECTIONS=4`，CLI 重试遗留的未关闭连接直接拒绝，防单线程事件循环被拖垮 |

`ensure_forward(force=True)` 会先 `hdc fport rm` 删旧规则再重建——仅靠本地端口探测
（`port_open`）会把残留进程占用的端口误判为隧道健康。

## 幂等性

- `hdc fport` 重复建立无害：`ensure_forward` 先经 `hdc fport ls` 确认真实规则，
  再回退本地端口探测；`force=True` 用于长传自愈时删旧重建（见上节「传输可靠性」）
- PushServer 收到同名文件直接覆盖（`OpenMode.CREATE | OpenMode.TRUNC`）
- App 未运行时自动拉 App 后重试一次

## 与打包 libs 的关系

| 维度 | 推送 bin/ | 打包 libs/ |
|---|---|---|
| 更新方式 | 免打包，hdc 推送 | 重新构建安装 HAP |
| 文件类型 | 普通文件，随机读正常 | bundle 内虚拟文件，随机读坏数据 |
| 名字格式 | 原样文件名 | 必须 `lib<name>.so` |
| loader 处理 | 先过 memfd（统一路径） | 先顺序读进 memfd 再加载 |
| 解析优先级 | **优先于** libs | 低于推送目录 |

## PULL 协议（设备 → PC）

复用同一端口（`PORT`）。首 4 字节是 pull 魔数，与 push 的 `nameLen` 值域不重叠，
PushServer 据此分流到 pull 处理器。与 push 一样分 v1 / v2 两版，靠首个 u32 区分。

### 请求（PC → 设备）

v1：

```
u32 PULL_MAGIC | u32 nameLen | name | u64 offset | u64 size
```

v2：

```
u32 PULL_MAGIC_V2 | u32 nameLen | name | u64 offset | u64 size | u32 probeLen | probe
```

- `offset` = 客户端本地 `.part` 的字节数（续传点），0 表示从头。
- `size` = 回传长度上限，0 表示不限；v2 保留该字段但未使用。CLI 两版都传 0。
- `probe` = 本地 `.part` 的头部（≤ `RESUME_PROBE_BYTES`）；设备拿它与**源文件**头部比对
  来证明同源。
- `nameLen` / `probeLen` 越界属于非法请求，设备同样回零响应（见下）。

### 响应（设备 → PC）

v1：

```
u64 totalSize | payload
```

v2：

```
u64 resumeAck | u64 totalSize | payload[resumeAck .. totalSize)
```

- `resumeAck` = 可复用字节数：0 = 从头重传；等于 `offset` 表示探针匹配、自该处续传；
  `offset == totalSize`（`.part` 已完整）时 `resumeAck = totalSize` 且无 payload。
- `totalSize = 0` 表示文件缺失或不可读，此时 `resumeAck` 也是 0。
- 失败路径一律显式回零响应（v1 8 字节、v2 16 字节），不再静默断连 —— 否则 CLI 会空等到
  socket 超时。

### 为什么另起魔数

v2 不复用 `PULL_MAGIC` 追加字段，而用独立的 `PULL_MAGIC_V2`：旧设备会把追加的字段误读成
`nameLen`，从而打开**错误的路径**。独立魔数让旧设备直接拒不认，CLI 再降级。

### 客户端降级与续传

- 新版 CLI 首次用 v2 协商。旧 HAP 收到不认的 `PULL_MAGIC_V2` 后走 push 的 `parseHeader` 并以
  `bad nameLen` 拒绝 —— 但它**只关自己的 fd，不关连接**，于是既无响应也无断连。因此协商窗口
  必须用**专门的短超时**（`PULL_NEGOTIATE_TIMEOUT`，3 秒）判定「不支持续传」，不能沿用
  `ACK_TIMEOUT`（实测会让每次带 `.part` 的 pull 白等 61 秒）。判定后把 `_PULL_V2_OK` 置为
  False 并退回 v1 整传，该标志只在单次 CLI 进程内缓存。
- v1 没有探针、无法验证同源，所以降级后**不按本地长度盲续**，一律从 0 整传（宁可多传，不可传错）。
- CLI 写入 `<local>.part`，收满后 `os.replace` 原子改名为正式名 —— 正式名下永不出现
  半成品，中断的 `.part` 供下次续传。
- 拉取期间同样保活（屏幕 + fport 巡检），中断按 `RESUME_MAX_ATTEMPTS` 重试并自愈，
  每次重试重新读 `.part` 长度协商，与 push 同构。
- **已知限制**：远端 0 字节文件与「不存在」不可区分（两者 `totalSize` 都是 0），
  拉取空文件会报「设备上不存在」。

### 安全校验

与 push 共用 `isSafeName()`，拒绝目录穿越、绝对路径、上级引用。

## 实现文件

| 文件 | 职责 |
|---|---|
| [`binrunner/push.py`](../binrunner/push.py) | push 策略：v1/v2 协议头、ACK 流控、重试与自愈、目录递归 |
| [`binrunner/pull.py`](../binrunner/pull.py) | pull 策略：v2 续传协商、`.part` 原子落盘、v1 降级、保活 |
| [`binrunner/transfer.py`](../binrunner/transfer.py) | 传输层公共件：`fmt_size`、u64 编解码、`open_connection`、`recv_exactly`、远端名校验 |
| [`binrunner/config.py`](../binrunner/config.py) | host-only 常量（超时、退避、进度阈值、在途上限、续传策略） |
| [`binrunner/protocol.py`](../binrunner/protocol.py) | 生成：Python 侧协议常量（勿手改） |
| [`protocol/protocol.json`](../protocol/protocol.json) | 跨语言协议常量唯一来源（端口、分块、魔数、探针、上限） |
| [`scripts/gen_protocol.py`](../scripts/gen_protocol.py) | 由 JSON 生成 `binrunner/protocol.py` 与 `app/entry/src/main/ets/common/Protocol.ets` |
| [`app/entry/src/main/ets/common/Protocol.ets`](../app/entry/src/main/ets/common/Protocol.ets) | 生成：设备侧协议常量（勿手改） |
| [`app/entry/src/main/ets/common/PushServer.ets`](../app/entry/src/main/ets/common/PushServer.ets) | 设备端 TCP server，push/pull 协议分流，流式收发 |
| [`app/entry/src/main/ets/entryability/EntryAbility.ets`](../app/entry/src/main/ets/entryability/EntryAbility.ets) | App 启动时初始化 `PushServer.recvDir` 并 start |
| [`tests/test_push.py`](../tests/test_push.py) | 协议编码、路径校验、流控、续传协商单测 |
| [`tests/test_pull.py`](../tests/test_pull.py) | pull 协商、`.part` 续传、降级单测 |
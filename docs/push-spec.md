# `binrunner push` 规格说明

## 概述

`binrunner push` 通过 `hdc fport` TCP 转发通道，将 PC 端文件推送到鸿蒙设备 App 沙箱的 `filesDir/bin/` 目录。支持单文件和递归目录推送。

## 命令形式

```bash
# 单文件
binrunner push <FILE> [NAME]
# 文件推送到 filesDir/bin/<NAME>，省略 NAME 时用本地文件名

# 目录（递归）
binrunner push <DIR>/
# 遍历目录树，以相对路径逐文件推送，保持子目录结构
# 目录模式不支持指定 NAME
```

## 协议

TCP 连接到设备 `127.0.0.1:8888`（经 `hdc fport` 转发至 App 内 PushServer）。

单连接单文件，小端字节序。协议有两个版本，设备侧**同时支持**（靠首个 u32 区分）：

### v1（简单模式，小文件）

```
┌──────────┬──────────────┬──────────────┬──────────────────┐
│ u32      │ nameLen 字节  │ u64          │ payloadSize 字节   │
│ nameLen  │ name (UTF-8) │ payloadSize  │ payload           │
└──────────┴──────────────┴──────────────┴──────────────────┘
```

### v2（断点续传模式，≥ 4 MiB 文件）

```
┌──────────┬──────────┬──────────────┬─────────────┬──────────┬──────────────┐
│ u32      │ u32      │ nameLen 字节  │ u64         │ u32      │ probeLen 字节 │
│ MAGIC    │ nameLen  │ name (UTF-8) │ payloadSize │ probeLen │ probe        │
└──────────┴──────────┴──────────────┴─────────────┴──────────┴──────────────┘
                              ↓ 设备回应
                    ┌──────────────────────┐
                    │ u64 resumeOffset     │  已落盘可复用字节数（0 = 从头传）
                    └──────────────────────┘
                              ↓ 客户端从 resumeOffset 起发剩余 payload
```

| 字段 | 类型 | 说明 |
|---|---|---|
| MAGIC | u32 LE | `0x42524E32`（"BRN2"），> 256 故不会与 v1 的 nameLen 混淆 |
| nameLen | u32 LE | name 的字节数，≤ 256 |
| name | bytes | 远端相对路径（如 `benchmark`、`models/net.ms`） |
| payloadSize | u64 LE | 文件**完整**内容的字节数（不是剩余量） |
| probeLen | u32 LE | 探针字节数，≤ 4096 |
| probe | bytes | 文件**头部** 4 KiB，用于确认 `.part` 属于同一文件 |
| resumeOffset | u64 LE | 设备回应：可复用的已落盘字节数 |

**版本协商**：v1 的首个 u32 是 `nameLen ∈ [1, 256]`；v2 的首个 u32 是 `0x42524E32`。
设备读到首 4 字节即可判定，无需额外握手。旧客户端与新设备、新客户端与小文件都走 v1。

payload 双端均为**流式处理**，不在内存中拼装完整报文 —— 故支持至 1GiB 的大文件：

- 客户端：先发协议头，再按 256KiB 分块 `sendall` 读一块发一块
- 服务端：头部收齐即打开目标文件，后续 `message` 事件直接写盘

客户端发完即关闭连接；服务端在收满 `payloadSize` 字节时判定完成。

### ACK 流控

服务端每落盘一批就回一个 `u64` 累计已写字节数（**含** `resumeOffset`，即文件绝对进度）。
客户端据此限制在途未确认数据量（`MAX_INFLIGHT_BYTES`），避免 `hdc fport` 转发链路
缓冲区被灌满导致设备侧 OOM 或长时间卡死。

## 递归目录推送流程

### CLI 侧 (`binrunner.py`)

```
push_tree(local_dir)
  ├── os.walk(local_dir)  遍历目录树
  ├── 对每个文件计算 relpath = os.path.relpath(full, local_dir)
  │   例: mylibs/models/net.ms → "models/net.ms"
  └── 逐文件流式发送（不读入整个文件）
       ├── 转发已建立则复用，未建立则 hdc fport（幂等）
       ├── 校验远端名与大小 → 发协议头 → 按 256KiB 分块 sendall
       └── 连接失败（App 未运行）→ 自动 aa start 拉 App 后重试一次
```

### PushServer 侧 (`PushServer.ets`)

流式状态机，header 到齐即开文件，payload 边收边写盘：

```
HEADER ──头部收齐&校验通过──> BODY ──收满 payloadSize──> DONE
   │                            │
   └── 校验失败 ──> FAILED <─────┘ 写盘失败/超出声明大小

message 事件:
  HEADER: v1 攒够 12 字节 / v2 攒够完整头 → 解析 → 安全校验
          v2 额外：比对 .part 头部探针
            ├── 探针一致且 .part 长度 < payloadSize → resumeFrom = 长度，APPEND 打开
            └── 探针不符/无 .part                   → resumeFrom = 0，TRUNC 打开
          → 回 u64 resumeOffset → mkdir 父目录 → 转 BODY
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

## 设备端文件布局

```
filesDir/bin/               ← recvDir，推送根目录
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
LD_LIBRARY_PATH = filesDir/bin : <bundle libs 目录>
```

**仅搜索 `filesDir/bin/` 根目录**，不递归搜索子目录。

这意味着 `.so` 依赖必须直接推送到根层级：

```bash
binrunner push ./libfoo.so        # ✅ 动态链接可解析
binrunner push ./libs/libfoo.so   # ❌ 动态链接找不到
```

**例外**：通过绝对路径 `@/bin/libs/libfoo.so` 显式指定的 .so 不受此限制，但一般场景不涉及。

数据文件（模型、配置等）可自由使用子目录组织：

```bash
binrunner push ./my-models/
# → @/bin/my-models/net.ms
# → @/bin/my-models/vocab.bin
```

## name 长度限制

- 协议 `nameLen` 字段为 u32（理论上 4GB），但代码限制 ≤ 256 **字节**
- 限制的是 UTF-8 编码后的字节数，不是字符数
- 子路径增大了超限风险（如 `very-long-directory-name/very-long-filename.so`），但 256 字节对正常文件名绰绰有余

## 文件大小限制

单文件上限 **1GiB**（`MAX_FILE_SIZE`），双端校验：

| 位置 | 时机 | 行为 |
|---|---|---|
| CLI | `os.path.getsize` 后、打开文件前 | 超限即 `sys.exit`，不读取内容 |
| PushServer | 解析头部时 | 超限即 `FAILED`，不创建文件 |

协议 `payloadSize` 为 u64，此限制是**策略性**的 —— 用于挡住误操作（如误推整个镜像），
而非技术瓶颈。双端流式处理，内存占用与文件大小无关。

调整上限需同步改两处，否则一端放行另一端拒绝：
- `binrunner/config.py` 的 `MAX_FILE_SIZE`
- `PushServer.ets` 的 `MAX_FILE_SIZE`

### 流式传输参数

| 参数 | 值 | 位置 | 说明 |
|---|---|---|---|
| `PUSH_CHUNK_SIZE` | 256 KiB | config.py | 单次 `sendall` 大小，兼顾 syscall 次数与阻塞时长 |
| `PROGRESS_THRESHOLD` | 4 MiB | config.py | 超过则在 stderr 打印传输进度 |
| `MAX_INFLIGHT_BYTES` | 4 MiB | config.py | 在途未确认上限，超出则等 ACK（流控） |
| `RESUME_MIN_SIZE` | 4 MiB | config.py | 达到此大小才用 v2 协议协商续传 |
| `RESUME_PROBE_SIZE` | 4 KiB | 双端 | 头部探针长度，**必须双端一致** |
| `RESUME_MAX_ATTEMPTS` | 3 | config.py | 单次 push 内的自动重试次数 |
| `RESUME_BACKOFF` | 1.0 s | config.py | 重试前的退避间隔 |
| `_SEND_TIMEOUT` | 600 s | push.py | 发送超时，1GiB 传输可能持续数分钟 |

### 断点续传

≥ `RESUME_MIN_SIZE`（4 MiB）的文件自动启用 v2 协议。传输中断后重新执行同一条
`br push`，会从设备已落盘处继续，而非从头重传。

```bash
br push ./big-model.bin        # 传到 60% 时网线被拔 / Ctrl-C
br push ./big-model.bin        # 自动续传：「续传自 19.2 MiB」，只发剩余 40%
```

**同一文件的判定**靠三重条件，任一不符即从头重传（宁可多传，不可传错）：

| 条件 | 作用 |
|---|---|
| `.part` 存在 | 有可续的基础 |
| 头部 4 KiB 探针逐字节一致 | 确认是同一文件，而非同名的不同内容 |
| `.part` 长度 < `payloadSize` | 长度已达标说明是陈旧残留，不可信 |

单次 `br push` 内部还会**自动重试** `RESUME_MAX_ATTEMPTS`（3）次，退避间隔
`RESUME_BACKOFF`。每次重试都重新协商偏移，因此瞬时网络抖动对用户是透明的。

调整这些参数需注意双端一致性：`RESUME_PROBE_SIZE` 改动必须同步
`binrunner/config.py` 与 `PushServer.ets`，否则探针长度不匹配导致续传永远失败
（表现为「每次都从头传」，不报错但白费流量）。

### 残留清理

中断后 `.part` 文件会保留（这是续传的前提）。若确定不再需要：

```bash
br ls "@/bin"                  # .part 后缀的即为未完成传输
br rm big-model.bin.part
```

## 幂等性

- `hdc fport` 重复建立无害（`ensure_forward` 检测 `127.0.0.1:8888` 已通则跳过）
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

## 实现文件

| 文件 | 职责 |
|---|---|
| [binrunner/push.py](../binrunner/push.py) | CLI：`push_file`、`push_tree`、`_send_file`、v2 协商与重试 |
| [binrunner/config.py](../binrunner/config.py) | 协议常量、限额、续传参数 |
| [app/entry/src/main/ets/common/PushServer.ets](../app/entry/src/main/ets/common/PushServer.ets) | 设备端 TCP server，协议解析、续传判定与落盘 |
| [app/entry/src/main/ets/entryability/EntryAbility.ets](../app/entry/src/main/ets/entryability/EntryAbility.ets) | App 启动时初始化 `PushServer.recvDir` 并 start |
| [tests/test_push.py](../tests/test_push.py) | 协议编码、路径校验、流控、续传协商单测 |

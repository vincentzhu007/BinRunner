# 传输域词汇表

`br push` / `br pull` 与设备 PushServer 之间传输协议的常用术语。
协议细节见 [docs/transfer-spec.md](transfer-spec.md)，跨语言协议常量的取值见
[`protocol/protocol.json`](../protocol/protocol.json)；术语在代码与文档中统一使用。

| 术语 | 含义 | 备注/出处 |
|---|---|---|
| PushServer | App 内 `PORT` 端口的 TCP 服务，接收 push、响应 pull | `PushServer.ets` |
| 远端名 (remote name) | 设备侧目标相对路径，UTF-8 字节数 ≤ `MAX_REMOTE_NAME_BYTES` | `transfer.py` 的 `is_safe_remote_name` |
| fport 隧道 | `hdc fport` 把 PC 本地端口转发到设备 `PORT` | `hdc.py` 的 `ensure_forward` |
| 魔数 (magic) | 协议头首 u32，用于分流/识别版本 | v1 首字段 `nameLen`；v2 push `RESUME_MAGIC`、pull v2 `PULL_MAGIC_V2`、pull v1 `PULL_MAGIC` |
| payloadSize | push 头部：文件**完整**内容的字节数（u64） | 头部字段 |
| 分块 (chunk) | 客户端单次 `sendall` / 设备侧单次读发的 `TRANSFER_CHUNK_SIZE` 数据，**push 与 pull 共用** | `protocol/protocol.json` |
| 流式落盘 | 服务端头部到齐即 open，`message` 事件直接 writeSync | `RecvState` 状态机 |
| `.part` / `<local>.part` | 传输中间文件（push 在设备侧、pull 在本地）；收满才原子 rename 为正式名 | 中断产物供续传 |
| 探针 (probe) | 文件头部 `RESUME_PROBE_BYTES` 字节，用于确认 `.part` 属于同一文件 | v2 头附加字段 |
| resumeFrom | push 方向设备回传的已落盘可复用字节数（u64），0 = 从头 | 续传协商 |
| resumeAck | pull 方向设备回传的可复用字节数（u64）：0 = 从头整传，`offset == totalSize` 时 = `totalSize` 且无 payload | PULL v2 响应 |
| ACK | 设备每落盘 `ACK_INTERVAL` 回一个 u64 累计已写字节 | `protocol/protocol.json` |
| 在途上限 | 已发送未确认的字节上限（8 MiB，2 × `ACK_INTERVAL`），超限等待 ACK | `MAX_INFLIGHT_BYTES`（config.py） |
| 续传 (resume) | 断线后从已落盘偏移继续发送/接收剩余 payload | `FLAG_RESUME`、`pull.py` |
| ENOSPC 兜底 | 设备空间不足时写盘失败 → `fail()` 清理 fd 与 `.part` | `PushServer.ets` |
| MAX_FILE_SIZE | 单文件策略上限（见 `protocol/protocol.json`），双端常量一致 | ADR-0001 |
| `br pull` | 设备 → PC 的文件拉取，复用 `PORT` 与 pull 魔数分流 | `pull.py` |
| `DIAG ` | native/ArkTS 诊断日志行的前缀标记，解析时剔除，避免混入目标程序 stdout | `binrunner/hilog.py` |

## 易混与措辞约定

- **探针 vs `probe` / `probe2`**：上表的**探针 (probe)** 是续传用的文件头字节；
  `probe` / `probe2` 是 README「调试命令」里的两个**诊断命令**（枚举关键目录、
  探测 exec-mmap 可行性）。同名不同义 —— 文档里前者一律写「探针」，
  后者一律写「诊断命令 `probe` / `probe2`」。
- **推送目录**：代码里是 `filesDir`（ArkTS / native）与 `recvDir`（PushServer），
  路径上是 `filesDir`，早期文档叫「推送区」。**文档统一写「推送目录（`filesDir`）」**，
  v1.5.0 起推送目录就是 files 根目录，不再隐式附加 bin/。
- **「设备已结束」是 CLI 的推断，不是设备事件**：实际判据是「本轮 hilog 中出现了**汇总行**」
  （`runner.device_finished`）。用户可见的报错文案仍保留「设备已结束，缺失部分无法通过 hilog
  补齐」—— 它比「已收到汇总行」更易懂，且推断可靠（汇总行只在执行结束时发出）。
  内部文档与注释不要把它描述成设备主动上报的状态。

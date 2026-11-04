"""跨语言协议常量 —— 生成文件，请勿手改。

来源：protocol/protocol.json（唯一来源）
重新生成：python3 scripts/gen_protocol.py
手改会被 python3 scripts/gen_protocol.py --check 与 tests/test_protocol.py 挡住。

host-only 策略常量（超时、退避、进度阈值等）在 binrunner/config.py。
"""
from __future__ import annotations

# 设备侧 PushServer 监听端口，也是 hdc fport 的转发端口
PORT = 8888

# 双端传输分块 256KiB：客户端单次 sendall / 设备侧单次读发（push 与 pull 共用）
TRANSFER_CHUNK_SIZE = 262144

# 设备每落盘这么多字节回一个 u64 累计 ACK；客户端据此限制在途字节
ACK_INTERVAL = 4194304

# 单文件策略上限 4GiB，双端一致（见 docs/adr/0001-single-file-size-cap-4gib.md）
MAX_FILE_SIZE = 4294967296

# 远端名（UTF-8 字节数）上限；双端路径校验规则必须一致
MAX_REMOTE_NAME_BYTES = 256

# v2 续传协议首 u32（"BRN1"）；> 256 故不与 v1 的 nameLen 值域混淆
RESUME_MAGIC = 0x42524E31

# v2 头部 flags 位：客户端愿意从设备已有偏移继续
FLAG_RESUME = 1

# 续传探针长度（文件头部 64KiB），用于确认 .part 与源文件同源
RESUME_PROBE_BYTES = 65536

# PULL v1 请求首 u32（线上字节 "PULL"）；与 push 的 nameLen 值域不重叠，设备据此分流
PULL_MAGIC = 0x4C4C5550

# PULL v2（带续传探针）请求首 u32（线上字节 "PUL2"）。另起魔数而非复用 PULL_MAGIC 加字段：旧设备会把新增的 flags 误读成 nameLen 从而读到错误路径
PULL_MAGIC_V2 = 0x324C5550

# v1 头部最小长度：u32 nameLen + u64 payloadSize
MIN_HEADER_BYTES = 12

# v2 头部最小长度：magic + flags + nameLen + u64 size
MIN_HEADER_V2_BYTES = 20

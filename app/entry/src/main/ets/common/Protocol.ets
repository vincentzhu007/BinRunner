/**
 * 跨语言协议常量 —— 生成文件，DO NOT EDIT。
 *
 * 来源：protocol/protocol.json（唯一来源）
 * 重新生成：python3 scripts/gen_protocol.py
 * 手改会被 Python 侧 parity 测试挡住。
 */

/** 设备侧 PushServer 监听端口，也是 hdc fport 的转发端口 */
export const PORT: number = 8888;

/** 双端传输分块 256KiB：客户端单次 sendall / 设备侧单次读发（push 与 pull 共用） */
export const TRANSFER_CHUNK_SIZE: number = 262144;

/** 设备每落盘这么多字节回一个 u64 累计 ACK；客户端据此限制在途字节 */
export const ACK_INTERVAL: number = 4194304;

/** 单文件策略上限 4GiB，双端一致（见 docs/adr/0001-single-file-size-cap-4gib.md） */
export const MAX_FILE_SIZE: number = 4294967296;

/** 远端名（UTF-8 字节数）上限；双端路径校验规则必须一致 */
export const MAX_REMOTE_NAME_BYTES: number = 256;

/** v2 续传协议首 u32（"BRN1"）；> 256 故不与 v1 的 nameLen 值域混淆 */
export const RESUME_MAGIC: number = 0x42524E31;

/** v2 头部 flags 位：客户端愿意从设备已有偏移继续 */
export const FLAG_RESUME: number = 1;

/** 续传探针长度（文件头部 64KiB），用于确认 .part 与源文件同源 */
export const RESUME_PROBE_BYTES: number = 65536;

/** PULL v1 请求首 u32（线上字节 "PULL"）；与 push 的 nameLen 值域不重叠，设备据此分流 */
export const PULL_MAGIC: number = 0x4C4C5550;

/** PULL v2（带续传探针）请求首 u32（线上字节 "PUL2"）。另起魔数而非复用 PULL_MAGIC 加字段：旧设备会把新增的 flags 误读成 nameLen 从而读到错误路径 */
export const PULL_MAGIC_V2: number = 0x324C5550;

/** v1 头部最小长度：u32 nameLen + u64 payloadSize */
export const MIN_HEADER_BYTES: number = 12;

/** v2 头部最小长度：magic + flags + nameLen + u64 size */
export const MIN_HEADER_V2_BYTES: number = 20;

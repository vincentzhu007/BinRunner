# ADR-0001: 单文件推送上限放宽到 4GiB

- 状态：已接受（2026-09-08）
- 关联：issue #5「无法传输超过1GB的文件」

## 背景

`br push` / `br pull` 使用 u64 承载文件长度，双端均为流式处理（256KiB 分块发送、
设备端头部到齐即落盘），内存占用与文件大小无关。代码里的 1GiB 上限
（`MAX_FILE_SIZE`）是**策略性护栏**，用于挡住误操作（如误推整个镜像），并非
技术瓶颈。issue #5 反馈实际需要传输 1GiB 以上的文件，而 CLI 在
`validate_remote`（`push.py`）发送前即以「文件过大…上限 1 GiB」拒绝，传输根本没开始。

## 决策

- 双端上限由 1GiB 提高到 **4GiB（`4 * 2^30` 字节，含等值）**；常量定义在
  `protocol/protocol.json` 的 `MAX_FILE_SIZE`，生成到 `binrunner/protocol.py` 与
  `app/entry/src/main/ets/common/Protocol.ets`。
- 拒绝语义不变：`size > MAX_FILE_SIZE` 才拒绝；恰好 4GiB 放行。
- 其余协议、流控、断点续传逻辑**不变**（`payloadSize`/ACK/偏移均为 u64，
  4GiB 远小于 ArkTS `Number` 精确整数上限 2^53，无溢出风险）。

## 为何不做别的

- 去掉上限改为「按设备剩余空间放行」需要新增 statfs 预检协议，改动面大；
  当前 4GiB 已覆盖报障场景，ENOSPC 场景由现有失败路径兜底。
- 不改成可配置参数：护栏仍应存在，且双端常量需严格一致，参数化会放大
  「一端放行另一端拒绝」的错配面。

## 后果

- 正面：>1GiB ≤4GiB 的文件可直接推送。
- 代价/风险：
  - 设备剩余空间 < 文件大小时，传输会在写盘中 ENOSPC 失败（现有 catch → `fail()`
    清理 fd 并删除 `.part`），用户看到明确报错；无预检。
  - 该上限由 `protocol/protocol.json` 单点定义、生成到双端，改 JSON 后重新生成即可；改动后 `TestSizeLimit`
    基于常量做边界断言，自动跟随。
- 未来若需更大文件：先做设备侧剩余空间预检，再放开上限或改为动态判断。

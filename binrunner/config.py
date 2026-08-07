"""集中配置常量。

无依赖叶子模块：其余模块都可安全导入，不会形成循环依赖。
"""

# 设备侧 App 标识（与 app/AppScope/app.json5 一致）
BUNDLE = "com.example.binrunner"
ABILITY = "EntryAbility"

# hilog tag，与设备侧 BinRunner.ets 的 TAG 一致
TAG = "BinRunner"

# PushServer 监听端口（设备侧 PushServer.ets 硬编码同值）
DEFAULT_PORT = 8888

# 指定目标设备的环境变量（优先级低于 -t 参数）
DEVICE_ENV = "BINRUNNER_DEVICE"

# hdc 不在 PATH 时的回退路径（DevEco Studio 默认安装位置，仅 macOS）
DEVECO_HDC = "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"

# 远端文件名上限（字节数，与 PushServer.ets 的 nameLen 校验一致）
MAX_REMOTE_NAME_BYTES = 256

# 单文件推送上限 1GiB。双端均为流式处理不占大内存，此限制用于挡住误操作
# （如误推整个镜像），需与 PushServer.ets 的 MAX_FILE_SIZE 保持一致。
MAX_FILE_SIZE = 1 << 30

# 流式发送的分块大小。256KiB 兼顾 syscall 次数与内存占用：
# 设备侧 TCP 接收缓冲有限，单次 send 过大会长时间阻塞。
PUSH_CHUNK_SIZE = 256 * 1024

# 超过此大小才打印传输进度（小文件无需刷屏）
PROGRESS_THRESHOLD = 4 * 1024 * 1024

# PULL 协议魔数（小端），与 PushServer.ets 的 PULL_MAGIC 一致
PULL_MAGIC = 0x4C4C5550

# 设备每落盘这么多字节回一个 u64 ACK，需与 PushServer.ets 的 ACK_INTERVAL 一致。
ACK_INTERVAL = 4 * 1024 * 1024

# 允许的在途（已发送但未被 ACK 确认）字节上限。
#
# 流控存在的原因：设备侧是 ArkTS 单线程，主线程被同步 IO 阻塞时
# PushServer 的 message 回调停摆。若客户端无视这点全速发送，内核接收
# 缓冲区填满后连接被重置（实测 64MB 文件在 33MB 处 Broken pipe）。
# 上限取 2 个 ACK 区间：足够填满链路，又不会在设备卡顿时积压过多。
MAX_INFLIGHT_BYTES = 2 * ACK_INTERVAL

# 等待 ACK 的超时。设备侧主线程可能被 runBin 等同步操作占用较久，
# 给足余量，避免正常卡顿被误判为断连。
ACK_TIMEOUT = 60.0

# ---- 断点续传 ----
#
# 大文件在 fport 隧道抖动时可能中断（实测隧道在连续大传输间偶发积压）。
# 无续传时整个文件必须重传；有续传则只补缺口。
#
# 协议协商：头部 flags 位标记客户端支持续传，设备回 u64 已有字节数。
# 旧版设备不认 flags，会把它当作 nameLen 的高位 —— 故 flags 放在
# 独立的 u32 而非复用现有字段，且由 magic 前缀保证不被误读。

# 续传协议魔数（小端 u32）。旧协议头首字段是 nameLen（1..256），
# 取一个远超该范围的值，设备可据此判断对端是否为新协议。
RESUME_MAGIC = 0x42524E31  # "BRN1"

# 头部 flags 位
FLAG_RESUME = 1 << 0  # 客户端愿意从设备已有偏移继续

# 续传前的完整性校验：客户端把文件头这么多字节作为探针随头部发出，
# 设备与已有 .part 的同位置逐字节比对，不一致就从头重传。
#
# 只比头部而非全文，因为设备侧读全文会长时间阻塞主线程（正是流控要
# 规避的问题）。头部足以挡住"同名不同内容"—— 重新编译的二进制其
# ELF 头、段表、构建 ID 必变，落在这个区间内。
RESUME_PROBE_BYTES = 64 * 1024

# 断点续传的最小文件大小。小文件重传成本低于协商往返，不值得。
RESUME_MIN_SIZE = PROGRESS_THRESHOLD

# 单个文件的最大尝试次数（首传 + 续传重试）。
# 每次尝试都从设备已确认的偏移继续，故失败也在推进；
# 上限用于挡住"每次只前进几字节"的病态链路，避免无限重试。
RESUME_MAX_ATTEMPTS = 5

# 续传重试前的退避基数（秒）。第 n 次重试等 RESUME_BACKOFF * n ——
# fport 隧道积压是瞬态的，线性退避足够，无需指数级等待。
RESUME_BACKOFF = 1.0

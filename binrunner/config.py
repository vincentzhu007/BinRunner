"""主机侧集中配置常量。

只放**主机侧策略**：设备标识、host 专用超时/退避/进度阈值，以及从协议常量派生的值。
协议常量（双端必须逐字节一致的值：魔数、分块、上限、探针长度…）在
`binrunner/protocol.py` —— 由 `protocol/protocol.json` 生成，见 `scripts/gen_protocol.py`。
需要改协议常量的地方不要改本文件，改 JSON 后重新生成。

无依赖叶子模块（只依赖 protocol）：其余模块都可安全导入，不会形成循环依赖。
"""
from __future__ import annotations

from binrunner.protocol import ACK_INTERVAL

# 设备侧 App 标识（与 app/AppScope/app.json5 一致）
BUNDLE = "com.example.binrunner"
ABILITY = "EntryAbility"

# hilog tag，与设备侧 BinRunner.ets 的 TAG 一致
TAG = "BinRunner"

# 指定目标设备的环境变量（优先级低于 -t 参数）
DEVICE_ENV = "BINRUNNER_DEVICE"

# hdc 不在 PATH 时的回退路径（DevEco Studio 默认安装位置，仅 macOS）
DEVECO_HDC = "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"

# 超过此大小才打印传输进度（小文件无需刷屏）
PROGRESS_THRESHOLD = 4 * 1024 * 1024

# 允许的在途（已发送但未被 ACK 确认）字节上限。
#
# 流控存在的原因：设备侧是 ArkTS 单线程，主线程被同步 IO 阻塞时
# PushServer 的 message 回调停摆。若客户端无视这点全速发送，内核接收
# 缓冲区填满后连接被重置（实测 64MB 文件在 33MB 处 Broken pipe）。
#
# 上限取 2 个 ACK 区间：足够填满链路，又不会在设备卡顿时积压过多。
MAX_INFLIGHT_BYTES = 2 * ACK_INTERVAL

# 等待 ACK 的超时。设备侧主线程可能被 runBin 等同步操作占用较久，
# 给足余量，避免正常卡顿被误判为断连。
ACK_TIMEOUT = 60.0

# 续传协商（pull v2）的响应超时，单独收紧而**不能**沿用 ACK_TIMEOUT。
#
# 协商本身只是 statSync + 读文件头 + 16 字节回包，正常在毫秒级。但旧 HAP 不认
# PULL_MAGIC_V2 时既不应答也**不关闭连接**（它的 fail() 只关 fd，message 回调
# 之后静默丢弃一切），只能靠超时判定「不支持续传」。若用 ACK_TIMEOUT，每次带
# `.part` 的 pull 都会白等 60 秒 —— 真机实测 61 秒。
PULL_NEGOTIATE_TIMEOUT = 3.0

# ---- 断点续传（策略侧；协议字段在 protocol.py）----
#
# 大文件在 fport 隧道抖动时可能中断（实测隧道在连续大传输间偶发积压）。
# 无续传时整个文件必须重传；有续传则只补缺口。

# 断点续传的最小文件大小。小文件重传成本低于协商往返，不值得。
RESUME_MIN_SIZE = PROGRESS_THRESHOLD

# 单个文件的最大尝试次数（首传 + 续传重试）。
# 每次尝试都从设备已确认的偏移继续，故失败也在推进；
# 上限用于挡住"每次只前进几字节"的病态链路，避免无限重试。
RESUME_MAX_ATTEMPTS = 5

# 续传重试前的退避基数（秒）。第 n 次重试等 RESUME_BACKOFF * n ——
# fport 隧道积压是瞬态的，线性退避足够，无需指数级等待。
RESUME_BACKOFF = 1.0

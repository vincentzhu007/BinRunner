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

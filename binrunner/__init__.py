"""BinRunner —— 在非 root 鸿蒙手机（HarmonyOS NEXT 零售版）上执行原生二进制。

版本号唯一来源：pyproject.toml 经 [tool.setuptools.dynamic] 读取此处的 __version__，
避免两处手改不同步。

本模块不导入子模块：provision 等需要 `from binrunner import __version__`，
若此处再反向导入会形成循环依赖。
"""

__version__ = "1.1.1"

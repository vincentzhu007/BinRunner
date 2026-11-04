"""传输层公共件：TCP 建连、u64 编解码、远端名校验、尺寸格式化。

push（PC→设备）与 pull（设备→PC）共用这些部件。协议常量（端口、分块、上限、
魔数、探针长度）的唯一来源是 `protocol/protocol.json`（生成到
`binrunner/protocol.py`）；本模块只提供**机制**，策略（重试、保活、进度、
流控阈值）在各自模块与 `binrunner/config.py`。

**路径校验规则必须与设备侧 PushServer.ets 的 isSafeName 逐条一致** ——
远端名会被拼进设备侧 recvDir，双端任一放行都会造成沙箱逃逸。
"""
from __future__ import annotations

import socket
import struct
import sys

from binrunner.config import ACK_TIMEOUT
from binrunner.protocol import MAX_REMOTE_NAME_BYTES, PORT

# 建连超时：只约束 TCP 握手（hdc fport 未就绪时快速失败）
_CONNECT_TIMEOUT = 10

# 协议里所有长度/偏移/ACK 都是小端 u64。单点定义，避免两端字节序写反。
_U64 = struct.Struct("<Q")

# 「socket 超时」异常元组。
#
# 3.10 起 socket.timeout 就是 TimeoutError 的别名，但 3.9 里它是**独立的 OSError
# 子类** —— 只写 `except TimeoutError` 在 3.9 上抓不到真实的 socket 超时（实测：
# socket.timeout is TimeoutError 在 3.9 为 False、3.12 为 True）。CI 的 3.9 矩阵
# 会挂在这里。单点定义，push 的 ACK 等待与 pull 的协商窗口共用。
SOCKET_TIMEOUT = (TimeoutError, socket.timeout)


def fmt_size(n: float) -> str:
    """人类可读的字节数。"""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GiB"


def pack_u64(value: int) -> bytes:
    """打包小端 u64。"""
    return _U64.pack(value)


def unpack_u64(buf: bytes) -> int:
    """解包小端 u64。"""
    return _U64.unpack(buf)[0]


def open_connection(port: int = PORT) -> socket.socket:
    """建立到设备 PushServer 的连接（经 hdc fport 转发到 127.0.0.1:port）。

    返回的 socket 已设好收发超时 —— 同一超时同时约束 send 与收 ACK：设备卡顿时
    两者都可能挂住。调用方负责关闭（一律用 `with`）。
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=_CONNECT_TIMEOUT)
    sock.settimeout(ACK_TIMEOUT)
    return sock


def recv_exactly(sock, n: int, what: str) -> bytes:
    """精确读取 n 字节。

    EOF 抛 OSError（等价于对端提前关闭）；socket 超时按 TimeoutError 抛出，
    由调用方决定是重试还是终止。
    """
    buf = b""
    while len(buf) < n:
        data = sock.recv(n - len(buf))
        if not data:
            raise OSError(f"设备在{what}前关闭了连接")
        buf += data
    return buf


def is_safe_remote_name(name: str) -> bool:
    """校验远端名，拒绝目录穿越。允许子路径（如 models/net.ms）。

    与 PushServer.ets 的 isSafeName 规则一致。
    """
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    return all(part not in ("", ".", "..", "@") for part in name.split("/"))


def resolve_remote_name(name: str) -> str:
    """将用户路径解析成相对 files/ 的规范协议路径；不隐式添加 bin/。"""
    original = name
    if name.startswith("@/"):
        name = name[2:]
    if name.startswith("/") or "\\" in name or "\x00" in name or ".." in name.split("/"):
        sys.exit(f"非法远端名: {original!r}")
    name = "/".join(part for part in name.split("/") if part not in ("", "."))
    validate_remote_name(name)
    return name


def validate_remote_name(name: str) -> None:
    """校验远端名（长度 + 安全），非法即终止。

    长度按 UTF-8 **字节数**算：设备侧 nameLen 字段是字节而非字符，
    多字节名（如中文）很容易超限。
    """
    if len(name.encode()) > MAX_REMOTE_NAME_BYTES:
        sys.exit(f"远端名过长（>{MAX_REMOTE_NAME_BYTES} 字节）: {name!r}")
    if not is_safe_remote_name(name):
        sys.exit(f"非法远端名: {name!r}")

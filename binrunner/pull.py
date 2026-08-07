"""文件回传：从设备拉取文件到 PC。

协议（复用 PushServer 8888 端口）：
    PC → 设备:  u32 PULL_MAGIC | u32 nameLen | name | u64 offset | u64 size
    设备 → PC:  u64 totalSize | 文件内容分块流

totalSize = 0 表示文件不存在或读取失败；> 0 后跟实际字节数的文件内容。
offset/size 用于续传/部分读取，0/0 表示从头读取整个文件。
"""
from __future__ import annotations

import os
import socket
import struct
import sys

from binrunner.config import (
    DEFAULT_PORT,
    MAX_REMOTE_NAME_BYTES,
    PULL_MAGIC,
)
from binrunner.hdc import ensure_forward, run_hdc
from binrunner.push import is_safe_remote_name

# 分块大小：与设备侧 PULL_CHUNK 一致
_PULL_CHUNK = 256 * 1024

# 大文件显示进度的阈值
_PROGRESS_THRESHOLD = 4 * 1024 * 1024


def encode_pull_request(name: str, offset: int = 0, size: int = 0) -> bytes:
    """封装 PULL 请求头。"""
    name_bytes = name.encode()
    return (
        struct.pack("<I", PULL_MAGIC)
        + struct.pack("<I", len(name_bytes))
        + name_bytes
        + struct.pack("<Q", offset)
        + struct.pack("<Q", size)
    )


def _fmt_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GiB"


def _read_exactly(sock, n: int, what: str) -> bytes:
    """从 socket 精确读取 n 字节，EOF/超时即报错。"""
    buf = b""
    while len(buf) < n:
        data = sock.recv(n - len(buf))
        if not data:
            raise OSError(f"设备在{what}前关闭了连接")
        buf += data
    return buf


def _pull_file(port: int, name: str, local_path: str,
               offset: int, req_size: int, udid: str) -> None:
    """从设备拉取单个文件并写入本地。"""
    if not is_safe_remote_name(name):
        sys.exit(f"非法远端名: {name!r}")

    request = encode_pull_request(name, offset, req_size)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.settimeout(120)
            s.sendall(request)

            # 读响应头
            total_size = struct.unpack("<Q", _read_exactly(s, 8, "响应头"))[0]
            if total_size == 0:
                sys.exit(f"设备上不存在: {name}")

            show_progress = total_size >= _PROGRESS_THRESHOLD
            written = 0
            with open(local_path, "wb") as f:
                remaining = total_size
                while remaining > 0:
                    chunk = s.recv(min(_PULL_CHUNK, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
                    if show_progress:
                        pct = written * 100 // total_size
                        print(f"\r  {name}: {pct}% ({_fmt_size(written)}/{_fmt_size(total_size)})",
                              end="", file=sys.stderr, flush=True)
                if show_progress:
                    print(file=sys.stderr)

            if written != total_size:
                sys.exit(f"传输不完整: {name}（收到 {written}，声明 {total_size}）")

            print(f"OK: {name} ({total_size} bytes) -> {local_path}")

    except OSError as e:
        sys.exit(f"拉取失败: {e}")


def cmd_pull(udid: str, remote: str, local: str | None,
             port: int = DEFAULT_PORT) -> int:
    """br pull <远端名> [本地路径]。

    remote 为设备侧文件名或相对路径，默认基于 filesDir/bin/。
    local 为空时取 remote 的文件名部分存到当前目录。
    """
    ensure_forward(udid, port)
    if local is None:
        local = os.path.basename(remote.rstrip("/") or ".")
    if os.path.isdir(local):
        local = os.path.join(local, os.path.basename(remote.rstrip("/") or "."))

    _pull_file(port, remote, local, 0, 0, udid)
    return 0

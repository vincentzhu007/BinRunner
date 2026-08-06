"""文件推送：TCP 协议封包与目录递归。

依赖：config, hdc。

协议（单连接单文件，小端）：
    u32 nameLen | name (UTF-8) | u64 payloadSize | payload

设备侧实现见 app/entry/src/main/ets/common/PushServer.ets。
路径校验规则必须与设备侧保持一致 —— 双端任一放行都会造成沙箱逃逸。
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import time

from binrunner.config import ABILITY, BUNDLE, MAX_REMOTE_NAME_BYTES
from binrunner.hdc import ensure_forward, run_hdc

# 连接与重试参数
_CONNECT_TIMEOUT = 10
_APP_LAUNCH_WAIT = 2


def is_safe_remote_name(name: str) -> bool:
    """校验远端名，拒绝目录穿越。允许子路径（如 models/net.ms）。

    与 PushServer.ets 的校验规则一致。
    """
    if not name:
        return False
    if name.startswith("/") or "\\" in name:
        return False
    if name in (".", ".."):
        return False
    if "/../" in name or name.endswith("/.."):
        return False
    return True


def encode_packet(name: str, payload: bytes) -> bytes:
    """封包：u32 nameLen | name | u64 payloadSize | payload（小端）。"""
    name_bytes = name.encode()
    return (
        struct.pack("<I", len(name_bytes))
        + name_bytes
        + struct.pack("<Q", len(payload))
        + payload
    )


def _send_file(port: int, name: str, payload: bytes, udid: str) -> None:
    """发送单个文件到 PushServer。首连失败时拉起 App 重试一次。"""
    if len(name.encode()) > MAX_REMOTE_NAME_BYTES:
        sys.exit(f"远端名过长（>{MAX_REMOTE_NAME_BYTES} 字节）: {name!r}")
    if not is_safe_remote_name(name):
        sys.exit(f"非法远端名: {name!r}")

    packet = encode_packet(name, payload)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=_CONNECT_TIMEOUT) as s:
            s.sendall(packet)
    except OSError as e:
        # 转发通了但 App 没监听（被系统杀掉等）→ 拉起 App 重试一次
        run_hdc(udid, "shell", f"aa start -b {BUNDLE} -a {ABILITY}", check=False)
        time.sleep(_APP_LAUNCH_WAIT)
        try:
            with socket.create_connection(
                ("127.0.0.1", port), timeout=_CONNECT_TIMEOUT
            ) as s:
                s.sendall(packet)
        except OSError:
            sys.exit(
                f"推送失败：{e}\n（已尝试拉起 App；若仍失败，检查 App 是否安装/被杀）"
            )
    print(f"OK: {name} ({len(payload)} bytes)")


def push_file(udid: str, local: str, remote: str, port: int) -> None:
    """推送单个文件到 filesDir/bin/<remote>。"""
    ensure_forward(udid, port)
    with open(local, "rb") as f:
        payload = f.read()
    _send_file(port, remote, payload, udid)


def collect_tree(local_dir: str) -> list[tuple[str, str]]:
    """遍历目录，返回 [(本地绝对路径, 远端相对路径)]。

    相对路径以 local_dir 为基准，故子目录结构得以保持。
    """
    base = os.path.normpath(local_dir)
    files = []
    for root, _dirs, filenames in os.walk(base):
        for fn in filenames:
            full = os.path.join(root, fn)
            files.append((full, os.path.relpath(full, base)))
    return files


def push_tree(udid: str, local_dir: str, port: int) -> None:
    """递归推送目录树到 filesDir/bin/，保持子目录结构。"""
    ensure_forward(udid, port)
    files = collect_tree(local_dir)
    if not files:
        print(f"目录为空: {local_dir}")
        return
    print(f"推送 {len(files)} 个文件...")
    for full, rel in files:
        with open(full, "rb") as f:
            payload = f.read()
        _send_file(port, rel, payload, udid)

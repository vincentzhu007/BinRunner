"""文件回传：从设备拉取文件到 PC。

依赖：config, hdc, keepalive, protocol, transfer。

协议（复用 PushServer 8888 端口，首 u32 分流）：

v1（无续传）：
    PC → 设备:  u32 PULL_MAGIC | u32 nameLen | name | u64 offset | u64 size
    设备 → PC:  u64 totalSize | 文件内容分块流

v2（续传）：
    PC → 设备:  u32 PULL_MAGIC_V2 | u32 nameLen | name | u64 offset | u64 size
                | u32 probeLen | probe
    设备 → PC:  u64 resumeAck | u64 totalSize | payload[resumeAck .. totalSize)

- `offset` = 本地 `.part` 的字节数（想从哪续），0 = 从头。
- `probe` = 本地 `.part` 的头部，设备拿它与**源文件**头部逐字节比对来确认是同一个
  文件 —— 与 push 的续传判据同构。只按长度续传会在「同名不同内容」时静默拼出坏
  文件，所以宁可多传。
- `totalSize` = 0 表示文件不存在或读取失败。
- `resumeAck` = 设备允许复用的字节数；0 表示必须从头传。

v2 另起一个魔数，而不是在 v1 后面追加字段：旧设备会把新增字段误读成 nameLen，
进而去读一个**错误的路径**。新 CLI 遇到不认 v2 的旧 HAP 时降级为 v1 整传
（见 `_PULL_V2_OK`）。

落盘用 `<local>.part`，收满才原子 rename 为正式名 —— 正式名下永不出现半成品，
与 push 同一约定。中断的 `.part` 保留下来供下次续传。
"""
from __future__ import annotations

import os
import struct
import sys
import time

from binrunner.config import (
    ABILITY,
    ACK_TIMEOUT,
    BUNDLE,
    PROGRESS_THRESHOLD,
    PULL_NEGOTIATE_TIMEOUT,
    RESUME_BACKOFF,
    RESUME_MAX_ATTEMPTS,
)
from binrunner.hdc import ensure_forward, restart_app, wakeup_screen
from binrunner.keepalive import KeepAlive
from binrunner.protocol import (
    PORT,
    PULL_MAGIC,
    PULL_MAGIC_V2,
    RESUME_PROBE_BYTES,
    TRANSFER_CHUNK_SIZE,
)
from binrunner.transfer import (
    fmt_size,
    open_connection,
    pack_u64,
    recv_exactly,
    SOCKET_TIMEOUT,
    unpack_u64,
    resolve_remote_name,
)

# 首连失败后拉起 App 再试一次的等待时间（与 push 一致）
_APP_LAUNCH_WAIT = 2

# 设备是否支持 PULL v2（带续传探针）。旧 HAP 会把 PULL_MAGIC_V2 当成 push 的
# nameLen 而拒收（连接被关、没有任何响应），此时降级为 v1 整传，避免每次 pull
# 都白等一轮协商。与 runner.py 的 _FILTERS_OK 同构。
_PULL_V2_OK = True


class _PullV2Unsupported(Exception):
    """设备不认 PULL_MAGIC_V2：需要降级为 v1 整传。"""


class _IncompleteTransfer(OSError):
    """连接在收满前断开：`.part` 保留已落盘部分，下次续传。"""


def encode_pull_request(name: str, offset: int = 0, size: int = 0,
                        probe: bytes = b"") -> bytes:
    """封装 PULL 请求头。

    带探针即 v2（续传），否则 v1 —— 与 push 按 probe 是否存在切换版本一致。
    """
    name_bytes = name.encode()
    head = (
        struct.pack("<I", PULL_MAGIC_V2 if probe else PULL_MAGIC)
        + struct.pack("<I", len(name_bytes))
        + name_bytes
        + pack_u64(offset)
        + pack_u64(size)
    )
    if probe:
        head += struct.pack("<I", len(probe)) + probe
    return head


def _local_target(remote: str, local: str | None) -> str:
    """决定本地落盘路径：remote 的文件名部分，或 local（目录则取其下同名）。"""
    name = os.path.basename(remote.rstrip("/")) or "."
    if local is None:
        return name
    return os.path.join(local, name) if os.path.isdir(local) else local


def _part_path(target: str) -> str:
    """传输中间文件名。收满才 rename，正式名下永不出现半成品。"""
    return target + ".part"


def _read_probe_head(part: str) -> bytes:
    """读本地 `.part` 的头部作为续传探针。

    只比头部而非全文：设备侧读全文会长时间阻塞主线程（正是 push 流控要规避的
    问题），而头部足以挡住「同名不同内容」—— 重新构建的二进制其 ELF 头、
    段表、构建 ID 必变。
    """
    with open(part, "rb") as f:
        return f.read(RESUME_PROBE_BYTES)


def _recv_payload(sock, name: str, part: str, start: int, total: int) -> None:
    """把 `total - start` 字节流式写入 `.part`，收满即返回。

    未收满抛 `_IncompleteTransfer`：`.part` 保留已落盘部分，下次从断点续传。
    """
    show_progress = total >= PROGRESS_THRESHOLD
    written = start
    remaining = total - start
    # start > 0 说明续传，保留已有前缀；否则截断重来
    with open(part, "ab" if start > 0 else "wb") as f:
        while remaining > 0:
            chunk = sock.recv(min(TRANSFER_CHUNK_SIZE, remaining))
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
            if show_progress:
                pct = written * 100 // total if total else 100
                print(f"\r  {name}: {pct}% ({fmt_size(written)}/{fmt_size(total)})",
                      end="", file=sys.stderr, flush=True)
    if show_progress:
        print(file=sys.stderr)
    if written != total:
        raise _IncompleteTransfer(
            f"{name} 传输不完整（已落盘 {fmt_size(written)}/{fmt_size(total)}）"
        )


def _pull_once(port: int, name: str, target: str) -> None:
    """一次协商 + 传输，收满后原子改名。中断时 `.part` 保留供下次续传。"""
    global _PULL_V2_OK

    part = _part_path(target)
    local_size = os.path.getsize(part) if os.path.exists(part) else 0
    # 有本地残片才值得协商续传：哪怕只省一次往返也比整传划算
    probe = _read_probe_head(part) if (_PULL_V2_OK and local_size > 0) else b""
    start = local_size if probe else 0

    with open_connection(port) as s:
        s.sendall(encode_pull_request(name, start, 0, probe))

        if probe:
            # 协商窗口单独收紧：旧 HAP 不认 PULL_MAGIC_V2 时不应答也不断连，
            # 只能靠超时判定「不支持续传」（见 config.PULL_NEGOTIATE_TIMEOUT）。
            s.settimeout(PULL_NEGOTIATE_TIMEOUT)
            try:
                resume_ack = unpack_u64(recv_exactly(s, 8, "续传协商"))
            except SOCKET_TIMEOUT:
                _PULL_V2_OK = False
                raise _PullV2Unsupported(
                    f"{PULL_NEGOTIATE_TIMEOUT:.0f}s 内无响应") from None
            except OSError as e:
                # 对端在协商前就断开（App 崩溃，或旧 HAP 的另一种表现）
                _PULL_V2_OK = False
                raise _PullV2Unsupported(str(e)) from None
            finally:
                s.settimeout(ACK_TIMEOUT)  # 后续 payload 用常规超时
            if resume_ack != start:
                start = 0  # 探针不符或设备要求重传，本地残片不可信
            elif start:
                print(f"  {name}: 续传自 {fmt_size(start)}", file=sys.stderr)

        total = unpack_u64(recv_exactly(s, 8, "响应头"))
        if total == 0:
            sys.exit(f"设备上不存在: {name}")
        if start > total:
            start = 0  # 远端文件变小了，本地残片比它还长
        _recv_payload(s, name, part, start, total)

    os.replace(part, target)  # 原子改名：正式名下永不出现半成品


def _pull_with_retry(udid: str, port: int, name: str, target: str) -> None:
    """拉取单个文件，中断则续传；与 push 的重试/自愈策略同构。

    每次重试都重新协商偏移（`_pull_once` 每轮重新读 `.part` 长度），故即使连续
    中断也在推进；`RESUME_MAX_ATTEMPTS` 挡住「每次只前进几字节」的病态链路。
    """
    launched = False
    last_reason = ""

    for attempt in range(1, RESUME_MAX_ATTEMPTS + 1):
        try:
            _pull_once(port, name, target)
            print(f"OK: {name} ({os.path.getsize(target)} bytes) -> {target}")
            return
        except _PullV2Unsupported as e:
            # 已降级为 v1：立即重试，不计入退避
            print(f"  {name}: 设备不支持续传协商（{e}），退回整传", file=sys.stderr)
            continue
        except OSError as e:
            last_reason = str(e)
            # 自愈组合拳：重建 fport（隧道可能被回收）+ 点亮屏幕（避免 App 进后台
            # 挂起监听）。首连失败才重启 App，后续重试保留续传状态不打断。
            ensure_forward(udid, port, force=True)
            wakeup_screen(udid)
            if not launched:
                restart_app(udid, BUNDLE, ABILITY)
                time.sleep(_APP_LAUNCH_WAIT)
                launched = True
                continue  # 拉起后立即重试，不计入退避

        if attempt == RESUME_MAX_ATTEMPTS:
            break
        backoff = RESUME_BACKOFF * attempt
        print(f"  {name}: 拉取中断（{last_reason}），{backoff:.0f}s 后续传"
              f"（第 {attempt + 1}/{RESUME_MAX_ATTEMPTS} 次）", file=sys.stderr)
        time.sleep(backoff)

    sys.exit(
        f"拉取失败：{name} 在 {RESUME_MAX_ATTEMPTS} 次尝试后仍未完成\n"
        f"（最后一次原因：{last_reason or '未知'}）"
    )


def cmd_pull(udid: str, remote: str, local: str | None,
             port: int = PORT) -> int:
    """br pull <远端名> [本地路径]。

    remote 为设备侧文件名或相对路径，默认基于 filesDir/。
    local 为空时取 remote 的文件名部分存到当前目录。
    """
    remote = resolve_remote_name(remote)
    target = _local_target(remote, local)
    # 保活覆盖整个重试循环：pull 同样怕熄屏挂起与隧道回收
    with KeepAlive(udid, port):
        _pull_with_retry(udid, port, remote, target)
    return 0

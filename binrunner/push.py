"""文件推送：TCP 协议封包与目录递归。

依赖：config, hdc。

协议（单连接单文件，小端）。首 4 字节区分版本：

v1（无续传）：
    PC → 设备:  u32 nameLen | name (UTF-8) | u64 payloadSize | payload

v2（续传，首字段为 RESUME_MAGIC）：
    PC → 设备:  u32 magic | u32 flags | u32 nameLen | name | u64 size
                | u32 probeLen | probe（flags & FLAG_RESUME 时）
    设备 → PC:  u64 resumeFrom（已有字节数，0 = 从头）
    PC → 设备:  payload 自 resumeFrom 起的剩余部分

两版都以 u64 累计 ACK 做流控（每 ACK_INTERVAL 字节一个）。
设备把传输中的数据写在 <name>.part，收满才原子 rename 为正式名 ——
故正式名下不会出现半成品，中断的 .part 供下次续传。

payload 分块流式发送，不在内存中拼装完整封包 —— 支持至 4GiB 的文件。
设备侧同样流式落盘（见 app/entry/src/main/ets/common/PushServer.ets）。

发送受 ACK 流控：在途字节超过 MAX_INFLIGHT_BYTES 就等设备确认。
设备侧是 ArkTS 单线程，主线程被同步 IO 阻塞时接收回调停摆，
无流控地全速发送会灌满内核缓冲区并被重置连接。

路径校验规则必须与设备侧保持一致 —— 双端任一放行都会造成沙箱逃逸。
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
    MAX_INFLIGHT_BYTES,
    PROGRESS_THRESHOLD,
    RESUME_BACKOFF,
    RESUME_MAX_ATTEMPTS,
    RESUME_MIN_SIZE,
)
from binrunner.hdc import ensure_forward, restart_app, wakeup_screen
from binrunner.keepalive import KeepAlive
from binrunner.protocol import (
    FLAG_RESUME,
    MAX_FILE_SIZE,
    RESUME_MAGIC,
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
    validate_remote_name,
    resolve_remote_name,
)

# 首连失败后拉起 App 再试一次的等待时间
_APP_LAUNCH_WAIT = 2



def encode_header(name: str, payload_size: int) -> bytes:
    """封装协议头：u32 nameLen | name | u64 payloadSize（小端）。

    payload 随后单独流式发送，不与头部拼接 —— 否则大文件会在内存里翻倍。
    """
    name_bytes = name.encode()
    return (
        struct.pack("<I", len(name_bytes))
        + name_bytes
        + pack_u64(payload_size)
    )


def encode_header_v2(name: str, payload_size: int, probe: bytes = b"") -> bytes:
    """封装 v2 协议头（支持续传）。

    格式：u32 magic | u32 flags | u32 nameLen | name | u64 size
          [flags & FLAG_RESUME 时附 u32 probeLen | probe]

    magic 让设备一眼分辨新旧协议 —— v1 首字段是 nameLen（1..256），
    与魔数值域不重叠，故旧设备不会把 v2 头误读成 v1。
    """
    name_bytes = name.encode()
    flags = FLAG_RESUME if probe else 0
    head = (
        struct.pack("<I", RESUME_MAGIC)
        + struct.pack("<I", flags)
        + struct.pack("<I", len(name_bytes))
        + name_bytes
        + pack_u64(payload_size)
    )
    if probe:
        head += struct.pack("<I", len(probe)) + probe
    return head


def encode_packet(name: str, payload: bytes) -> bytes:
    """封装完整报文（头 + payload）。仅供测试与小数据使用。

    生产路径用 encode_header + 流式发送，避免大文件占用双倍内存。
    """
    return encode_header(name, len(payload)) + payload


def validate_remote(name: str, size: int) -> None:
    """发送前校验远端名与文件大小，非法即终止（不读内容、不建连）。"""
    validate_remote_name(name)
    if size > MAX_FILE_SIZE:
        sys.exit(
            f"文件过大: {name} ({size / (1 << 30):.2f} GiB)，"
            f"上限 {MAX_FILE_SIZE / (1 << 30):.0f} GiB"
        )


def _read_ack(sock, acked: int) -> int:
    """读一个 u64 ACK，返回设备已落盘总量。

    设备可能把多个 ACK 合并在一个 TCP 段里；只取最后一个完整的即可，
    因为 ACK 是累计值而非增量。

    超时抛 TimeoutError（OSError 子类）而非直接退出 —— 调用方的
    _send_stream 重试循环据此重建 fport/唤醒/重启 App 后续传。
    """
    try:
        return unpack_u64(recv_exactly(sock, 8, "确认"))
    except SOCKET_TIMEOUT:
        raise TimeoutError(
            f"等待设备确认超时（已确认 {fmt_size(acked)}，"
            f"{ACK_TIMEOUT:.0f}s 无响应）；设备侧可能卡住或 App 被杀"
        ) from None


def _stream_payload(sock, reader, size: int, name: str, start: int = 0) -> int:
    """分块发送 payload，受 ACK 流控，大文件打印进度。

    reader 需支持 read(n)，且已定位到 start（续传时非 0）。
    按 TRANSFER_CHUNK_SIZE 分块，避免整文件驻留内存；在途字节达到
    MAX_INFLIGHT_BYTES 时阻塞等设备 ACK，防止灌爆接收缓冲区。

    进度与 ACK 都按整个文件的绝对偏移计算（而非本次连接的增量），
    这样续传时百分比是连续的，设备回的累计 ACK 也能直接比对。

    返回设备确认的落盘总量 —— 中断时调用方据此决定下次从哪续。
    """
    show_progress = size >= PROGRESS_THRESHOLD
    sent = start
    acked = start

    def report() -> None:
        if not show_progress:
            return
        pct = sent * 100 // size if size else 100
        resumed = f" (续传自 {fmt_size(start)})" if start else ""
        print(f"\r  {name}: {pct}% ({fmt_size(sent)}/{fmt_size(size)}){resumed}",
              end="", file=sys.stderr, flush=True)

    try:
        while True:
            chunk = reader.read(TRANSFER_CHUNK_SIZE)
            if not chunk:
                break
            sock.sendall(chunk)
            sent += len(chunk)
            report()
            # 在途过多则等待，直到设备消化到安全水位
            while sent - acked > MAX_INFLIGHT_BYTES:
                acked = _read_ack(sock, acked)

        if sent != size:
            # 传输中文件被截断/改写：设备侧会因 size mismatch 丢弃，此处提前报错
            if show_progress:
                print(file=sys.stderr)
            sys.exit(f"读取字节数与文件大小不符: {name}（{sent} != {size}）")

        # 等收尾 ACK，确认全部落盘（设备在收满时必回一个）
        while acked < size:
            acked = _read_ack(sock, acked)
        report()
    finally:
        if show_progress:
            print(file=sys.stderr)  # 进度行收尾换行（含异常路径）
    return acked


def _read_probe(opener, size: int) -> bytes:
    """读文件头作为续传探针，供设备比对已有 .part 是否同源。

    小文件不值得协商（重传比往返更快），返回空探针即退化为普通传输。
    """
    if size < RESUME_MIN_SIZE:
        return b""
    with opener() as f:
        return f.read(min(RESUME_PROBE_BYTES, size))


def _open_and_send(port: int, name: str, opener, size: int,
                   resume: bool = True) -> int:
    """建连、协商续传偏移、流式发送。opener() 返回可 read(n)/seek() 的对象。

    resume=True 时用 v2 协议头带探针，设备回可续偏移；否则从头传。
    返回设备确认的落盘总量，供调用方判断是否需要再续。
    """
    probe = _read_probe(opener, size) if resume else b""
    with open_connection(port) as s:
        if probe:
            s.sendall(encode_header_v2(name, size, probe))
            # v2 设备在头部解析后立即回 u64 可续偏移
            start = _read_ack(s, 0)
            if start > size:
                start = 0  # 设备状态异常，保守地从头传
        else:
            s.sendall(encode_header(name, size))
            start = 0

        if start == size:
            return size  # 设备已有完整文件，无需再传

        with opener() as reader:
            if start:
                reader.seek(start)
            return _stream_payload(s, reader, size, name, start)


def _send_stream(port: int, name: str, opener, size: int, udid: str) -> None:
    """流式发送单个文件到 PushServer，中断则从设备已落盘处续传。

    重试策略分两类：
      - 首连失败（App 没监听）→ 拉起 App 再试
      - 传输中断（fport 隧道抖动等）→ 退避后续传，不重发已确认部分

    每次尝试都重新协商偏移，故即使连续中断也在推进；
    RESUME_MAX_ATTEMPTS 用于挡住"每次只前进几字节"的病态链路。
    """
    name = resolve_remote_name(name)
    validate_remote(name, size)
    launched = False
    last_acked = 0
    last_reason = ""

    for attempt in range(1, RESUME_MAX_ATTEMPTS + 1):
        try:
            acked = _open_and_send(port, name, opener, size)
            if acked >= size:
                print(f"OK: {name} ({size} bytes)")
                return
            # 设备确认量不足却没抛异常：视作中断，继续续传
            reason = f"设备仅确认 {fmt_size(acked)}/{fmt_size(size)}"
        except OSError as e:
            acked = 0
            reason = str(e)
            last_reason = reason
            # 自愈组合拳：重建 fport（隧道可能被回收）+ 点亮屏幕（避免 App
            # 进后台挂起监听）+ 拉起 App。三者缺一不可 —— 只拉起 App 而
            # 隧道已断仍会 Connection refused。首连失败才重启 App（保留
            # 续传状态）；后续重试只重建隧道 + 唤醒，避免反复重启打断续传。
            ensure_forward(udid, port, force=True)
            wakeup_screen(udid)
            if not launched:
                restart_app(udid, BUNDLE, ABILITY)
                time.sleep(_APP_LAUNCH_WAIT)
                launched = True
                continue  # 拉起后立即重试，不计入退避

        if attempt == RESUME_MAX_ATTEMPTS:
            break
        if acked and acked <= last_acked:
            # 一轮下来毫无进展，再试也是徒劳
            sys.exit(f"推送失败：{name} 无法继续推进（停在 {fmt_size(acked)}）\n{reason}")
        last_acked = max(last_acked, acked)
        backoff = RESUME_BACKOFF * attempt
        print(f"  {name}: 传输中断（{reason}），{backoff:.0f}s 后续传"
              f"（第 {attempt + 1}/{RESUME_MAX_ATTEMPTS} 次）", file=sys.stderr)
        time.sleep(backoff)

    sys.exit(
        f"推送失败：{name} 在 {RESUME_MAX_ATTEMPTS} 次尝试后仍未完成\n"
        f"（最后进度 {fmt_size(last_acked)}/{fmt_size(size)}；"
        f"最后一次原因：{last_reason or '未知'}）"
    )


def _send_file(port: int, name: str, payload: bytes, udid: str) -> None:
    """发送内存中的数据（小数据/测试用）。大文件走 push_file 的流式路径。"""
    import io

    _send_stream(port, name, lambda: io.BytesIO(payload), len(payload), udid)


def _push_local_file(udid: str, local: str, remote: str, port: int) -> None:
    """流式推送本地文件（不将整个文件读入内存）。"""
    size = os.path.getsize(local)
    _send_stream(port, remote, lambda: open(local, "rb"), size, udid)


def push_file(udid: str, local: str, remote: str, port: int) -> None:
    """推送单个文件到 filesDir/<remote>。"""
    with KeepAlive(udid, port):
        _push_local_file(udid, local, remote, port)


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
    """递归推送目录树到 filesDir/，保持子目录结构。"""
    files = collect_tree(local_dir)
    if not files:
        print(f"目录为空: {local_dir}")
        return
    # 本地目录中的 @ 是字面目录名，不能当 CLI 根目录语法再展开。
    for _full, rel in files:
        validate_remote_name(rel)
    print(f"推送 {len(files)} 个文件...")
    with KeepAlive(udid, port):
        for full, rel in files:
            # 走流式路径：大文件不驻留内存，且中断可续传
            _push_local_file(udid, full, rel, port)

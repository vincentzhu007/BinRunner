"""br pull 的续传协商与传输可靠性测试（binrunner.pull）。

pull 此前**没有任何测试**，而本轮给它加了 v2 续传、`.part` 原子落盘与保活 ——
这些恰好是会静默产出坏文件的地方（半成品被当成正式文件、同名不同内容被拼接），
所以全部钉在这里。
"""
from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest

from binrunner import pull as pullmod
from binrunner.config import ACK_TIMEOUT
from binrunner.protocol import (
    PULL_MAGIC,
    PULL_MAGIC_V2,
    RESUME_PROBE_BYTES,
    TRANSFER_CHUNK_SIZE,
)
from binrunner.pull import _local_target, encode_pull_request
from binrunner.transfer import pack_u64, unpack_u64


class FakePullDevice:
    """模拟 PushServer 的 pull 应答，并记录收到的请求。

    按设备侧契约实现：v1 只回 totalSize；v2 先回 resumeAck 再回 totalSize，
    探针与源文件头部一致才允许复用本地残片。

    `supports_v2=False` 模拟旧 HAP。真机上它是**静默挂住**：`fail()` 只关 fd，
    不 close 连接，`message` 回调随后丢弃一切 —— 既没有应答也没有断连，
    客户端只能等到协商超时（这正是本轮踩到的坑，故按 hang 建模为默认）。
    """

    def __init__(self, payload: bytes, *, exists: bool = True,
                 supports_v2: bool = True, drop_after_payload: int | None = None,
                 hang_on_v2: bool = True):
        self.payload = payload
        self.exists = exists
        self.supports_v2 = supports_v2
        self.drop_after_payload = drop_after_payload
        # 默认按「静默挂住」建模；hang_on_v2=False 模拟直接断开（App 崩溃）
        self.hang_on_v2 = hang_on_v2
        self.requests: list[dict] = []
        self.timeouts: list[float] = []
        self._out = b""
        self._served = 0
        self._cut: int | None = None
        self._closed = False
        self._hang = False
    # --- socket 接口 ---
    def settimeout(self, t):
        """记录超时设置：协商窗口与 payload 窗口用的超时不同，必须可断言。"""
        self.timeouts.append(t)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, request: bytes):
        assert len(request) >= 16, f"请求头不完整: {len(request)} 字节"
        magic = struct.unpack("<I", request[:4])[0]
        name_len = struct.unpack("<I", request[4:8])[0]
        name = request[8:8 + name_len].decode()
        off = 8 + name_len
        offset = unpack_u64(request[off:off + 8])
        size = unpack_u64(request[off + 8:off + 16])
        off += 16
        probe = b""
        if magic == PULL_MAGIC_V2:
            probe_len = struct.unpack("<I", request[off:off + 4])[0]
            probe = request[off + 4:off + 4 + probe_len]

        self.requests.append({
            "magic": magic, "name": name, "offset": offset,
            "size": size, "probe": probe,
        })

        if magic == PULL_MAGIC_V2 and not self.supports_v2:
            if self.hang_on_v2:
                self._hang = True    # 静默挂住：既不应答也不断连
            else:
                self._closed = True  # 变体：直接断开
            return

        if not self.exists:
            self._out = pack_u64(0) if magic == PULL_MAGIC else pack_u64(0) * 2
            return

        # 可复用本地残片：探针与源文件头部逐字节一致，且偏移不超过源文件大小
        start = 0
        if magic == PULL_MAGIC_V2 and probe and 0 < offset <= len(self.payload) \
                and self.payload[:len(probe)] == probe:
            start = offset

        if magic == PULL_MAGIC_V2:
            self._out = pack_u64(start)
        self._out += pack_u64(len(self.payload))
        self._payload_start = len(self._out)
        self._out += self.payload[start:]
        if self.drop_after_payload is not None:
            self._cut = self._payload_start + self.drop_after_payload

    def recv(self, n):
        if self._hang:
            # 旧 HAP 挂住 → 客户端协商超时。抛 socket.timeout 而非内置 TimeoutError：
            # 3.9 里前者是独立的 OSError 子类（3.10 起才是同一类），必须按真实类型建模。
            raise socket.timeout("timed out")
        if self._closed:
            return b""
        if self._cut is not None:
            allowed = self._cut - self._served
            if allowed <= 0:
                return b""      # 模拟链路中断
            n = min(n, allowed)
        data = self._out[:n]
        self._out = self._out[n:]
        self._served += len(data)
        return data


@pytest.fixture(autouse=True)
def reset_v2_flag(monkeypatch):
    """_PULL_V2_OK 是模块级状态（跨 pull 缓存），逐测试复位避免相互污染。"""
    monkeypatch.setattr(pullmod, "_PULL_V2_OK", True)


@pytest.fixture
def connect(monkeypatch):
    """把建连换成假设备；返回 install(payload, **kw) 便于逐次替换设备行为。"""
    def install(payload: bytes, **kw) -> FakePullDevice:
        dev = FakePullDevice(payload, **kw)
        monkeypatch.setattr(pullmod, "open_connection", lambda port: dev)
        return dev

    return install


@pytest.fixture
def no_hdc(monkeypatch):
    """跳过 fport/唤醒/重启（不需要真实设备）。"""
    monkeypatch.setattr(pullmod, "ensure_forward", lambda *a, **kw: None)
    monkeypatch.setattr(pullmod, "wakeup_screen", lambda *a, **kw: None)
    monkeypatch.setattr(pullmod, "restart_app", lambda *a, **kw: None)
    monkeypatch.setattr(pullmod.time, "sleep", lambda _s: None)


def pattern(n: int) -> bytes:
    """可验证的内容模式（能捕获错序/重复，全同字节不能）。"""
    return bytes(i % 251 for i in range(n))


class TestEncodePullRequest:
    def test_v1_layout_without_probe(self):
        req = encode_pull_request("a.bin", 3, 0)
        assert struct.unpack("<I", req[:4])[0] == PULL_MAGIC
        assert struct.unpack("<I", req[4:8])[0] == 5
        assert req[8:13] == b"a.bin"
        assert unpack_u64(req[13:21]) == 3
        assert unpack_u64(req[21:29]) == 0
        assert len(req) == 29

    def test_v2_layout_with_probe(self):
        probe = b"\x7fELF" + b"\x00" * 60
        req = encode_pull_request("a.bin", 1024, 0, probe)
        assert struct.unpack("<I", req[:4])[0] == PULL_MAGIC_V2
        off = 8 + 5 + 8 + 8
        assert struct.unpack("<I", req[off:off + 4])[0] == len(probe)
        assert req[off + 4:] == probe
        assert len(req) == off + 4 + len(probe)

    def test_probe_presence_selects_version(self):
        assert struct.unpack("<I", encode_pull_request("a", 0, 0, b"")[:4])[0] == PULL_MAGIC
        assert struct.unpack("<I", encode_pull_request("a", 0, 0, b"x")[:4])[0] == PULL_MAGIC_V2


class TestLocalTarget:
    def test_defaults_to_remote_basename(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _local_target("models/net.ms", None) == "net.ms"

    def test_keeps_explicit_file_path(self):
        assert _local_target("a.bin", "/tmp/x.bin") == "/tmp/x.bin"

    def test_directory_gets_remote_basename(self, tmp_path):
        d = tmp_path / "dl"
        d.mkdir()
        assert _local_target("models/net.ms", str(d)) == str(d / "net.ms")

    def test_trailing_slash_remote(self):
        assert _local_target("dir/", None) == "dir"


class TestCmdPullGuards:
    @pytest.mark.parametrize("bad", ["/etc/passwd", "..", "a/../b", "a\\b"])
    def test_rejects_unsafe_name_before_touching_device(self, bad):
        with pytest.raises(SystemExit):
            pullmod.cmd_pull("UDID", bad, None)


class TestFreshPull:
    def test_uses_v1_and_writes_target(self, tmp_path, connect):
        payload = pattern(1000)
        target = str(tmp_path / "f.bin")

        dev = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        # 没有本地残片 → 无从校验同源 → 必须走 v1 整传
        assert dev.requests[0]["magic"] == PULL_MAGIC
        assert dev.requests[0]["offset"] == 0
        assert Path(target).read_bytes() == payload
        assert not Path(target + ".part").exists(), "收满后不得残留 .part"

    def test_missing_remote_file_exits(self, tmp_path, connect):
        target = str(tmp_path / "f.bin")
        connect(b"", exists=False)

        with pytest.raises(SystemExit) as ei:
            pullmod._pull_once(8888, "f.bin", target)
        assert "不存在" in str(ei.value)
        assert not Path(target).exists()


class TestResumeNegotiation:
    def test_resumes_only_the_tail_when_probe_matches(self, tmp_path, connect):
        payload = pattern(4096)
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(payload[:1500])

        dev = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        assert dev.requests[0]["magic"] == PULL_MAGIC_V2
        assert dev.requests[0]["offset"] == 1500, "应从本地残片长度处续"
        assert dev.requests[0]["probe"] == payload[:1500], "探针必须是残片头部"
        assert Path(target).read_bytes() == payload
        assert not Path(target + ".part").exists()

    def test_probe_is_capped_at_configured_length(self, tmp_path, connect):
        size = RESUME_PROBE_BYTES + 500
        payload = pattern(size)
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(payload)

        dev = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        assert len(dev.requests[0]["probe"]) == RESUME_PROBE_BYTES

    def test_probe_mismatch_retransfers_from_scratch(self, tmp_path, connect):
        """同名不同内容：设备拒绝复用，客户端必须整传而不是拼出坏文件。"""
        payload = b"A" * 4096
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(b"B" * 1000)

        dev = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        assert dev.requests[0]["offset"] == 1000, "客户端先提出续传"
        assert Path(target).read_bytes() == payload, "设备拒绝后必须整传"

    def test_complete_local_part_finalizes_without_refetch(self, tmp_path, connect):
        payload = pattern(2048)
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(payload)

        dev = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        assert dev.requests[0]["offset"] == 2048
        assert Path(target).read_bytes() == payload


class TestInterruption:
    def test_interrupted_transfer_keeps_part_and_defers_final_name(self, tmp_path, connect):
        payload = pattern(4096)
        target = str(tmp_path / "f.bin")

        connect(payload, drop_after_payload=1000)
        with pytest.raises(pullmod._IncompleteTransfer):
            pullmod._pull_once(8888, "f.bin", target)

        part = Path(target + ".part")
        assert part.read_bytes() == payload[:1000], "已落盘部分必须保留供续传"
        assert not Path(target).exists(), "未收满不得出现正式名"

    def test_second_attempt_resumes_from_interruption(self, tmp_path, connect):
        payload = pattern(4096)
        target = str(tmp_path / "f.bin")

        connect(payload, drop_after_payload=1000)
        with pytest.raises(pullmod._IncompleteTransfer):
            pullmod._pull_once(8888, "f.bin", target)

        dev = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        assert dev.requests[0]["offset"] == 1000, "第二次应只补缺口"
        assert Path(target).read_bytes() == payload
        assert not Path(target + ".part").exists()

    def test_retry_loop_completes_after_interruption(self, tmp_path, connect, no_hdc, monkeypatch):
        payload = pattern(8192)
        target = str(tmp_path / "f.bin")
        opens = []

        def open_conn(port):
            opens.append(1)
            if len(opens) == 1:
                return FakePullDevice(payload, drop_after_payload=1000)
            return FakePullDevice(payload)

        monkeypatch.setattr(pullmod, "open_connection", open_conn)

        pullmod._pull_with_retry("UDID", 8888, "f.bin", target)

        assert len(opens) >= 2, "中断后应自动重试"
        assert Path(target).read_bytes() == payload
        assert not Path(target + ".part").exists()


class TestOldHapFallback:
    def test_downgrades_to_v1_and_retransfers(self, tmp_path, connect):
        """旧 HAP 不认 PULL_MAGIC_V2：降级为 v1 整传，且不按长度盲续。"""
        payload = pattern(4096)
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(payload[:1000])

        # 真机旧 HAP 的行为：不认 v2 → fail() 只关 fd，既不应答也不断连。
        # 所以协商窗口必须靠**短超时**判定，而不是等对端断开。
        dev = connect(payload, supports_v2=False, hang_on_v2=True)
        with pytest.raises(pullmod._PullV2Unsupported):
            pullmod._pull_once(8888, "f.bin", target)

        assert dev.requests[0]["magic"] == PULL_MAGIC_V2
        assert dev.requests[0]["offset"] == 1000
        assert pullmod._PULL_V2_OK is False, "应记住设备不支持续传，避免每轮白等"

        dev2 = connect(payload)
        pullmod._pull_once(8888, "f.bin", target)

        assert dev2.requests[0]["magic"] == PULL_MAGIC
        assert dev2.requests[0]["offset"] == 0, (
            "v1 没有探针，无法验证同源 —— 必须整传而不是按长度盲续"
        )
        assert Path(target).read_bytes() == payload

    def test_also_handles_a_device_that_just_disconnects(self, tmp_path, connect):
        """另一种表现：对端在协商前直接断开（App 崩溃）。两条路径都要降级。"""
        payload = pattern(4096)
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(payload[:1000])

        dev = connect(payload, supports_v2=False, hang_on_v2=False)
        with pytest.raises(pullmod._PullV2Unsupported):
            pullmod._pull_once(8888, "f.bin", target)
        assert dev.requests[0]["magic"] == PULL_MAGIC_V2

    def test_negotiation_uses_a_short_timeout_not_ack_timeout(
            self, tmp_path, connect, monkeypatch):
        """协商窗口必须短：旧 HAP 静默挂住时，用 ACK_TIMEOUT 会白等 60 秒（实测 61 秒）。"""
        payload = pattern(4096)
        target = str(tmp_path / "f.bin")
        Path(target + ".part").write_bytes(payload[:1000])

        monkeypatch.setattr(pullmod, "PULL_NEGOTIATE_TIMEOUT", 0.05)
        dev = connect(payload, supports_v2=False, hang_on_v2=True)
        with pytest.raises(pullmod._PullV2Unsupported):
            pullmod._pull_once(8888, "f.bin", target)

        assert 0.05 in dev.timeouts, f"协商应使用短超时，实际为 {dev.timeouts}"
        assert dev.timeouts[-1] == ACK_TIMEOUT, "协商结束后必须恢复常规超时"


class TestStreaming:
    def test_large_payload_arrives_intact_in_chunks(self, tmp_path, connect):
        """分块传输不损坏、不重排；单块不超过 TRANSFER_CHUNK_SIZE。"""
        size = TRANSFER_CHUNK_SIZE * 2 + 777
        payload = bytes(i % 256 for i in range(size))
        target = str(tmp_path / "big.bin")

        dev = connect(payload)
        pullmod._pull_once(8888, "big.bin", target)

        assert Path(target).read_bytes() == payload
        assert dev.requests[0]["size"] == 0, "size 字段当前保留未用"

    def test_empty_remote_file_looks_missing(self, tmp_path, connect):
        """已知限制：协议用 totalSize=0 同时表示「文件不存在」和「空文件」。

        设备上确实有一个 0 字节文件时，CLI 会报「设备上不存在」。要区分两者
        得让设备多回一个 found 标志（线上协议变更，需双端同时更新），不在本轮
        范围 —— 这里把现有行为钉住，避免以后误以为它已经能用。
        """
        target = str(tmp_path / "empty.bin")
        connect(b"")
        with pytest.raises(SystemExit) as ei:
            pullmod._pull_once(8888, "empty.bin", target)
        assert "不存在" in str(ei.value)
        assert not Path(target).exists()


@pytest.mark.parametrize('remote, expected', [('x', 'x'), ('@/x', 'x'),
                                             ('bin/x', 'bin/x'), ('@/bin/x', 'bin/x')])
def test_pull_files_root_wire_path(tmp_path, connect, no_hdc, monkeypatch, remote, expected):
    from contextlib import nullcontext
    monkeypatch.setattr(pullmod, 'KeepAlive', lambda *a: nullcontext())
    dev = connect(b'ok\n')
    target = tmp_path / 'out'
    assert pullmod.cmd_pull('UDID', remote, str(target)) == 0
    assert dev.requests[0]['name'] == expected
    assert target.read_bytes() == b'ok\n'

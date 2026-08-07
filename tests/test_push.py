"""推送路径校验与协议封包测试（binrunner.push）。

路径校验是安全边界（防目录穿越），与设备侧 PushServer.ets 的校验规则
必须一致 —— 双端任一放行都会造成沙箱逃逸。
"""
import socket
import struct

import pytest

from binrunner import push as pushmod
from binrunner.config import (
    FLAG_RESUME,
    MAX_FILE_SIZE,
    MAX_INFLIGHT_BYTES,
    PROGRESS_THRESHOLD,
    PUSH_CHUNK_SIZE,
    RESUME_MAGIC,
    RESUME_MIN_SIZE,
    RESUME_PROBE_BYTES,
)
from binrunner.push import (
    collect_tree,
    encode_header,
    encode_packet,
    is_safe_remote_name,
    push_file,
    push_tree,
)


class FakeSocket:
    """记录 sendall 内容的 socket 替身，模拟设备侧协议行为。

    流式发送会多次 sendall（头 + 各分块），故把所有片段拼成单个报文
    追加到 sink，使断言可按完整协议解析。

    双版本协议都支持：识别 v2 魔数后先回 resume_from 偏移，再按累计
    ACK 确认 payload —— 与 PushServer.ets 的行为对齐，否则客户端的
    流控和续传协商会在测试里死等。
    """

    def __init__(self, sink, resume_from=0):
        self._sink = sink
        self._buf = bytearray()
        self._header_len = None
        self._ack = bytearray()
        # 设备已有的字节数：v2 握手时回给客户端
        self._resume_from = resume_from

    def settimeout(self, _timeout):
        """真实 socket 会被设置发送超时，替身需接受该调用。"""

    def _parse_header_len(self):
        """解析头长度；头未收齐返回 None。同时排入 v2 的续传偏移应答。"""
        buf = bytes(self._buf)
        if len(buf) < 4:
            return None
        first = struct.unpack("<I", buf[:4])[0]
        if first != RESUME_MAGIC:
            # v1: u32 nameLen | name | u64 size
            if len(buf) < 4 + first + 8:
                return None
            return 4 + first + 8
        # v2: magic | flags | nameLen | name | u64 size [| probeLen | probe]
        if len(buf) < 12:
            return None
        flags = struct.unpack("<I", buf[4:8])[0]
        name_len = struct.unpack("<I", buf[8:12])[0]
        off = 12 + name_len + 8
        if flags & FLAG_RESUME:
            if len(buf) < off + 4:
                return None
            probe_len = struct.unpack("<I", buf[off:off + 4])[0]
            off += 4 + probe_len
        if len(buf) < off:
            return None
        # 头收齐：设备此刻回续传偏移
        self._ack += struct.pack("<Q", self._resume_from)
        return off

    def sendall(self, data):
        self._buf += data
        # 模拟设备侧行为：payload 一到就确认，让流控立即放行。
        if self._header_len is None:
            self._header_len = self._parse_header_len()
            if self._header_len is None:
                return  # 头仍未收齐
        received = len(self._buf) - self._header_len
        if received > 0:
            # ACK 是落盘总量（含续传起点），与设备侧语义一致
            self._ack += struct.pack("<Q", self._resume_from + received)

    def recv(self, n):
        """回放已排队的 ACK / 续传偏移。"""
        out = bytes(self._ack[:n])
        del self._ack[:n]
        return out

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        # 连接关闭时才落账：此时该文件的所有分块已发完
        self._sink.append(bytes(self._buf))
        self._buf = bytearray()
        return False


@pytest.fixture
def sent(monkeypatch):
    """拦截 socket 连接，返回收集到的封包列表。"""
    packets = []
    monkeypatch.setattr(
        pushmod.socket, "create_connection", lambda *a, **kw: FakeSocket(packets)
    )
    return packets


@pytest.fixture
def no_forward(monkeypatch):
    """跳过 hdc fport 和 hdc 重试（不需要真实设备/App）。"""
    monkeypatch.setattr(pushmod, "ensure_forward", lambda *a: None)
    monkeypatch.setattr(pushmod, "run_hdc", lambda *a, **kw: None)


def unpack(packet):
    """解析 v1/v2 协议，返回 (name, declared_size, payload)。

    v2 头多出魔数/标志/探针，但对断言而言关心的仍是这三项，
    故统一成同一元组，让既有用例不必区分版本。
    """
    (first,) = struct.unpack("<I", packet[:4])
    if first != RESUME_MAGIC:
        name_len = first
        name = packet[4 : 4 + name_len].decode()
        off = 4 + name_len
        (size,) = struct.unpack("<Q", packet[off : off + 8])
        return name, size, packet[off + 8 :]

    (flags,) = struct.unpack("<I", packet[4:8])
    (name_len,) = struct.unpack("<I", packet[8:12])
    name = packet[12 : 12 + name_len].decode()
    off = 12 + name_len
    (size,) = struct.unpack("<Q", packet[off : off + 8])
    off += 8
    if flags & FLAG_RESUME:
        (probe_len,) = struct.unpack("<I", packet[off : off + 4])
        off += 4 + probe_len
    return name, size, packet[off:]


class TestSafeRemoteName:
    """路径校验纯函数。"""

    @pytest.mark.parametrize(
        "bad",
        [
            "",                     # 空名
            "/etc/passwd",          # 绝对路径
            "/",                    # 根
            "..",                   # 上级
            ".",                    # 当前目录
            "a/../../etc/passwd",   # 中间穿越
            "sub/..",               # 结尾穿越
            "a\\b",                 # Windows 分隔符
        ],
    )
    def test_rejects_unsafe(self, bad):
        assert not is_safe_remote_name(bad), f"应拒绝: {bad!r}"

    @pytest.mark.parametrize(
        "good",
        [
            "hello",
            "libfoo.so",
            "models/net.ms",     # 子目录
            "a/b/c/deep.bin",    # 多层
            "name.with.dots",
            "..hidden",          # 前缀含点但非穿越
            "dir/..name",        # 段首含点但非穿越
        ],
    )
    def test_accepts_safe(self, good):
        assert is_safe_remote_name(good), f"应接受: {good!r}"


class TestEncodePacket:
    """封包格式：u32 nameLen | name | u64 payloadSize | payload（小端）。"""

    def test_header_and_payload_layout(self):
        name, size, payload = unpack(encode_packet("hello", b"\x00\x01\x02bin"))
        assert name == "hello"
        assert payload == b"\x00\x01\x02bin"
        assert size == len(payload), "声明长度需与实际 payload 一致"

    def test_empty_payload(self):
        assert unpack(encode_packet("empty", b"")) == ("empty", 0, b"")

    def test_name_length_is_byte_count_not_char_count(self):
        packet = encode_packet("模型.ms", b"x")
        (name_len,) = struct.unpack("<I", packet[:4])
        assert name_len == len("模型.ms".encode())
        assert unpack(packet)[0] == "模型.ms"

    def test_binary_payload_not_mangled(self):
        payload = bytes(range(256))
        name, size, got = unpack(encode_packet("raw.bin", payload))
        assert got == payload
        assert size == 256


class TestSendFileValidation:
    """_send_file 在发送前拒绝非法名（不依赖设备侧兜底）。"""

    @pytest.mark.parametrize("bad", ["/abs", "..", "a/../b", "a\\b"])
    def test_illegal_names_exit_without_sending(self, bad, sent):
        with pytest.raises(SystemExit):
            pushmod._send_file(8888, bad, b"data", "UDID")
        assert sent == [], f"非法名 {bad!r} 不应发出任何数据"

    def test_name_over_limit_exits(self, sent):
        with pytest.raises(SystemExit):
            pushmod._send_file(8888, "a" * 257, b"data", "UDID")
        assert sent == []

    def test_name_at_limit_accepted(self, sent):
        pushmod._send_file(8888, "a" * 256, b"data", "UDID")
        assert len(sent) == 1

    def test_multibyte_name_limit_counted_in_bytes(self, sent):
        """长度上限按 UTF-8 字节数算，不是字符数。"""
        name = "中" * 100  # 100 字符 / 300 字节
        assert len(name) < 256 < len(name.encode())
        with pytest.raises(SystemExit):
            pushmod._send_file(8888, name, b"data", "UDID")
        assert sent == []


class TestSendRetry:
    """首连失败时拉起 App 重试一次。"""

    def test_retries_once_after_launching_app(self, monkeypatch):
        attempts, launched = [], []

        def flaky_connect(*a, **kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("connection refused")
            return FakeSocket([])

        monkeypatch.setattr(pushmod.socket, "create_connection", flaky_connect)
        monkeypatch.setattr(pushmod, "run_hdc", lambda *a, **kw: launched.append(a))
        monkeypatch.setattr(pushmod.time, "sleep", lambda _: None)

        pushmod._send_file(8888, "hello", b"data", "UDID")

        assert len(attempts) == 2, "应重试一次"
        assert launched, "重试前应尝试拉起 App"

    def test_exits_when_both_attempts_fail(self, monkeypatch):
        def always_fail(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr(pushmod.socket, "create_connection", always_fail)
        monkeypatch.setattr(pushmod, "run_hdc", lambda *a, **kw: None)
        monkeypatch.setattr(pushmod.time, "sleep", lambda _: None)

        with pytest.raises(SystemExit):
            pushmod._send_file(8888, "hello", b"data", "UDID")


class TestCollectTree:
    """目录遍历：相对路径保持子目录结构。"""

    def test_relative_paths_preserved(self, tmp_path):
        (tmp_path / "top.bin").write_bytes(b"A")
        sub = tmp_path / "models"
        sub.mkdir()
        (sub / "net.ms").write_bytes(b"B")
        (sub / "deep").mkdir()
        (sub / "deep" / "x.dat").write_bytes(b"C")

        rels = {rel for _, rel in collect_tree(str(tmp_path))}
        assert rels == {"top.bin", "models/net.ms", "models/deep/x.dat"}

    def test_empty_dir_yields_nothing(self, tmp_path):
        assert collect_tree(str(tmp_path)) == []

    def test_trailing_slash_does_not_add_leading_separator(self, tmp_path):
        """br push ./dir/ 带尾斜杠时相对路径不应多出前导分隔符。"""
        (tmp_path / "f.bin").write_bytes(b"x")
        rels = [rel for _, rel in collect_tree(str(tmp_path) + "/")]
        assert rels == ["f.bin"]


class TestPushTree:
    """目录递归推送端到端。"""

    def test_sends_all_files_with_relative_names(self, tmp_path, sent, no_forward):
        (tmp_path / "a.bin").write_bytes(b"A")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"B")

        push_tree("UDID", str(tmp_path), 8888)

        assert {unpack(p)[0] for p in sent} == {"a.bin", "sub/b.bin"}

    def test_payloads_match_file_contents(self, tmp_path, sent, no_forward):
        (tmp_path / "a.bin").write_bytes(b"content-a")
        push_tree("UDID", str(tmp_path), 8888)
        assert unpack(sent[0]) == ("a.bin", 9, b"content-a")

    def test_empty_dir_sends_nothing(self, tmp_path, sent, no_forward, capsys):
        push_tree("UDID", str(tmp_path), 8888)
        assert sent == []
        assert "目录为空" in capsys.readouterr().out


class TestResumeNegotiation:
    """续传协商：只补缺口，不重发已确认部分。

    这些用例守的是数据正确性 —— 偏移算错会拼出损坏文件，
    而损坏的 ELF 在设备上表现为随机 SIGSEGV，极难回溯到推送环节。
    """

    @pytest.fixture
    def big(self, tmp_path):
        """足够大以触发续传协商的文件（小文件按设计退化为 v1）。"""
        path = tmp_path / "big.bin"
        path.write_bytes(bytes(range(256)) * (RESUME_MIN_SIZE // 256 + 1))
        return path

    def _connect(self, monkeypatch, packets, resume_from):
        monkeypatch.setattr(
            pushmod.socket, "create_connection",
            lambda *a, **kw: FakeSocket(packets, resume_from=resume_from),
        )

    def test_uses_v2_header_when_file_large_enough(self, big, monkeypatch, no_forward):
        packets = []
        self._connect(monkeypatch, packets, 0)
        push_file("UDID", str(big), "big.bin", 8888)
        assert struct.unpack("<I", packets[0][:4])[0] == RESUME_MAGIC

    def test_small_file_stays_on_v1(self, tmp_path, sent, no_forward):
        """小文件重传比往返协商更快，不该付握手成本。"""
        small = tmp_path / "small.bin"
        small.write_bytes(b"x" * 16)
        push_file("UDID", str(small), "small.bin", 8888)
        assert struct.unpack("<I", sent[0][:4])[0] != RESUME_MAGIC

    def test_sends_only_the_remaining_tail(self, big, monkeypatch, no_forward):
        """设备已有 N 字节时，客户端只补 size-N。"""
        data = big.read_bytes()
        offset = len(data) // 3
        packets = []
        self._connect(monkeypatch, packets, offset)

        push_file("UDID", str(big), "big.bin", 8888)

        _, size, payload = unpack(packets[0])
        assert size == len(data), "声明的仍是文件总长"
        assert payload == data[offset:], "补的必须正好是缺口，且字节对齐"

    def test_skips_transfer_when_device_already_complete(self, big, monkeypatch,
                                                        no_forward, capsys):
        data = big.read_bytes()
        packets = []
        self._connect(monkeypatch, packets, len(data))

        push_file("UDID", str(big), "big.bin", 8888)

        assert unpack(packets[0])[2] == b"", "设备已完整时不应再发 payload"
        assert "OK" in capsys.readouterr().out

    def test_bogus_device_offset_falls_back_to_full_send(self, big, monkeypatch,
                                                        no_forward):
        """设备回的偏移超过文件长度（状态错乱）→ 保守地整份重传。"""
        data = big.read_bytes()
        packets = []
        self._connect(monkeypatch, packets, len(data) + 999)

        push_file("UDID", str(big), "big.bin", 8888)

        assert unpack(packets[0])[2] == data

    def test_probe_carries_file_head(self, big, monkeypatch, no_forward):
        """探针必须是文件头，设备靠它判断 .part 是否同源。"""
        data = big.read_bytes()
        packets = []
        self._connect(monkeypatch, packets, 0)

        push_file("UDID", str(big), "big.bin", 8888)

        packet = packets[0]
        (name_len,) = struct.unpack("<I", packet[8:12])
        off = 12 + name_len + 8
        (probe_len,) = struct.unpack("<I", packet[off : off + 4])
        probe = packet[off + 4 : off + 4 + probe_len]
        assert probe == data[:probe_len]
        assert probe_len == min(RESUME_PROBE_BYTES, len(data))


class TestResumeRetry:
    """中断后自动续传，以及病态链路的止损。"""

    def test_resumes_after_midway_interruption(self, tmp_path, monkeypatch,
                                              no_forward, capsys):
        """首次连接传一半就断，第二次从设备确认处接着传，拼出完整文件。

        `device` 模拟设备侧 .part 文件：每次连接把收到的 payload 追加进去，
        下次连接就以它的长度作为续传偏移 —— 这样断言的是端到端的字节正确性，
        而不只是"发了多少"。
        """
        data = bytes(range(256)) * (RESUME_MIN_SIZE // 256 + 1)
        local = tmp_path / "big.bin"
        local.write_bytes(data)
        cutoff = len(data) // 2
        device = bytearray()
        attempts = []

        class Device(FakeSocket):
            """第一次连接收到 cutoff 字节后断开，之后正常收完。"""

            def sendall(self, chunk):
                super().sendall(chunk)
                if self._header_len is None:
                    return
                if attempts[-1] == 1 and len(self._buf) - self._header_len >= cutoff:
                    self._flush()
                    raise OSError("tunnel reset")

            def _flush(self):
                if self._header_len is not None:
                    device.extend(unpack(bytes(self._buf))[2])
                    self._buf = bytearray()
                    self._header_len = None

            def __exit__(self, *exc):
                self._flush()
                return False

        def connect(*a, **kw):
            attempts.append(len(attempts) + 1)
            return Device(sink=[], resume_from=len(device))

        monkeypatch.setattr(pushmod.socket, "create_connection", connect)
        monkeypatch.setattr(pushmod.time, "sleep", lambda _: None)

        push_file("UDID", str(local), "big.bin", 8888)

        assert bytes(device) == data, "续传拼出的内容必须与源文件逐字节一致"
        assert len(attempts) == 2, "应恰好续传一次"
        assert "续传" in capsys.readouterr().err

    def test_gives_up_when_no_forward_progress(self, tmp_path, monkeypatch,
                                               no_forward):
        """设备每次都只确认同一个偏移 → 判定为无法推进，不无限重试。"""
        data = b"x" * (RESUME_MIN_SIZE + 4096)
        local = tmp_path / "stuck.bin"
        local.write_bytes(data)

        class Stuck(FakeSocket):
            def sendall(self, chunk):
                super().sendall(chunk)
                raise OSError("reset")

        monkeypatch.setattr(
            pushmod.socket, "create_connection",
            lambda *a, **kw: Stuck(sink=[], resume_from=1024),
        )
        monkeypatch.setattr(pushmod.time, "sleep", lambda _: None)
        monkeypatch.setattr(pushmod, "run_hdc", lambda *a, **kw: None)

        with pytest.raises(SystemExit) as exc:
            push_file("UDID", str(local), "stuck.bin", 8888)
        assert "推送失败" in str(exc.value)


class TestPushFile:
    """单文件推送。"""

    def test_reads_local_and_sends_under_remote_name(self, tmp_path, sent, no_forward):
        local = tmp_path / "local-name.bin"
        local.write_bytes(b"payload-bytes")

        push_file("UDID", str(local), "remote-name.bin", 8888)

        name, size, payload = unpack(sent[0])
        assert name == "remote-name.bin"
        assert payload == b"payload-bytes"
        assert size == len(b"payload-bytes")


class TestSizeLimit:
    """1GiB 上限校验（挡误操作，如误推整个镜像）。"""

    def test_rejects_oversized_file(self, sent):
        with pytest.raises(SystemExit) as ei:
            pushmod.validate_remote("huge.bin", MAX_FILE_SIZE + 1)
        assert "过大" in str(ei.value)

    def test_accepts_file_at_limit(self):
        pushmod.validate_remote("exact.bin", MAX_FILE_SIZE)  # 不应抛出

    def test_push_file_checks_size_before_reading(
        self, tmp_path, sent, no_forward, monkeypatch
    ):
        """超限文件应在读取前就被拒，不能先加载进内存。"""
        local = tmp_path / "fake-huge.bin"
        local.write_bytes(b"small actual content")
        # 伪造一个超限的大小，验证校验发生在 open 之前
        monkeypatch.setattr(pushmod.os.path, "getsize", lambda _: MAX_FILE_SIZE + 1)

        def forbidden_open(*a, **kw):
            raise AssertionError("超限文件不应被打开读取")

        monkeypatch.setattr("builtins.open", forbidden_open)

        with pytest.raises(SystemExit):
            push_file("UDID", str(local), "fake-huge.bin", 8888)
        assert sent == []


class TestStreaming:
    """分块流式发送：payload 不整体驻留内存。"""

    def test_large_payload_sent_in_multiple_chunks(self, tmp_path, no_forward, monkeypatch):
        """验证确实分块 sendall，而非一次性发送。"""
        size = PUSH_CHUNK_SIZE * 3 + 12345
        local = tmp_path / "big.bin"
        local.write_bytes(b"\xab" * size)

        chunk_sizes = []

        class ChunkRecorder(FakeSocket):
            def sendall(self, data):
                chunk_sizes.append(len(data))
                super().sendall(data)

        packets = []
        monkeypatch.setattr(
            pushmod.socket, "create_connection", lambda *a, **kw: ChunkRecorder(packets)
        )

        push_file("UDID", str(local), "big.bin", 8888)

        # 头部 1 次 + payload 至少 4 次（3 整块 + 余量）
        assert len(chunk_sizes) >= 5, f"应分块发送，实际 {len(chunk_sizes)} 次"
        payload_chunks = chunk_sizes[1:]
        assert max(payload_chunks) <= PUSH_CHUNK_SIZE, "单块不应超过 PUSH_CHUNK_SIZE"
        assert sum(payload_chunks) == size, "分块总和应等于文件大小"

    def test_large_payload_content_intact(self, tmp_path, sent, no_forward):
        """分块传输不应损坏或重排数据。"""
        size = PUSH_CHUNK_SIZE * 2 + 777
        # 用可验证的模式而非全同字节，能捕获错序/重复
        content = bytes(i % 256 for i in range(size))
        local = tmp_path / "pattern.bin"
        local.write_bytes(content)

        push_file("UDID", str(local), "pattern.bin", 8888)

        name, declared, payload = unpack(sent[0])
        assert name == "pattern.bin"
        assert declared == size, "头部声明的大小应与实际一致"
        assert payload == content, "分块重组后内容应完全一致"

    def test_empty_file_sends_header_only(self, tmp_path, sent, no_forward):
        local = tmp_path / "empty.bin"
        local.write_bytes(b"")

        push_file("UDID", str(local), "empty.bin", 8888)

        assert unpack(sent[0]) == ("empty.bin", 0, b"")

    def test_socket_timeout_covers_device_stalls(
        self, tmp_path, no_forward, monkeypatch
    ):
        """设备主线程可能被同步 IO 占用较久，send 与 ACK 等待都需放宽超时。"""
        local = tmp_path / "f.bin"
        local.write_bytes(b"x")
        timeouts = []

        class TimeoutRecorder(FakeSocket):
            def settimeout(self, t):
                timeouts.append(t)

        monkeypatch.setattr(
            pushmod.socket, "create_connection", lambda *a, **kw: TimeoutRecorder([])
        )

        push_file("UDID", str(local), "f.bin", 8888)

        assert timeouts and timeouts[0] >= 60, "超时应远大于连接超时"


class TestAckFlowControl:
    """ACK 流控：设备侧接收回调停摆时不得无限发送（实测会被重置连接）。"""

    def test_blocks_when_device_stops_acking(
        self, tmp_path, no_forward, monkeypatch
    ):
        """设备不回 ACK 时，在途字节不应超过上限太多。"""
        size = MAX_INFLIGHT_BYTES * 4
        local = tmp_path / "big.bin"
        local.write_bytes(b"x" * size)

        class SilentSocket(FakeSocket):
            """只收不确认，模拟主线程被阻塞的设备。"""

            def recv(self, n):
                raise socket.timeout()

        monkeypatch.setattr(
            pushmod.socket, "create_connection", lambda *a, **kw: SilentSocket([])
        )

        with pytest.raises(SystemExit) as exc:
            push_file("UDID", str(local), "big.bin", 8888)
        assert "超时" in str(exc.value)

    def test_broken_pipe_when_device_closes_early(
        self, tmp_path, no_forward, monkeypatch
    ):
        """设备在确认前关闭连接 → 应作为传输失败上报，而非静默成功。"""
        size = MAX_INFLIGHT_BYTES * 2
        local = tmp_path / "big.bin"
        local.write_bytes(b"x" * size)

        class ClosingSocket(FakeSocket):
            def recv(self, n):
                return b""  # EOF

        monkeypatch.setattr(
            pushmod.socket, "create_connection", lambda *a, **kw: ClosingSocket([])
        )

        with pytest.raises(SystemExit) as exc:
            push_file("UDID", str(local), "big.bin", 8888)
        assert "推送失败" in str(exc.value)

    def test_waits_for_final_ack(self, tmp_path, no_forward, monkeypatch):
        """全部发完后仍需等收尾 ACK，确认设备已落盘。"""
        local = tmp_path / "f.bin"
        local.write_bytes(b"y" * 1024)
        acks_read = []

        class CountingSocket(FakeSocket):
            def recv(self, n):
                data = super().recv(n)
                acks_read.append(len(data))
                return data

        monkeypatch.setattr(
            pushmod.socket, "create_connection", lambda *a, **kw: CountingSocket([])
        )

        push_file("UDID", str(local), "f.bin", 8888)

        assert sum(acks_read) >= 8, "应至少读取一个完整的 u64 收尾 ACK"


class TestStreamIntegrity:
    """读取字节数与声明大小不符时应报错（传输中文件被改写）。"""

    def test_short_read_detected(self, monkeypatch):
        """声明 100 字节但只读到 10 → 设备侧会 size mismatch，此处提前拦截。"""
        import io

        class ShortReader(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            pushmod.socket, "create_connection", lambda *a, **kw: FakeSocket([])
        )

        with pytest.raises(SystemExit) as ei:
            pushmod._send_stream(
                8888, "x.bin", lambda: ShortReader(b"0123456789"), 100, "UDID"
            )
        assert "不符" in str(ei.value)


class TestProgressOutput:
    """大文件打印进度，小文件不刷屏。"""

    def test_no_progress_for_small_file(self, tmp_path, sent, no_forward, capsys):
        local = tmp_path / "small.bin"
        local.write_bytes(b"x" * 1024)

        push_file("UDID", str(local), "small.bin", 8888)

        assert "%" not in capsys.readouterr().err, "小文件不应打印进度"

    def test_progress_shown_for_large_file(self, tmp_path, sent, no_forward, capsys):
        local = tmp_path / "large.bin"
        local.write_bytes(b"x" * (PROGRESS_THRESHOLD + 1))

        push_file("UDID", str(local), "large.bin", 8888)

        err = capsys.readouterr().err
        assert "%" in err and "100%" in err, "大文件应打印进度且到达 100%"

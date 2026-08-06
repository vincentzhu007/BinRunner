"""推送路径校验与协议封包测试。

_send_file 的路径校验是安全边界（防目录穿越），与设备侧 PushServer.ets
的校验规则必须一致 —— 双端任一放行都会造成沙箱逃逸。
"""
import socket
import struct

import pytest

from binrunner import __main__ as br


class FakeSocket:
    """记录 sendall 内容的 socket 替身。"""

    def __init__(self, sink):
        self._sink = sink

    def sendall(self, data):
        self._sink.append(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    """拦截 socket 连接，返回收集到的封包列表。"""
    packets = []
    monkeypatch.setattr(
        br.socket, "create_connection", lambda *a, **kw: FakeSocket(packets)
    )
    return packets


def unpack(packet):
    """解析协议：u32 nameLen | name | u64 payloadSize | payload。"""
    (name_len,) = struct.unpack("<I", packet[:4])
    name = packet[4 : 4 + name_len].decode()
    off = 4 + name_len
    (size,) = struct.unpack("<Q", packet[off : off + 8])
    payload = packet[off + 8 :]
    return name, size, payload


class TestPathValidationRejects:
    """非法远端名必须在客户端就被拒（不依赖设备侧兜底）。"""

    @pytest.mark.parametrize(
        "bad_name",
        [
            "/etc/passwd",          # 绝对路径
            "/",                    # 根
            "..",                   # 上级
            ".",                    # 当前目录
            "a/../../etc/passwd",   # 中间穿越
            "sub/..",               # 结尾穿越
            "a\\b",                 # Windows 分隔符
        ],
    )
    def test_illegal_names_exit(self, bad_name, sent):
        with pytest.raises(SystemExit):
            br._send_file(8888, bad_name, b"data", "UDID")
        assert sent == [], f"非法名 {bad_name!r} 不应发出任何数据"

    def test_name_over_256_bytes_exits(self, sent):
        with pytest.raises(SystemExit):
            br._send_file(8888, "a" * 257, b"data", "UDID")
        assert sent == []

    def test_multibyte_name_length_counted_in_bytes(self, sent):
        """长度上限按 UTF-8 字节数算，不是字符数。"""
        name = "中" * 100  # 300 字节
        assert len(name) < 256 and len(name.encode()) > 256
        with pytest.raises(SystemExit):
            br._send_file(8888, name, b"data", "UDID")
        assert sent == []


class TestPathValidationAccepts:
    """合法名应放行（含子目录）。"""

    @pytest.mark.parametrize(
        "good_name",
        [
            "hello",
            "libfoo.so",
            "models/net.ms",              # 子目录
            "a/b/c/deep.bin",             # 多层
            "name.with.dots",
            "..hidden",                   # 前缀含点但非穿越
            "dir/..name",                 # 段首含点但非穿越
            "a" * 256,                    # 恰好上限
        ],
    )
    def test_legal_names_are_sent(self, good_name, sent):
        br._send_file(8888, good_name, b"payload", "UDID")
        assert len(sent) == 1
        name, _, _ = unpack(sent[0])
        assert name == good_name


class TestProtocolEncoding:
    """封包格式：u32 nameLen | name | u64 payloadSize | payload（小端）。"""

    def test_header_and_payload_layout(self, sent):
        br._send_file(8888, "hello", b"\x00\x01\x02binary", "UDID")
        name, size, payload = unpack(sent[0])
        assert name == "hello"
        assert payload == b"\x00\x01\x02binary"
        assert size == len(payload), "声明长度需与实际 payload 一致"

    def test_empty_payload(self, sent):
        br._send_file(8888, "empty", b"", "UDID")
        name, size, payload = unpack(sent[0])
        assert (name, size, payload) == ("empty", 0, b"")

    def test_multibyte_name_encoded_as_utf8(self, sent):
        br._send_file(8888, "模型.ms", b"x", "UDID")
        packet = sent[0]
        (name_len,) = struct.unpack("<I", packet[:4])
        assert name_len == len("模型.ms".encode()), "nameLen 应为字节数"
        name, _, _ = unpack(packet)
        assert name == "模型.ms"

    def test_binary_payload_not_mangled(self, sent):
        """二进制内容必须原样传输（含 NUL 和非 UTF-8 字节）。"""
        payload = bytes(range(256))
        br._send_file(8888, "raw.bin", payload, "UDID")
        _, size, got = unpack(sent[0])
        assert got == payload
        assert size == 256


class TestSendRetry:
    """首连失败时拉起 App 重试一次。"""

    def test_retries_once_after_launching_app(self, monkeypatch):
        attempts = []
        launched = []

        def flaky_connect(*a, **kw):
            attempts.append(1)
            if len(attempts) == 1:
                raise OSError("connection refused")
            return FakeSocket([])

        monkeypatch.setattr(br.socket, "create_connection", flaky_connect)
        monkeypatch.setattr(br, "run_hdc", lambda *a, **kw: launched.append(a))
        monkeypatch.setattr(br.time, "sleep", lambda _: None)

        br._send_file(8888, "hello", b"data", "UDID")

        assert len(attempts) == 2, "应重试一次"
        assert launched, "重试前应尝试拉起 App"

    def test_exits_when_both_attempts_fail(self, monkeypatch):
        def always_fail(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr(br.socket, "create_connection", always_fail)
        monkeypatch.setattr(br, "run_hdc", lambda *a, **kw: None)
        monkeypatch.setattr(br.time, "sleep", lambda _: None)

        with pytest.raises(SystemExit):
            br._send_file(8888, "hello", b"data", "UDID")


class TestPushTree:
    """目录递归推送：相对路径保持子目录结构。"""

    def test_relative_paths_preserved(self, tmp_path, sent, monkeypatch):
        monkeypatch.setattr(br, "ensure_forward", lambda *a: None)
        (tmp_path / "top.bin").write_bytes(b"A")
        sub = tmp_path / "models"
        sub.mkdir()
        (sub / "net.ms").write_bytes(b"B")
        (sub / "deep").mkdir()
        (sub / "deep" / "x.dat").write_bytes(b"C")

        br.push_tree("UDID", str(tmp_path), 8888)

        names = {unpack(p)[0] for p in sent}
        assert names == {"top.bin", "models/net.ms", "models/deep/x.dat"}

    def test_payloads_match_file_contents(self, tmp_path, sent, monkeypatch):
        monkeypatch.setattr(br, "ensure_forward", lambda *a: None)
        (tmp_path / "a.bin").write_bytes(b"content-a")

        br.push_tree("UDID", str(tmp_path), 8888)

        name, _, payload = unpack(sent[0])
        assert (name, payload) == ("a.bin", b"content-a")

    def test_empty_dir_sends_nothing(self, tmp_path, sent, monkeypatch, capsys):
        monkeypatch.setattr(br, "ensure_forward", lambda *a: None)
        br.push_tree("UDID", str(tmp_path), 8888)
        assert sent == []
        assert "目录为空" in capsys.readouterr().out

    def test_trailing_slash_does_not_break_relative_paths(
        self, tmp_path, sent, monkeypatch
    ):
        """br push ./dir/ 带尾斜杠时相对路径不应多出前导分隔符。"""
        monkeypatch.setattr(br, "ensure_forward", lambda *a: None)
        (tmp_path / "f.bin").write_bytes(b"x")

        br.push_tree("UDID", str(tmp_path) + "/", 8888)

        name, _, _ = unpack(sent[0])
        assert name == "f.bin"


class TestPushFile:
    """单文件推送。"""

    def test_reads_local_file_and_sends_under_remote_name(
        self, tmp_path, sent, monkeypatch
    ):
        monkeypatch.setattr(br, "ensure_forward", lambda *a: None)
        local = tmp_path / "local-name.bin"
        local.write_bytes(b"payload-bytes")

        br.push_file("UDID", str(local), "remote-name.bin", 8888)

        name, size, payload = unpack(sent[0])
        assert name == "remote-name.bin"
        assert payload == b"payload-bytes"
        assert size == len(b"payload-bytes")

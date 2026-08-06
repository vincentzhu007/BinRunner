"""推送路径校验与协议封包测试（binrunner.push）。

路径校验是安全边界（防目录穿越），与设备侧 PushServer.ets 的校验规则
必须一致 —— 双端任一放行都会造成沙箱逃逸。
"""
import struct

import pytest

from binrunner import push as pushmod
from binrunner.push import (
    collect_tree,
    encode_packet,
    is_safe_remote_name,
    push_file,
    push_tree,
)


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
        pushmod.socket, "create_connection", lambda *a, **kw: FakeSocket(packets)
    )
    return packets


@pytest.fixture
def no_forward(monkeypatch):
    """跳过 hdc fport（不需要真实设备）。"""
    monkeypatch.setattr(pushmod, "ensure_forward", lambda *a: None)


def unpack(packet):
    """解析协议：u32 nameLen | name | u64 payloadSize | payload。"""
    (name_len,) = struct.unpack("<I", packet[:4])
    name = packet[4 : 4 + name_len].decode()
    off = 4 + name_len
    (size,) = struct.unpack("<Q", packet[off : off + 8])
    return name, size, packet[off + 8 :]


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

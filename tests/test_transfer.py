"""传输层公共件测试（binrunner.transfer）。

远端名校验是安全边界（防目录穿越），与设备侧 PushServer.ets 的校验规则必须
一致 —— 双端任一放行都会造成沙箱逃逸。u64 编解码同理：两端字节序写反不会
报错，只会静默传错数据，所以在这里钉住线上字节。
"""
import pytest

from binrunner.protocol import MAX_REMOTE_NAME_BYTES
from binrunner.transfer import (
    fmt_size,
    is_safe_remote_name,
    pack_u64,
    recv_exactly,
    unpack_u64,
    validate_remote_name,
)


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


class TestRemoteNameLength:
    """长度上限按 UTF-8 字节数算，不是字符数。"""

    def test_accepts_name_at_byte_limit(self):
        validate_remote_name("a" * MAX_REMOTE_NAME_BYTES)  # 不应抛出

    def test_rejects_name_over_byte_limit(self):
        with pytest.raises(SystemExit) as ei:
            validate_remote_name("a" * (MAX_REMOTE_NAME_BYTES + 1))
        assert "过长" in str(ei.value)

    def test_multibyte_name_counted_in_bytes(self):
        name = "中" * 100  # 100 字符 / 300 字节
        assert len(name) < MAX_REMOTE_NAME_BYTES < len(name.encode())
        with pytest.raises(SystemExit):
            validate_remote_name(name)

    @pytest.mark.parametrize("bad", ["/abs", "..", "a/../b", "a\\b", ""])
    def test_rejects_unsafe(self, bad):
        with pytest.raises(SystemExit):
            validate_remote_name(bad)


class TestU64:
    """u64 一律小端；两端写反不会报错，只会静默传错数据。"""

    @pytest.mark.parametrize(
        "value", [0, 1, 255, 256, 65535, 1 << 32, (1 << 53) - 1, (1 << 64) - 1]
    )
    def test_round_trip(self, value):
        assert unpack_u64(pack_u64(value)) == value

    def test_little_endian_on_the_wire(self):
        assert pack_u64(1) == b"\x01\x00\x00\x00\x00\x00\x00\x00"
        assert pack_u64(0x0102030405060708) == bytes(range(8, 0, -1))

    def test_always_eight_bytes(self):
        assert len(pack_u64(0)) == 8


class TestFmtSize:
    def test_units_and_boundaries(self):
        assert fmt_size(0) == "0 B"
        assert fmt_size(512) == "512 B"
        assert fmt_size(1024) == "1.0 KiB"
        assert fmt_size(1 << 20) == "1.0 MiB"
        assert fmt_size(1 << 30) == "1.0 GiB"
        assert fmt_size(4 << 30) == "4.0 GiB"


class _FakeSock:
    """按给定片段依次喂给调用方的 socket 替身。

    每个片段只交出前 n 字节，模拟 TCP 不保证消息边界。
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)[:n]


class TestRecvExactly:
    def test_reassembles_across_partial_reads(self):
        assert recv_exactly(_FakeSock([b"ab", b"cd", b"ef"]), 6, "测试") == b"abcdef"

    def test_single_read_when_already_complete(self):
        assert recv_exactly(_FakeSock([b"abcdef"]), 6, "测试") == b"abcdef"

    def test_eof_names_what_was_expected(self):
        with pytest.raises(OSError) as ei:
            recv_exactly(_FakeSock([b"ab"]), 6, "响应头")
        assert "响应头" in str(ei.value)


@pytest.mark.parametrize('path, expected', [
    ('x', 'x'), ('@/x', 'x'), ('bin/x', 'bin/x'), ('@/bin/x', 'bin/x'),
    ('models/x', 'models/x'), ('@/models/x', 'models/x'), ('./a//x', 'a/x'),
])
def test_files_root_paths(path, expected):
    from binrunner.transfer import resolve_remote_name
    assert resolve_remote_name(path) == expected


@pytest.mark.parametrize('path', ['@', '@/', '.', '/', '../x', '@/../x',
                                  '@/a/../x', '@//etc/passwd', 'x\x00y', 'a/@/x'])
def test_reject_invalid_user_paths(path):
    from binrunner.transfer import resolve_remote_name
    with pytest.raises(SystemExit):
        resolve_remote_name(path)

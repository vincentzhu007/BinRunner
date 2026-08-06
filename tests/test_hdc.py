"""hdc 封装与设备选择测试（binrunner.hdc）。"""
import subprocess

import pytest

from binrunner import hdc as hdcmod
from binrunner.config import DEVICE_ENV
from binrunner.hdc import (
    find_hdc,
    hdc_cmd,
    hdc_path,
    list_targets,
    pick_device,
    port_open,
)


@pytest.fixture
def stub_hdc_path(monkeypatch):
    """固定 hdc 路径，避免依赖真实环境。"""
    monkeypatch.setattr(hdcmod, "hdc_path", lambda: "/usr/bin/hdc")


def stub_targets(monkeypatch, stdout):
    """替换 run_hdc，模拟 `hdc list targets` 输出。"""
    monkeypatch.setattr(
        hdcmod, "run_hdc",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout, ""),
    )


class TestFindHdc:
    """hdc 定位：PATH 优先，回退 DevEco 默认路径。"""

    def test_path_lookup_wins(self, monkeypatch):
        monkeypatch.setattr(hdcmod.shutil, "which", lambda _: "/usr/local/bin/hdc")
        assert find_hdc() == "/usr/local/bin/hdc"

    def test_falls_back_to_deveco_path(self, monkeypatch):
        monkeypatch.setattr(hdcmod.shutil, "which", lambda _: None)
        monkeypatch.setattr(
            hdcmod.os.path, "exists", lambda p: p == hdcmod.DEVECO_HDC
        )
        assert find_hdc() == hdcmod.DEVECO_HDC

    def test_exits_when_not_found_anywhere(self, monkeypatch):
        """找不到 hdc 是被测行为本身，而非意外崩溃。"""
        monkeypatch.setattr(hdcmod.shutil, "which", lambda _: None)
        monkeypatch.setattr(hdcmod.os.path, "exists", lambda _: False)
        with pytest.raises(SystemExit):
            find_hdc()


class TestHdcPathCache:
    """hdc 路径惰性解析且只解析一次（import 时不解析，否则单测无法收集）。"""

    def test_resolved_once_and_cached(self, monkeypatch):
        monkeypatch.setattr(hdcmod, "_HDC_CACHE", None)
        calls = []

        def counting_find():
            calls.append(1)
            return "/found/hdc"

        monkeypatch.setattr(hdcmod, "find_hdc", counting_find)
        assert hdc_path() == "/found/hdc"
        assert hdc_path() == "/found/hdc"
        assert len(calls) == 1, "find_hdc 应只调用一次"


class TestHdcCmd:
    """命令行拼装。"""

    def test_udid_inserted_as_t_flag(self, stub_hdc_path):
        assert hdc_cmd("UDID", "shell", "ls") == [
            "/usr/bin/hdc", "-t", "UDID", "shell", "ls",
        ]

    def test_no_udid_omits_t_flag(self, stub_hdc_path):
        assert hdc_cmd(None, "list", "targets") == [
            "/usr/bin/hdc", "list", "targets",
        ]


class TestListTargets:
    """设备列表解析。"""

    def test_parses_single_device(self, monkeypatch):
        stub_targets(monkeypatch, "4VF0225717009856\n")
        assert list_targets() == ["4VF0225717009856"]

    def test_parses_multiple_devices(self, monkeypatch):
        stub_targets(monkeypatch, "DEV_A\nDEV_B\n")
        assert list_targets() == ["DEV_A", "DEV_B"]

    def test_empty_marker_filtered_out(self, monkeypatch):
        """hdc 无设备时输出 [Empty]，不应当成设备名。"""
        stub_targets(monkeypatch, "[Empty]\n")
        assert list_targets() == []

    def test_blank_output_yields_empty_list(self, monkeypatch):
        stub_targets(monkeypatch, "")
        assert list_targets() == []


class TestPickDevice:
    """设备选择优先级：-t 参数 > 环境变量 > 自动检测。"""

    def test_explicit_udid_wins_over_env(self, monkeypatch):
        monkeypatch.setenv(DEVICE_ENV, "FROM_ENV")
        assert pick_device("FROM_ARG") == "FROM_ARG"

    def test_env_used_when_no_arg(self, monkeypatch):
        monkeypatch.setenv(DEVICE_ENV, "FROM_ENV")
        assert pick_device(None) == "FROM_ENV"

    def test_single_device_auto_selected(self, monkeypatch):
        monkeypatch.delenv(DEVICE_ENV, raising=False)
        stub_targets(monkeypatch, "4VF0225717009856\n")
        assert pick_device(None) == "4VF0225717009856"

    def test_multiple_devices_exit_with_hint(self, monkeypatch):
        monkeypatch.delenv(DEVICE_ENV, raising=False)
        stub_targets(monkeypatch, "DEV_A\nDEV_B\n")
        with pytest.raises(SystemExit) as ei:
            pick_device(None)
        assert "-t" in str(ei.value), "错误信息应提示用 -t 指定"

    def test_no_device_exits_when_required(self, monkeypatch):
        monkeypatch.delenv(DEVICE_ENV, raising=False)
        stub_targets(monkeypatch, "[Empty]\n")
        with pytest.raises(SystemExit):
            pick_device(None)

    def test_no_device_returns_none_when_optional(self, monkeypatch):
        """br version 无设备时也要能显示 CLI 版本。"""
        monkeypatch.delenv(DEVICE_ENV, raising=False)
        stub_targets(monkeypatch, "[Empty]\n")
        assert pick_device(None, required=False) is None

    def test_multiple_devices_ok_when_optional(self, monkeypatch):
        monkeypatch.delenv(DEVICE_ENV, raising=False)
        stub_targets(monkeypatch, "DEV_A\nDEV_B\n")
        assert pick_device(None, required=False) == "DEV_A"


class TestPortOpen:
    """端口探测（判断 fport 是否已建立）。"""

    def test_returns_true_when_connectable(self, monkeypatch):
        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            hdcmod.socket, "create_connection", lambda *a, **kw: FakeConn()
        )
        assert port_open(8888) is True

    def test_returns_false_on_connection_error(self, monkeypatch):
        def refuse(*a, **kw):
            raise OSError("connection refused")

        monkeypatch.setattr(hdcmod.socket, "create_connection", refuse)
        assert port_open(8888) is False


class TestRunHdc:
    """命令执行与错误处理。"""

    def test_returns_completed_process(self, monkeypatch, stub_hdc_path):
        monkeypatch.setattr(
            hdcmod.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, "out", ""),
        )
        assert hdcmod.run_hdc("UDID", "shell", "ls").stdout == "out"

    def test_exits_on_nonzero_when_check_true(self, monkeypatch, stub_hdc_path):
        monkeypatch.setattr(
            hdcmod.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "boom"),
        )
        with pytest.raises(SystemExit):
            hdcmod.run_hdc("UDID", "shell", "bad", check=True)

    def test_tolerates_nonzero_when_check_false(self, monkeypatch, stub_hdc_path):
        monkeypatch.setattr(
            hdcmod.subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "boom"),
        )
        r = hdcmod.run_hdc("UDID", "shell", "bad", check=False)
        assert r.returncode == 1


class TestEnsureForward:
    """fport 幂等建立。"""

    def test_skips_when_port_already_open(self, monkeypatch):
        monkeypatch.setattr(hdcmod, "port_open", lambda _: True)
        spawned = []
        monkeypatch.setattr(
            hdcmod.subprocess, "Popen", lambda *a, **kw: spawned.append(a)
        )
        hdcmod.ensure_forward("UDID", 8888)
        assert spawned == [], "端口已通时不应重复建立转发"

    def test_spawns_detached_when_port_closed(self, monkeypatch, stub_hdc_path):
        states = iter([False, True])  # 首次探测失败，建立后成功
        monkeypatch.setattr(hdcmod, "port_open", lambda _: next(states, True))
        spawned = []
        monkeypatch.setattr(
            hdcmod.subprocess, "Popen", lambda *a, **kw: spawned.append(kw)
        )
        monkeypatch.setattr(hdcmod.time, "sleep", lambda _: None)

        hdcmod.ensure_forward("UDID", 8888)

        assert len(spawned) == 1
        assert spawned[0].get("start_new_session") is True, "fport 需脱离会话驻留"

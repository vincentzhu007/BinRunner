"""设备侧安装与版本管理测试（binrunner.provision）。"""
import subprocess

import pytest

from binrunner import __version__
from binrunner import provision as prov
from binrunner.provision import cmd_setup, cmd_version, ensure_app, get_device_version


def stub_dump(monkeypatch, stdout, stderr=""):
    """替换 run_hdc，模拟 `bm dump -n <bundle>` 输出。"""
    monkeypatch.setattr(
        prov, "run_hdc",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout, stderr),
    )


@pytest.fixture
def record_hdc(monkeypatch):
    """记录所有 run_hdc 调用，返回参数列表。"""
    calls = []
    monkeypatch.setattr(prov, "run_hdc", lambda *a, **kw: calls.append(a))
    return calls


@pytest.fixture
def stub_bundled(monkeypatch):
    """避免依赖真实构建产物。"""
    monkeypatch.setattr(prov, "_find_bundled", lambda n: f"/fake/{n}")


@pytest.fixture
def record_verify(monkeypatch):
    """拦截验证步骤（需要真实设备）。"""
    calls = []
    monkeypatch.setattr(prov, "_verify", lambda *a: calls.append(a))
    return calls


class TestGetDeviceVersion:
    """从 bm dump 输出解析 versionName。"""

    def test_parses_version_name(self, monkeypatch):
        stub_dump(monkeypatch, '''
{
    "name": "com.example.binrunner",
    "versionName": "1.0.0",
    "versionCode": 1000000
}
''')
        assert get_device_version("UDID") == "1.0.0"

    def test_returns_none_when_not_installed(self, monkeypatch):
        stub_dump(
            monkeypatch, "",
            "error: failed to get information and the parameters may be wrong.",
        )
        assert get_device_version("UDID") is None

    def test_reads_from_stderr_too(self, monkeypatch):
        """bm dump 有时把内容写到 stderr。"""
        stub_dump(monkeypatch, "", '"versionName": "2.1.0"')
        assert get_device_version("UDID") == "2.1.0"

    def test_multi_segment_version(self, monkeypatch):
        stub_dump(monkeypatch, '"versionName": "1.2.3-beta.4"')
        assert get_device_version("UDID") == "1.2.3-beta.4"


class TestCmdVersion:
    """br version 输出。"""

    def test_prints_cli_version_without_device(self, capsys):
        assert cmd_version(None) == 0
        out = capsys.readouterr().out
        assert __version__ in out
        assert "Device HAP" not in out, "无设备时不应打印设备行"

    def test_prints_device_version_when_installed(self, monkeypatch, capsys):
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.0.0")
        assert cmd_version("UDID") == 0
        out = capsys.readouterr().out
        assert __version__ in out
        assert "1.0.0" in out
        assert prov.BUNDLE in out

    def test_reports_not_installed(self, monkeypatch, capsys):
        monkeypatch.setattr(prov, "get_device_version", lambda _: None)
        assert cmd_version("UDID") == 0
        assert "not installed" in capsys.readouterr().out


class TestEnsureApp:
    """首次使用自动安装（对用户透明）。"""

    def test_skips_install_when_already_present(self, monkeypatch, record_hdc):
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.0.0")
        ensure_app("UDID")
        assert record_hdc == [], "已安装时不应执行任何 hdc 命令"

    def test_installs_and_verifies_when_absent(
        self, monkeypatch, stub_bundled, record_hdc, record_verify, capsys
    ):
        monkeypatch.setattr(prov, "get_device_version", lambda _: None)

        ensure_app("UDID")

        joined = " ".join(" ".join(map(str, c)) for c in record_hdc)
        assert "bm install" in joined, "应执行安装"
        assert record_verify, "安装后应跑验证"
        assert "首次使用" in capsys.readouterr().out

    def test_first_install_without_replace_flag(
        self, monkeypatch, stub_bundled, record_hdc, record_verify
    ):
        """首次安装不带 -r（无既有数据需保留）。"""
        monkeypatch.setattr(prov, "get_device_version", lambda _: None)

        ensure_app("UDID")

        install = [c for c in record_hdc if any("bm install" in str(x) for x in c)]
        assert install, "应有安装命令"
        assert " -r" not in " ".join(map(str, install[0]))


class TestCmdSetup:
    """br setup 安装/升级。"""

    def test_skips_when_installed_without_reinstall(
        self, monkeypatch, stub_bundled, record_hdc, capsys
    ):
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.0.0")

        assert cmd_setup("UDID", reinstall=False) == 0
        assert record_hdc == []
        assert "--reinstall" in capsys.readouterr().out, "应提示如何强制升级"

    def test_reinstall_flag_forces_install_with_replace(
        self, monkeypatch, stub_bundled, record_hdc, record_verify
    ):
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.0.0")

        assert cmd_setup("UDID", reinstall=True) == 0

        joined = " ".join(" ".join(map(str, c)) for c in record_hdc)
        assert "bm install" in joined
        assert "-r" in joined, "升级应带 -r 保留数据"

    def test_fresh_install_when_not_present(
        self, monkeypatch, stub_bundled, record_hdc, record_verify, capsys
    ):
        monkeypatch.setattr(prov, "get_device_version", lambda _: None)

        assert cmd_setup("UDID") == 0

        assert "安装 BinRunner" in capsys.readouterr().out
        assert record_verify, "安装后应验证"

    def test_upgrade_message_shows_both_versions(
        self, monkeypatch, stub_bundled, record_hdc, record_verify, capsys
    ):
        monkeypatch.setattr(prov, "get_device_version", lambda _: "0.9.0")

        cmd_setup("UDID", reinstall=True)

        out = capsys.readouterr().out
        assert "0.9.0" in out and __version__ in out, "升级提示应含新旧版本"


class TestInstallHapCleanup:
    """中转文件清理（安装失败也要清）。"""

    def test_stage_dir_cleaned_on_success(self, monkeypatch):
        calls = []
        monkeypatch.setattr(prov, "run_hdc", lambda *a, **kw: calls.append(a))

        prov._install_hap("UDID", "/fake/binrunner.hap", replace=False)

        joined = " ".join(" ".join(map(str, c)) for c in calls)
        assert "rm -rf" in joined, "应清理中转目录"

    def test_stage_dir_cleaned_when_install_fails(self, monkeypatch):
        """安装失败时仍需清理，否则设备残留垃圾文件。"""
        calls = []

        def failing_run(*a, **kw):
            calls.append(a)
            if any("bm install" in str(x) for x in a):
                raise SystemExit("install failed")

        monkeypatch.setattr(prov, "run_hdc", failing_run)

        with pytest.raises(SystemExit):
            prov._install_hap("UDID", "/fake/binrunner.hap", replace=False)

        joined = " ".join(" ".join(map(str, c)) for c in calls)
        assert "rm -rf" in joined, "失败路径也应清理中转目录"

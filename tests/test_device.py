"""设备选择与版本解析测试。"""
import subprocess

import pytest

from binrunner import __main__ as br


def fake_targets(monkeypatch, stdout):
    """替换 run_hdc，模拟 `hdc list targets` 输出。"""
    monkeypatch.setattr(
        br, "run_hdc",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout, ""),
    )


class TestPickDevice:
    """设备选择优先级：-t 参数 > BINRUNNER_DEVICE > 自动检测。"""

    def test_explicit_udid_wins_over_env(self, monkeypatch):
        monkeypatch.setenv("BINRUNNER_DEVICE", "FROM_ENV")
        assert br.pick_device("FROM_ARG") == "FROM_ARG"

    def test_env_used_when_no_arg(self, monkeypatch):
        monkeypatch.setenv("BINRUNNER_DEVICE", "FROM_ENV")
        assert br.pick_device(None) == "FROM_ENV"

    def test_single_device_auto_selected(self, monkeypatch):
        monkeypatch.delenv("BINRUNNER_DEVICE", raising=False)
        fake_targets(monkeypatch, "4VF0225717009856\n")
        assert br.pick_device(None) == "4VF0225717009856"

    def test_multiple_devices_exit_with_hint(self, monkeypatch):
        monkeypatch.delenv("BINRUNNER_DEVICE", raising=False)
        fake_targets(monkeypatch, "DEV_A\nDEV_B\n")
        with pytest.raises(SystemExit) as ei:
            br.pick_device(None)
        assert "-t" in str(ei.value), "错误信息应提示用 -t 指定"

    def test_no_device_exits_when_required(self, monkeypatch):
        monkeypatch.delenv("BINRUNNER_DEVICE", raising=False)
        fake_targets(monkeypatch, "[Empty]\n")
        with pytest.raises(SystemExit):
            br.pick_device(None)

    def test_no_device_returns_none_when_optional(self, monkeypatch):
        """br version 无设备时也要能显示 CLI 版本。"""
        monkeypatch.delenv("BINRUNNER_DEVICE", raising=False)
        fake_targets(monkeypatch, "[Empty]\n")
        assert br.pick_device(None, required=False) is None

    def test_multiple_devices_ok_when_optional(self, monkeypatch):
        monkeypatch.delenv("BINRUNNER_DEVICE", raising=False)
        fake_targets(monkeypatch, "DEV_A\nDEV_B\n")
        assert br.pick_device(None, required=False) == "DEV_A"

    def test_empty_output_treated_as_no_device(self, monkeypatch):
        monkeypatch.delenv("BINRUNNER_DEVICE", raising=False)
        fake_targets(monkeypatch, "")
        with pytest.raises(SystemExit):
            br.pick_device(None)


class TestHdcCmd:
    """hdc 命令行拼装。"""

    @pytest.fixture(autouse=True)
    def stub_hdc_path(self, monkeypatch):
        monkeypatch.setattr(br, "hdc_path", lambda: "/usr/bin/hdc")

    def test_udid_inserted_as_t_flag(self):
        assert br.hdc_cmd("UDID", "shell", "ls") == [
            "/usr/bin/hdc", "-t", "UDID", "shell", "ls",
        ]

    def test_no_udid_omits_t_flag(self):
        assert br.hdc_cmd(None, "list", "targets") == [
            "/usr/bin/hdc", "list", "targets",
        ]


class TestHdcPathCache:
    """hdc 路径惰性解析且只解析一次。"""

    def test_resolved_once_and_cached(self, monkeypatch):
        monkeypatch.setattr(br, "_HDC_CACHE", None)
        calls = []

        def counting_find():
            calls.append(1)
            return "/found/hdc"

        monkeypatch.setattr(br, "find_hdc", counting_find)
        assert br.hdc_path() == "/found/hdc"
        assert br.hdc_path() == "/found/hdc"
        assert len(calls) == 1, "find_hdc 应只调用一次"


class TestFindHdc:
    """hdc 定位：PATH 优先，回退 DevEco 默认路径。"""

    def test_path_lookup_wins(self, monkeypatch):
        monkeypatch.setattr(br.shutil, "which", lambda _: "/usr/local/bin/hdc")
        assert br.find_hdc() == "/usr/local/bin/hdc"

    def test_falls_back_to_deveco_path(self, monkeypatch):
        monkeypatch.setattr(br.shutil, "which", lambda _: None)
        monkeypatch.setattr(br.os.path, "exists", lambda p: p == br.DEVECO_HDC)
        assert br.find_hdc() == br.DEVECO_HDC

    def test_exits_when_not_found_anywhere(self, monkeypatch):
        monkeypatch.setattr(br.shutil, "which", lambda _: None)
        monkeypatch.setattr(br.os.path, "exists", lambda _: False)
        with pytest.raises(SystemExit):
            br.find_hdc()


class TestDeviceVersion:
    """从 bm dump 输出解析 versionName。"""

    def _stub_dump(self, monkeypatch, stdout, stderr=""):
        monkeypatch.setattr(
            br, "run_hdc",
            lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout, stderr),
        )

    def test_parses_version_name(self, monkeypatch):
        self._stub_dump(monkeypatch, '''
{
    "name": "com.example.binrunner",
    "versionName": "1.0.0",
    "versionCode": 1000000
}
''')
        assert br._get_device_version("UDID") == "1.0.0"

    def test_returns_none_when_not_installed(self, monkeypatch):
        self._stub_dump(
            monkeypatch, "",
            "error: failed to get information and the parameters may be wrong.",
        )
        assert br._get_device_version("UDID") is None

    def test_reads_from_stderr_too(self, monkeypatch):
        """bm dump 有时把内容写到 stderr。"""
        self._stub_dump(monkeypatch, "", '"versionName": "2.1.0"')
        assert br._get_device_version("UDID") == "2.1.0"

    def test_multi_segment_version(self, monkeypatch):
        self._stub_dump(monkeypatch, '"versionName": "1.2.3-beta.4"')
        assert br._get_device_version("UDID") == "1.2.3-beta.4"


class TestCmdVersion:
    """br version 输出。"""

    def test_prints_cli_version_without_device(self, capsys):
        assert br.cmd_version(None) == 0
        out = capsys.readouterr().out
        assert br.VERSION in out
        assert "Device HAP" not in out, "无设备时不应打印设备行"

    def test_prints_device_version_when_installed(self, monkeypatch, capsys):
        monkeypatch.setattr(br, "_get_device_version", lambda _: "1.0.0")
        assert br.cmd_version("UDID") == 0
        out = capsys.readouterr().out
        assert br.VERSION in out
        assert "1.0.0" in out
        assert br.BUNDLE in out

    def test_reports_not_installed(self, monkeypatch, capsys):
        monkeypatch.setattr(br, "_get_device_version", lambda _: None)
        assert br.cmd_version("UDID") == 0
        assert "not installed" in capsys.readouterr().out


class TestEnsureApp:
    """首次使用自动安装 HAP。"""

    def test_skips_install_when_already_present(self, monkeypatch):
        monkeypatch.setattr(br, "_get_device_version", lambda _: "1.0.0")
        calls = []
        monkeypatch.setattr(br, "run_hdc", lambda *a, **kw: calls.append(a))
        br.ensure_app("UDID")
        assert calls == [], "已安装时不应执行任何 hdc 命令"

    def test_installs_and_verifies_when_absent(self, monkeypatch, capsys):
        monkeypatch.setattr(br, "_get_device_version", lambda _: None)
        monkeypatch.setattr(br, "_find_bundled", lambda n: f"/fake/{n}")
        hdc_calls = []
        monkeypatch.setattr(br, "run_hdc", lambda *a, **kw: hdc_calls.append(a))
        verified = []
        monkeypatch.setattr(br, "_setup_verify", lambda *a: verified.append(a))

        br.ensure_app("UDID")

        joined = " ".join(" ".join(str(x) for x in c) for c in hdc_calls)
        assert "bm install" in joined, "应执行安装"
        assert verified, "安装后应跑验证"
        assert "首次使用" in capsys.readouterr().out


class TestCmdSetup:
    """br setup 安装/升级。"""

    def test_skips_when_installed_without_reinstall(self, monkeypatch, capsys):
        monkeypatch.setattr(br, "_find_bundled", lambda n: f"/fake/{n}")
        monkeypatch.setattr(br, "_get_device_version", lambda _: "1.0.0")
        calls = []
        monkeypatch.setattr(br, "run_hdc", lambda *a, **kw: calls.append(a))

        assert br.cmd_setup("UDID", reinstall=False) == 0
        assert calls == []
        assert "--reinstall" in capsys.readouterr().out, "应提示如何强制升级"

    def test_reinstall_flag_forces_install(self, monkeypatch):
        monkeypatch.setattr(br, "_find_bundled", lambda n: f"/fake/{n}")
        monkeypatch.setattr(br, "_get_device_version", lambda _: "1.0.0")
        calls = []
        monkeypatch.setattr(br, "run_hdc", lambda *a, **kw: calls.append(a))
        monkeypatch.setattr(br, "_setup_verify", lambda *a: None)

        assert br.cmd_setup("UDID", reinstall=True) == 0
        joined = " ".join(" ".join(str(x) for x in c) for c in calls)
        assert "bm install" in joined
        assert "-r" in joined, "升级应带 -r 保留数据"

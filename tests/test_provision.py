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


class TestVerify:
    """全链路验证以 hello 的退出码为证据（设备交互全部打桩）。"""

    @pytest.fixture
    def stub_chain(self, monkeypatch):
        """打桩 _verify 的设备交互，返回 (可改写退出码的容器, cmd_run 调用记录)。"""
        monkeypatch.setattr(prov, "_find_bundled", lambda n: f"/fake/{n}")
        monkeypatch.setattr(prov, "ensure_forward", lambda *a: None)
        monkeypatch.setattr(prov, "push_file", lambda *a: None)
        code = [prov._HELLO_EXIT]
        calls = []

        def fake_cmd_run(udid, cmdline, timeout):
            calls.append((udid, cmdline, timeout))
            return code[0]

        monkeypatch.setattr(prov, "cmd_run", fake_cmd_run)
        return code, calls

    def test_passes_when_exit_code_matches(self, stub_chain, capsys):
        code, calls = stub_chain
        code[0] = 42  # examples/hello/hello.c 就是这么返回的

        prov._verify("UDID", 1234)

        assert "全链路验证通过" in capsys.readouterr().out
        assert calls and calls[0][:2] == ("UDID", "hello"), "应经 cmd_run 执行 hello"

    def test_fails_and_hides_success_on_other_exit_code(self, stub_chain, capsys):
        code, _ = stub_chain
        code[0] = 0  # 二进制未执行，或退出码未透传

        with pytest.raises(SystemExit) as exc:
            prov._verify("UDID", 1234)

        assert "全链路验证通过" not in capsys.readouterr().out
        assert "42" in str(exc.value), "失败信息应给出期望退出码"

    @pytest.mark.parametrize("bad", [0, 1, 41, 43, 255])
    def test_rejects_any_other_exit_code(self, stub_chain, capsys, bad):
        code, _ = stub_chain
        code[0] = bad

        with pytest.raises(SystemExit):
            prov._verify("UDID", 1234)

        assert "全链路验证通过" not in capsys.readouterr().out


class TestEnsureApp:
    """首次使用自动安装（对用户透明）。"""

    def test_skips_install_when_already_present(self, monkeypatch, record_hdc):
        monkeypatch.setattr(prov, "get_device_version", lambda _: __version__)
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

    def test_warns_when_device_version_differs(self, monkeypatch, record_hdc, capsys):
        """版本不一致必须提示（原则：CLI 与 HAP 需一致），且只警告不自动升级。"""
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.5.1")

        ensure_app("UDID")

        captured = capsys.readouterr()
        assert record_hdc == [], "只警告，不应自动安装或升级"
        assert "1.5.1" in captured.err and __version__ in captured.err
        assert "--reinstall" in captured.err, "应给出设备侧升级方式"
        assert captured.out == "", "警告走 stderr，不得污染 stdout"

    def test_silent_when_versions_match(self, monkeypatch, record_hdc, capsys):
        monkeypatch.setattr(prov, "get_device_version", lambda _: __version__)

        ensure_app("UDID")

        captured = capsys.readouterr()
        assert record_hdc == []
        assert captured.out == "" and captured.err == "", "版本一致时不应有任何输出"

    def test_warns_also_when_device_is_newer(self, monkeypatch, record_hdc, capsys):
        """设备比 CLI 新同样不一致（此时该升级 CLI），提示里两种升级方式都给。"""
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.5.9")

        ensure_app("UDID")

        err = capsys.readouterr().err
        assert "1.5.9" in err and "pip install -U" in err


class TestCmdSetup:
    """br setup 安装/升级。"""

    def test_skips_when_installed_without_reinstall(
        self, monkeypatch, stub_bundled, record_hdc, capsys
    ):
        monkeypatch.setattr(prov, "get_device_version", lambda _: "1.0.0")

        assert cmd_setup("UDID", reinstall=False) == 0
        assert record_hdc == []
        out = capsys.readouterr().out
        assert "--reinstall" in out, "应提示设备侧升级方式"
        assert "1.0.0" in out and __version__ in out, "应同时给出设备版本与 CLI 版本"

    def test_reports_consistent_without_upgrade_hint_when_versions_match(
        self, monkeypatch, stub_bundled, record_hdc, capsys
    ):
        """版本一致时明确说「无需升级」，不再提示 --reinstall。"""
        monkeypatch.setattr(prov, "get_device_version", lambda _: __version__)

        assert cmd_setup("UDID", reinstall=False) == 0

        out = capsys.readouterr().out
        assert record_hdc == []
        assert "一致" in out and "无需升级" in out
        assert "--reinstall" not in out

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

    def test_failed_verify_blocks_success(
        self, monkeypatch, stub_bundled, record_hdc, capsys
    ):
        """hello 退出码不符时，安装成功也不能报『全链路验证通过』。"""
        monkeypatch.setattr(prov, "get_device_version", lambda _: None)
        monkeypatch.setattr(prov, "ensure_forward", lambda *a: None)
        monkeypatch.setattr(prov, "push_file", lambda *a: None)
        monkeypatch.setattr(prov, "cmd_run", lambda *a: 1)  # 期望 42

        with pytest.raises(SystemExit):
            cmd_setup("UDID")

        assert "全链路验证通过" not in capsys.readouterr().out


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


@pytest.mark.parametrize('installed', ['1.0.0', '1.4.1', '1.6.0', '2.0.0'])
def test_incompatible_path_version_is_blocked(monkeypatch, record_hdc, installed):
    monkeypatch.setattr(prov, 'get_device_version', lambda _: installed)
    with pytest.raises(SystemExit, match='路径规格不兼容'):
        ensure_app('UDID')
    assert record_hdc == []

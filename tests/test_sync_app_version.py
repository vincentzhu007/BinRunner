"""版本联动脚本（scripts/sync_app_version.py）的测试。

存在的理由：`hap-sign.yml` 直接调 hvigorw、**不走 build.sh**，因此不会自动同步 app.json5。
一旦 `__version__` 变了而 app.json5 没跟着变，nightly 就会发布 versionName 谎报的 HAP，
而 `br setup` 会因「同版本」直接返回、不安装 —— 用户拿不到新协议。
"""
import sys

import pytest

from scripts import sync_app_version as sav

_INIT_PY = '"""doc"""\n\n__version__ = "{ver}"\n'
_APP_JSON5 = """{{
  "app": {{
    "bundleName": "com.example.binrunner",
    "versionCode": {code},
    "versionName": "{name}",
  }}
}}
"""


@pytest.fixture
def repo(monkeypatch, tmp_path):
    """把脚本的路径常量指向临时仓库，避免碰真实仓库文件。"""
    version_file = tmp_path / "binrunner" / "__init__.py"
    version_file.parent.mkdir(parents=True)
    app_json5 = tmp_path / "app" / "AppScope" / "app.json5"
    app_json5.parent.mkdir(parents=True)
    monkeypatch.setattr(sav, "ROOT", tmp_path)
    monkeypatch.setattr(sav, "VERSION_FILE", version_file)
    monkeypatch.setattr(sav, "APP_JSON5", app_json5)
    return tmp_path


def seed(version: str, name: str, code: int) -> None:
    """按给定状态写好临时仓库的两个文件。"""
    sav.VERSION_FILE.write_text(_INIT_PY.format(ver=version), encoding="utf-8")
    sav.APP_JSON5.write_text(_APP_JSON5.format(code=code, name=name), encoding="utf-8")


def run(monkeypatch, *argv) -> None:
    monkeypatch.setattr(sys, "argv", ["sync_app_version.py", *argv])
    sav.main()


class TestCheckMode:
    def test_passes_when_synced(self, repo, monkeypatch):
        seed("1.5.0", "1.5.0", 1005000)
        run(monkeypatch, "--check")  # 不抛即通过

    def test_fails_when_version_name_not_synced(self, repo, monkeypatch):
        seed("1.5.0", "1.4.1", 1005000)
        with pytest.raises(SystemExit) as ei:
            run(monkeypatch, "--check")
        msg = str(ei.value)
        assert "未与版本号同步" in msg
        assert "1.5.0" in msg and "1.4.1" in msg, f"诊断应同时给出当前值与期望值：{msg}"

    def test_fails_when_version_code_not_synced(self, repo, monkeypatch):
        seed("1.5.0", "1.5.0", 1004001)
        with pytest.raises(SystemExit):
            run(monkeypatch, "--check")

    def test_never_writes(self, repo, monkeypatch):
        """--check 必须是只读的：顺手改文件会让 CI 掩盖漂移。"""
        seed("1.5.0", "1.4.1", 1004001)
        before = sav.APP_JSON5.read_text(encoding="utf-8")
        with pytest.raises(SystemExit):
            run(monkeypatch, "--check")
        assert sav.APP_JSON5.read_text(encoding="utf-8") == before

    def test_rejects_version_code_regression(self, repo, monkeypatch):
        """versionCode 回退会让已安装设备无法覆盖安装，两种模式都必须拒绝。"""
        seed("1.5.0", "1.5.0", 1005000)
        with pytest.raises(SystemExit) as ei:
            run(monkeypatch, "--check", "--version", "1.4.9")
        assert "回退" in str(ei.value)


class TestWriteMode:
    def test_syncs_both_fields(self, repo, monkeypatch):
        seed("1.5.0", "1.4.1", 1004001)
        run(monkeypatch)
        text = sav.APP_JSON5.read_text(encoding="utf-8")
        assert '"versionName": "1.5.0"' in text
        assert '"versionCode": 1005000' in text

    def test_is_idempotent(self, repo, monkeypatch):
        seed("1.5.0", "1.5.0", 1005000)
        before = sav.APP_JSON5.read_text(encoding="utf-8")
        run(monkeypatch)
        assert sav.APP_JSON5.read_text(encoding="utf-8") == before


class TestRepoConsistency:
    def test_committed_app_json5_matches_package_version(self):
        """真实仓库的不变量：app.json5 必须与 `__version__` 同步。

        `hap-sign.yml` 不做同步，本测试是 CI 里唯一的早发现渠道（test.yml 会跑它）。
        """
        ver = sav.read_current_version()
        text = sav.APP_JSON5.read_text(encoding="utf-8")
        name = sav._VERSION_NAME_VALUE_RE.search(text)
        code = sav._VERSION_CODE_RE.search(text)
        assert name and code, "app.json5 缺 versionName / versionCode"
        assert name.group(1) == ver, "app.json5 的 versionName 与 __version__ 不一致"
        assert int(code.group(1)) == sav.version_code(ver), (
            "app.json5 的 versionCode 与 __version__ 映射不一致"
        )

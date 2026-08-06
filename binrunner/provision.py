"""设备侧 App 安装与版本管理。

依赖：config, hdc, push。

内嵌资源（HAP + hello 验证二进制）随 pip 包分发；开发模式下回退到工程构建产物。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from binrunner import __version__
from binrunner.config import ABILITY, BUNDLE, DEFAULT_PORT
from binrunner.hdc import ensure_forward, hdc_cmd, run_hdc
from binrunner.push import push_file

# 设备侧临时目录，用于中转 HAP（App 沙箱不可直接写入）
_STAGE_DIR = "/data/local/tmp/br_install"

# 开发模式下 HAP 的构建产物路径（相对工程根）
_DEV_HAP_PATH = "app/entry/build/default/outputs/default/entry-default-signed.hap"

_VERSION_RE = re.compile(r'"versionName":\s*"([^"]+)"')


def _project_root() -> str:
    """工程根目录（开发模式用）。binrunner/ 的上一级。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _find_bundled(name: str) -> str:
    """定位内嵌资源。优先 pip 包内，其次工程目录（开发模式）。"""
    try:
        from importlib import resources

        path = resources.files("binrunner.data").joinpath(name)
        if path.is_file():
            return str(path)
    except (ImportError, ModuleNotFoundError, AttributeError):
        pass

    root = _project_root()
    candidates = [os.path.join(root, "binrunner", "data", name)]
    if name == "binrunner.hap":
        candidates.append(os.path.join(root, _DEV_HAP_PATH))
    elif name == "hello":
        candidates.append(os.path.join(root, "examples", "hello", "hello"))

    for c in candidates:
        if os.path.exists(c):
            return c
    sys.exit(f"找不到 {name}。先运行 ./build.sh 构建，或 pip install binrunner。")


def get_device_version(udid: str) -> str | None:
    """读取设备上已安装的 BinRunner 版本号，未安装返回 None。"""
    r = run_hdc(udid, "shell", f"bm dump -n {BUNDLE}", check=False)
    # bm dump 有时把内容写到 stderr，两者都查
    m = _VERSION_RE.search(r.stdout + r.stderr)
    return m.group(1) if m else None


def _install_hap(udid: str, hap: str, replace: bool) -> None:
    """经 /data/local/tmp 中转安装 HAP。replace=True 时保留应用数据。"""
    base = os.path.basename(hap)
    staged = f"{_STAGE_DIR}/{base}"
    flag = " -r" if replace else ""
    run_hdc(udid, "shell", f"mkdir -p {_STAGE_DIR}")
    try:
        run_hdc(udid, "file", "send", hap, staged)
        run_hdc(udid, "shell", f"bm install -p {staged}{flag}")
    finally:
        # 无论安装成功与否都清理中转文件
        run_hdc(udid, "shell", f"rm -rf {_STAGE_DIR}", check=False)


def _verify(udid: str, port: int) -> None:
    """推送 hello 并执行，确认 安装→推送→执行 全链路可用。"""
    hello = _find_bundled("hello")
    print("[binrunner] 推送验证二进制 hello...")
    ensure_forward(udid, port)
    push_file(udid, hello, "hello", port)

    print("[binrunner] 执行 hello...")
    r = subprocess.run(
        hdc_cmd(udid, "shell", f"aa start -b {BUNDLE} -a {ABILITY} --ps cmd 'hello'"),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if r.returncode != 0:
        print(f"[binrunner] 验证执行失败: {r.stderr}")
    else:
        print("[binrunner] 全链路验证通过。")


def ensure_app(udid: str) -> None:
    """保证设备已安装 BinRunner，未安装则自动安装并验证。

    被所有需要设备侧 App 的命令调用（run/push/ls/rm/logs），对用户透明。
    """
    if get_device_version(udid) is not None:
        return
    print(f"[binrunner] 首次使用，正在安装 BinRunner {__version__} 到设备...")
    _install_hap(udid, _find_bundled("binrunner.hap"), replace=False)
    print("[binrunner] 安装完成。")
    _verify(udid, DEFAULT_PORT)


def cmd_setup(udid: str, reinstall: bool = False) -> int:
    """手动安装/升级 HAP 并验证全链路。"""
    installed = get_device_version(udid)
    if installed and not reinstall:
        print(f"BinRunner {installed} 已安装。使用 --reinstall 覆盖升级。")
        return 0

    if installed:
        print(f"升级 BinRunner: {installed} → {__version__}")
    else:
        print(f"安装 BinRunner {__version__} 到设备...")

    _install_hap(udid, _find_bundled("binrunner.hap"), replace=True)
    print(f"BinRunner {__version__} 安装完成。")
    _verify(udid, DEFAULT_PORT)
    return 0


def cmd_version(udid: str | None = None) -> int:
    """显示 CLI 版本，有设备时附带设备侧版本。"""
    print(f"BinRunner CLI {__version__}")
    if udid:
        ver = get_device_version(udid)
        print(f"Device HAP   {ver} ({BUNDLE})" if ver else "Device HAP   not installed")
    return 0

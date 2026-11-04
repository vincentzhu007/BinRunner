"""设备侧 App 安装与版本管理。

依赖：config, hdc, push, runner。

内嵌资源（HAP + hello 验证二进制）随 pip 包分发；开发模式下回退到工程构建产物。
"""
from __future__ import annotations

import os
import re
import sys

from binrunner import __version__
from binrunner.config import BUNDLE
from binrunner.hdc import ensure_forward, run_hdc
from binrunner.protocol import PORT
from binrunner.push import push_file
from binrunner.runner import cmd_run

# 设备侧临时目录，用于中转 HAP（App 沙箱不可直接写入）
_STAGE_DIR = "/data/local/tmp/br_install"

# 开发模式下 HAP 的构建产物路径（相对工程根）
_DEV_HAP_PATH = "app/entry/build/default/outputs/default/entry-default-signed.hap"

_VERSION_RE = re.compile(r'"versionName":\s*"([^"]+)"')

# examples/hello/hello.c 故意 `return 42`：只有被推送的二进制真的被加载执行，
# 退出码才会原样透传（EACCES / 签名校验 / 加载器失败都得不到该码）。
_HELLO_EXIT = 42


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
    """推送 hello 并执行，确认 安装→推送→执行 全链路可用。

    校验对象是 hello 的退出码而非 `aa start` 的返回码：aa start 成功只说明
    Ability 被拉起，无法证明二进制被加载执行——而这正是本步骤要拦的失败
    （EACCES / 签名错误 / 动态加载器失败）。故复用 cmd_run 的真实执行路径，
    要求退出码精确等于 _HELLO_EXIT。

    代价是比裸 aa start 多一轮 hilog 轮询：起步等待 1s 起，报告回传最多再加
    30s 宽限（见 runner._STARTUP_WAIT / _REPORT_GRACE）。

    退出码不符是致命问题：sys.exit 终止进程，避免调用方（ensure_app /
    cmd_setup）继续打印成功信息。
    """
    hello = _find_bundled("hello")
    print("[binrunner] 推送验证二进制 hello...")
    ensure_forward(udid, port)
    push_file(udid, hello, "hello", port)

    print("[binrunner] 执行 hello...")
    code = cmd_run(udid, "hello", 30)
    if code != _HELLO_EXIT:
        sys.exit(
            f"[binrunner] 验证执行失败：hello 退出码 {code}，期望 {_HELLO_EXIT}"
            "（二进制未被正确加载执行？）"
        )
    print("[binrunner] 全链路验证通过。")


def ensure_app(udid: str) -> None:
    """保证设备已安装 BinRunner，未安装则自动安装并验证。

    被所有需要设备侧 App 的命令调用（run/push/pull/ls/rm/logs），对用户透明。
    已安装时主次版本不同则拒绝执行，补丁版本不同只警告；不自动升级：自动替换已装 HAP 会越过
    `br setup` 的显式契约（`--reinstall` 才是升级路径），也有把可用环境换坏的风险。
    """
    installed = get_device_version(udid)
    if installed is not None:
        if installed.split('.')[:2] != __version__.split('.')[:2]:
            sys.exit(
                f"路径规格不兼容：CLI {__version__} / HAP {installed}。"
                "1.5.x 相对路径基于 files/，1.4.x 及更早版本基于 files/bin/；禁止混用。"
                "请先 br setup --reinstall，或使用匹配设备版本的 CLI（pip install -U binrunner）。"
            )
        if installed != __version__:
            # 走 stderr：stdout 是命令的正式输出（br ls / br run 的内容），可能被捕获
            print(
                f"[binrunner] 警告：设备 HAP {installed} 与 CLI {__version__} 不一致，"
                "两者原则上需一致（协议与报告格式可能不同）；"
                "设备侧升级 `br setup --reinstall`，CLI 侧升级 `pip install -U binrunner`",
                file=sys.stderr,
            )
        return
    print(f"[binrunner] 首次使用，正在安装 BinRunner {__version__} 到设备...")
    _install_hap(udid, _find_bundled("binrunner.hap"), replace=False)
    print("[binrunner] 安装完成。")
    _verify(udid, PORT)


def cmd_setup(udid: str, reinstall: bool = False) -> int:
    """手动安装/升级 HAP 并验证全链路。"""
    installed = get_device_version(udid)
    if installed and not reinstall:
        if installed == __version__:
            print(f"BinRunner {installed} 已安装，与 CLI 版本一致，无需升级。")
        else:
            print(
                f"BinRunner {installed} 已安装，与 CLI {__version__} 不一致"
                "（两者原则上需一致）；用 `br setup --reinstall` 覆盖升级。"
            )
        return 0

    if installed:
        print(f"升级 BinRunner: {installed} → {__version__}")
    else:
        print(f"安装 BinRunner {__version__} 到设备...")

    _install_hap(udid, _find_bundled("binrunner.hap"), replace=True)
    print(f"BinRunner {__version__} 安装完成。")
    _verify(udid, PORT)
    return 0


def cmd_version(udid: str | None = None) -> int:
    """显示 CLI 版本，有设备时附带设备侧版本。"""
    print(f"BinRunner CLI {__version__}")
    if udid:
        ver = get_device_version(udid)
        print(f"Device HAP   {ver} ({BUNDLE})" if ver else "Device HAP   not installed")
    return 0

"""命令行入口：参数解析与命令分发。

依赖全部业务模块，被 __main__.py 与 [project.scripts] 的 br 入口调用。
"""
from __future__ import annotations

import argparse
import os
import sys

from binrunner import __version__
from binrunner.config import DEVICE_ENV
from binrunner.hdc import ensure_forward, pick_device, run_hdc
from binrunner.protocol import PORT
from binrunner.provision import cmd_setup, cmd_version, ensure_app
from binrunner.pull import cmd_pull
from binrunner.push import push_file, push_tree
from binrunner.runner import cmd_run, cmd_logs

USAGE = f"""\
BinRunner {__version__} —— 非 root 鸿蒙手机上跑二进制的全流程封装。

  br devices                      列出已连接设备
  br setup [--reinstall]          安装/升级 HAP 到设备
  br forward                      建立 hdc fport 转发（幂等，后台驻留）
  br push FILE [NAME]             推送文件到 filesDir/
  br push DIR/                    递归推送目录（保持子目录结构）
  br run "hello foo bar"          触发执行并把 stdout/stderr 打印到本地终端
  br ls [path]                    列出设备目录（files 根目录，相对路径基于 files/）
  br rm <path>                    删除文件或目录（默认 files/，递归）
  br pull <remote> [local]        从设备拉取文件到本地
  br logs                         持续跟踪设备上 BinRunner 日志
  br version                      显示 CLI 和设备版本

设备选择：-t UDID，或环境变量 {DEVICE_ENV}；只有一台设备时自动选用。
hdc 不在 PATH 时自动尝试 DevEco Studio 默认安装路径。
"""

# 需要设备侧 App 的命令：执行前自动确保已安装
_NEEDS_APP = {"push", "run", "ls", "rm", "logs", "pull"}


def _execution_timeout(value: str) -> int:
    """设备 native 接口使用正 int32 秒数。"""
    seconds = int(value)
    if not 1 <= seconds <= 2147483647:
        raise argparse.ArgumentTypeError("执行超时必须为 1–2147483647 的整数秒")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="br",
        description=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "-t", dest="udid", metavar="UDID",
        help=f"设备 UDID（或环境变量 {DEVICE_ENV}）",
    )
    ap.add_argument(
        "-p", dest="port", type=int, default=PORT,
        help=f"转发端口（默认 {PORT}）；push / pull / forward 使用",
    )
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("devices", help="列出已连接设备")

    p_setup = sub.add_parser("setup", help="安装/升级 HAP 到设备")
    p_setup.add_argument("--reinstall", action="store_true", help="覆盖安装（保留数据）")

    sub.add_parser("forward", help="建立 hdc fport 转发（幂等）")

    p_push = sub.add_parser(
        "push", help="推送文件/目录到 filesDir/（目录递归，保持子目录结构）"
    )
    p_push.add_argument("local")
    p_push.add_argument("remote", nargs="?", help="远端路径（相对 files/，支持 @/；仅文件需要）")

    p_run = sub.add_parser("run", help="在设备上执行命令并打印输出")
    p_run.add_argument(
        "cmdline",
        help='完整命令行，如 "benchmark --modelFile=@/m.ms"（@ = 沙箱 files 根）',
    )
    p_run.add_argument(
        "--timeout", type=_execution_timeout, default=60,
        help="设备执行超时秒数（正整数，默认 60）；主机额外等待 30 秒供启动和报告回传",
    )

    p_ls = sub.add_parser("ls", help="列出设备目录（默认 files 根目录）")
    p_ls.add_argument("path", nargs="?", help='设备侧路径，如 "@" 或 "@/bin"')

    p_rm = sub.add_parser("rm", help="删除已推送的文件或目录（递归）")
    p_rm.add_argument("path", help='设备侧路径，如 "hello"、"@/bin/subdir"')

    p_pull = sub.add_parser("pull", help="从设备拉取文件到本地")
    p_pull.add_argument("remote", help='设备侧文件名，如 "hello"、"models/net.ms"')
    p_pull.add_argument("local", nargs="?", help="本地路径（默认当前目录）")

    sub.add_parser("logs", help="持续跟踪 BinRunner 日志")
    sub.add_parser("version", help="显示 CLI 和设备版本")
    return ap


def _dispatch_push(udid: str, args: argparse.Namespace) -> int:
    if os.path.isdir(args.local):
        if args.remote:
            sys.exit("目录推送不支持指定 remote 名，子目录结构由本地决定")
        push_tree(udid, args.local, args.port)
    else:
        push_file(udid, args.local, args.remote or os.path.basename(args.local), args.port)
    return 0


def main() -> int:
    args = build_parser().parse_args()
    action = args.action

    # 无需设备即可完成的命令
    if action == "devices":
        print(run_hdc(None, "list", "targets").stdout, end="")
        return 0
    if action == "version":
        # 无设备也要能显示 CLI 版本，故 required=False
        return cmd_version(pick_device(args.udid, required=False))

    udid = pick_device(args.udid)
    if action == "setup":
        return cmd_setup(udid, args.reinstall)
    if action == "forward":
        ensure_forward(udid, args.port)
        print(f"OK: 127.0.0.1:{args.port} -> device:{args.port} ({udid})")
        return 0

    # 以下命令都依赖设备侧 App
    if action in _NEEDS_APP:
        ensure_app(udid)

    if action == "push":
        return _dispatch_push(udid, args)
    if action == "run":
        return cmd_run(udid, args.cmdline, args.timeout)
    if action == "ls":
        return cmd_run(udid, "ls" + (f" {args.path}" if args.path else ""), 30)
    if action == "rm":
        return cmd_run(udid, f"rm {args.path}", 30)
    if action == "pull":
        return cmd_pull(udid, args.remote, args.local, args.port)
    if action == "logs":
        return cmd_logs(udid)
    return 1

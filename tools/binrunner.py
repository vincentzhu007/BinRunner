#!/usr/bin/env python3
"""BinRunner host CLI —— 非 root 鸿蒙手机上跑二进制的全流程封装。

  binrunner devices                      列出已连接设备
  binrunner forward                      建立 hdc fport 转发（幂等，后台驻留）
  binrunner push FILE [NAME]             推送二进制/依赖库/数据文件到 filesDir/bin/
  binrunner run "hello foo bar"          触发执行并把 stdout/stderr 打印到本地终端
  binrunner ls [path]                    列出设备目录（默认沙箱 files 根目录，推送文件在 bin/ 下）
  binrunner logs                         持续跟踪设备上 BinRunner 日志

设备选择：-t UDID，或环境变量 BINRUNNER_DEVICE；只有一台设备时自动选用。
hdc 不在 PATH 时自动尝试 DevEco Studio 默认安装路径。
"""
import argparse
import os
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import time

BUNDLE = "com.example.binrunner"
ABILITY = "EntryAbility"
TAG = "BinRunner"
DEFAULT_PORT = 8888
DEVECO_HDC = "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc"


def find_hdc() -> str:
    hdc = shutil.which("hdc")
    if hdc:
        return hdc
    if os.path.exists(DEVECO_HDC):
        return DEVECO_HDC
    sys.exit("找不到 hdc：请把 DevEco 工具链加入 PATH，或安装 DevEco Studio")


HDC = find_hdc()


def hdc_cmd(udid: str | None, *args: str) -> list[str]:
    cmd = [HDC]
    if udid:
        cmd += ["-t", udid]
    return cmd + list(args)


def run_hdc(udid: str | None, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
    p = subprocess.run(hdc_cmd(udid, *args), capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        sys.exit(f"hdc {' '.join(args)} 失败:\n{p.stdout}{p.stderr}")
    return p


def pick_device(udid: str | None) -> str:
    if udid:
        return udid
    env = os.environ.get("BINRUNNER_DEVICE")
    if env:
        return env
    out = run_hdc(None, "list", "targets").stdout.split()
    targets = [t for t in out if t and t != "[Empty]"]
    if not targets:
        sys.exit("没有已连接的设备（hdc list targets 为空）")
    if len(targets) > 1:
        sys.exit(f"多台设备在线，请用 -t 指定：{', '.join(targets)}")
    return targets[0]


# ---------------- forward ----------------

def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def ensure_forward(udid: str, port: int) -> None:
    """fport 是前台进程；作为脱离会话的后台进程驻留，重复调用幂等。"""
    if port_open(port):
        return
    subprocess.Popen(
        hdc_cmd(udid, "fport", f"tcp:{port}", f"tcp:{port}"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    for _ in range(50):
        if port_open(port):
            return
        time.sleep(0.1)
    sys.exit(f"hdc fport 建立失败（127.0.0.1:{port} 一直连不上）")


# ---------------- push ----------------

def push_file(udid: str, local: str, remote: str, port: int) -> None:
    ensure_forward(udid, port)
    with open(local, "rb") as f:
        payload = f.read()
    name = remote.encode()
    if len(name) > 256 or "/" in remote or "\\" in remote or remote in (".", ".."):
        sys.exit(f"非法远端名: {remote!r}")
    packet = struct.pack("<I", len(name)) + name + struct.pack("<Q", len(payload)) + payload
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(packet)
    except OSError as e:
        # 转发通了但 App 没监听：拉一次 App 再试一次
        run_hdc(udid, "shell", f"aa start -b {BUNDLE} -a {ABILITY}", check=False)
        time.sleep(2)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                s.sendall(packet)
        except OSError:
            sys.exit(f"推送失败：{e}\n（已尝试拉起 App；若仍失败，检查 App 是否安装/被杀）")
    print(f"OK: {local} -> filesDir/bin/{remote} ({len(payload)} bytes)")


# ---------------- run ----------------

def cmd_run(udid: str, cmdline: str, timeout: int) -> int:
    # 清掉旧日志，只收集本次输出（失败也无妨，靠 >>> 标记过滤）
    run_hdc(udid, "shell", "hilog -r", check=False)
    run_hdc(udid, "shell", f"aa start -b {BUNDLE} -a {ABILITY} --ps cmd '{cmdline}'")

    proc = subprocess.Popen(
        hdc_cmd(udid, "shell", "hilog"),
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )
    started = False
    report_lines: list[str] = []
    parts: dict[int, str] = {}   # 超长行的 [i/n] 分段缓存
    parts_total = 0
    done = False
    deadline = time.time() + timeout
    try:
        assert proc.stdout is not None
        while not done:
            r, _, _ = select.select([proc.stdout], [], [], max(deadline - time.time(), 0.01))
            if not r:
                print(f"[binrunner] 等待输出超时（{timeout}s）", file=sys.stderr)
                return 1
            raw = proc.stdout.readline()
            if raw == b"":
                break  # hilog 流结束（设备掉线等）
            # 其他进程的日志可能含非 UTF-8 字节，宽容解码
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            # 设备端保证单条日志无内嵌换行：每行即一条完整记录
            m = re.search(r"BinRunner: (.*)", line)
            if not m:
                continue
            body = m.group(1)
            if not started:
                # 设备端已清过日志，第一条 >>> 即本次执行
                # （名字可能是 ~ 展开后的绝对路径，不做名字匹配）
                if body.startswith(">>> exec "):
                    started = True
                continue
            if body == "<<< END":
                done = True
                continue
            if body == "<<<":
                report_lines.append("")  # 空行（hilog 可能裁掉尾部空格）
                continue
            if not body.startswith("<<< "):
                continue  # 诊断行（resolved via... 等），不进报告
            content = body[4:]
            cm = re.match(r"\[(\d+)/(\d+)\] (.*)", content)
            if cm:
                # 单行超 900 字符的分段：分段间无换行，直接拼接
                idx, parts_total = int(cm.group(1)), int(cm.group(2))
                parts[idx] = cm.group(3)
                if len(parts) == parts_total:
                    report_lines.append("".join(parts[i] for i in range(1, parts_total + 1)))
                    parts = {}
            else:
                report_lines.append(content)
    finally:
        proc.kill()

    if not done and not report_lines:
        print("[binrunner] 没收到执行报告（App 未运行或 cmd 未触发？）", file=sys.stderr)
        return 1
    report = "\n".join(report_lines)
    print(report)
    em = re.search(r"exit=(-?\d+)", report_lines[0] if report_lines else "")
    return int(em.group(1)) if em else 0


def cmd_logs(udid: str) -> int:
    return subprocess.call(hdc_cmd(udid, "shell", f"hilog | grep {TAG}"))


# ---------------- main ----------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="binrunner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", dest="udid", metavar="UDID", help="设备 UDID（或环境变量 BINRUNNER_DEVICE）")
    ap.add_argument("-p", dest="port", type=int, default=DEFAULT_PORT, help="转发端口（默认 8888）")
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("devices", help="列出已连接设备")
    sub.add_parser("forward", help="建立 hdc fport 转发（幂等）")

    p_push = sub.add_parser("push", help="推送文件到 filesDir/bin/")
    p_push.add_argument("local")
    p_push.add_argument("remote", nargs="?", help="远端名（默认取本地文件名）")

    p_run = sub.add_parser("run", help="在设备上执行命令并打印输出")
    p_run.add_argument("cmdline", help='完整命令行，如 "benchmark --modelFile=@/m.ms"（@ = 沙箱 files 根）')
    p_run.add_argument("--timeout", type=int, default=60, help="等待输出的秒数（默认 60）")

    p_ls = sub.add_parser("ls", help="列出设备目录（默认 files 根目录）")
    p_ls.add_argument("path", nargs="?", help='设备侧路径，如 "@" 或 "@/bin"')

    sub.add_parser("logs", help="持续跟踪 BinRunner 日志")
    args = ap.parse_args()

    if args.action == "devices":
        print(run_hdc(None, "list", "targets").stdout, end="")
        return 0

    udid = pick_device(args.udid)
    if args.action == "forward":
        ensure_forward(udid, args.port)
        print(f"OK: 127.0.0.1:{args.port} -> device:{args.port} ({udid})")
        return 0
    if args.action == "push":
        remote = args.remote or os.path.basename(args.local)
        push_file(udid, args.local, remote, args.port)
        return 0
    if args.action == "run":
        return cmd_run(udid, args.cmdline, args.timeout)
    if args.action == "ls":
        cmdline = "ls" + (f" {args.path}" if args.path else "")
        return cmd_run(udid, cmdline, 30)
    if args.action == "logs":
        return cmd_logs(udid)
    return 1


if __name__ == "__main__":
    sys.exit(main())

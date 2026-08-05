#!/usr/bin/env python3
"""BinRunner host CLI —— 非 root 鸿蒙手机上跑二进制的全流程封装。

  binrunner devices                      列出已连接设备
  binrunner forward                      建立 hdc fport 转发（幂等，后台驻留）
  binrunner push FILE [NAME]             推送文件到 filesDir/bin/
  binrunner push DIR/                    递归推送目录（保持子目录结构）
  binrunner run "hello foo bar"          触发执行并把 stdout/stderr 打印到本地终端
  binrunner ls [path]                    列出设备目录（默认沙箱 files 根目录，推送文件在 bin/ 下）
  binrunner logs                         持续跟踪设备上 BinRunner 日志

设备选择：-t UDID，或环境变量 BINRUNNER_DEVICE；只有一台设备时自动选用。
hdc 不在 PATH 时自动尝试 DevEco Studio 默认安装路径。
"""
import argparse
import os
import random
import re
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

def _send_file(port: int, name: str, payload: bytes, udid: str) -> None:
    """发送单个文件到 PushServer。失败时自动拉 App 重试一次。"""
    nameBytes = name.encode()
    if len(nameBytes) > 256:
        sys.exit(f"远端名过长（>256 字节）: {name!r}")
    # 安全检查：拒绝绝对路径和上级引用（与 PushServer 侧一致）
    if name.startswith('/') or '\\' in name or name == '.' or name == '..' or '/../' in name or name.endswith('/..'):
        sys.exit(f"非法远端名: {name!r}")
    packet = struct.pack("<I", len(nameBytes)) + nameBytes + struct.pack("<Q", len(payload)) + payload
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
            s.sendall(packet)
    except OSError as e:
        run_hdc(udid, "shell", f"aa start -b {BUNDLE} -a {ABILITY}", check=False)
        time.sleep(2)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
                s.sendall(packet)
        except OSError:
            sys.exit(f"推送失败：{e}\n（已尝试拉起 App；若仍失败，检查 App 是否安装/被杀）")
    print(f"OK: {name} ({len(payload)} bytes)")


def push_file(udid: str, local: str, remote: str, port: int) -> None:
    """推送单个文件到 filesDir/bin/。"""
    ensure_forward(udid, port)
    with open(local, "rb") as f:
        payload = f.read()
    _send_file(port, remote, payload, udid)


def push_tree(udid: str, local_dir: str, port: int) -> None:
    """递归推送目录树到 filesDir/bin/，保持子目录结构。"""
    ensure_forward(udid, port)
    base = os.path.normpath(local_dir)
    files = []
    for root, __, filenames in os.walk(base):
        for fn in filenames:
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base)
            files.append((full, rel))
    if not files:
        print(f"目录为空: {local_dir}")
        return
    print(f"推送 {len(files)} 个文件...")
    for full, rel in files:
        with open(full, "rb") as f:
            payload = f.read()
        _send_file(port, rel, payload, udid)


# ---------------- run ----------------

def _is_diag_line(body: str) -> bool:
    """过滤 native 侧诊断日志（execv blocked / memfd diag / resolved via 等）。"""
    return (
        body.startswith("exec ") or
        body.startswith("execv ") or
        body.startswith("memfd ") or
        body.startswith("resolved ") or
        body.startswith("hnp ") or
        body.startswith("opendir ") or
        body.startswith("probe") or
        body.startswith("CRASH ") or
        body.startswith("no executable ")
    )


def _parse_hilog_output(output: str, started: bool, report_lines: list[str],
                        parts: dict[int, str], run_id: str = "") -> tuple[bool, bool]:
    """解析 hilog -x 输出中的 BinRunner 行。返回 (started, done)。

    run_id 非空时仅处理含指定 ID 的行（多终端并发场景互不干扰）；
    空串则处理所有 BinRunner 行（兼容手动 aa start 和无 ID 的旧版 App）。
    """
    done = False
    id_marker = f"[{run_id}] " if run_id else ""
    for line in output.split("\n"):
        m = re.search(r"BinRunner: (.*)", line)
        if not m:
            continue
        body = m.group(1)

        # run_id 过滤：非空时跳过不匹配的行
        if id_marker:
            if not body.startswith(id_marker):
                continue
            body = body[len(id_marker):]  # 剥离前缀，后续解析不变

        if not started:
            if body.startswith(">>> exec "):
                started = True
            continue

        if body == "<<< END":
            done = True
            continue

        # 跳过已知的 native 诊断行（以固定前缀开头）
        if _is_diag_line(body):
            continue

        # 剥离可选的 <<< 前缀（仅首行/分段行有）
        content = body
        if content.startswith("<<< "):
            content = content[4:]

        # 处理可选的 [i/n] 分段（单行超 900 字符时启用）
        cm = re.match(r"\[(\d+)/(\d+)\] (.*)", content)
        if cm:
            idx, ptotal = int(cm.group(1)), int(cm.group(2))
            parts[idx] = cm.group(3)
            if len(parts) == ptotal:
                report_lines.append("".join(parts[i] for i in range(1, ptotal + 1)))
                parts.clear()
        else:
            report_lines.append(content)

    return started, done


def _report_is_complete(lines: list[str]) -> bool:
    """检查报告是否结构完整：exit code + stdout 段 + stderr 段。"""
    text = "\n".join(lines)
    has_exit = bool(re.search(r"^exit=-?\d+", text, re.MULTILINE))
    has_stdout = "--- stdout ---" in text
    has_stderr = "--- stderr ---" in text
    return has_exit and has_stdout and has_stderr


def cmd_run(udid: str, cmdline: str, timeout: int) -> int:
    # 生成随机执行 ID，多终端并发时互不干扰
    run_id = ''.join(random.choices('0123456789abcdef', k=8))
    # 清掉旧日志，只收集本次输出
    run_hdc(udid, "shell", "hilog -r", check=False)
    run_hdc(udid, "shell",
            f"aa start -b {BUNDLE} -a {ABILITY} --ps run_id {run_id} --ps cmd '{cmdline}'")

    started = False
    report_lines: list[str] = []
    parts: dict[int, str] = {}   # 超长行的 [i/n] 分段缓存
    done = False
    deadline = time.time() + timeout
    # 首次等待：给 App 冷启动 + 300ms setTimeout + 命令执行预留时间
    time.sleep(1.0)
    while not done:
        remain = deadline - time.time()
        if remain <= 0:
            print(f"[binrunner] 等待输出超时（{timeout}s）", file=sys.stderr)
            return 1
        r = subprocess.run(
            hdc_cmd(udid, "shell", "hilog -x"),
            capture_output=True, timeout=max(remain, 5),
        )
        output = r.stdout.decode("utf-8", errors="replace")
        started, done = _parse_hilog_output(
            output, started, report_lines, parts, run_id,
        )
        if done:
            break
        # <<< END 可能被 hilog 丢弃 → 用报告结构完整性兜底
        if started and _report_is_complete(report_lines):
            break
        time.sleep(0.5)

    if not done and not report_lines:
        print("[binrunner] 没收到执行报告（App 未运行或 cmd 未触发？）", file=sys.stderr)
        return 1
    report = "\n".join(report_lines)
    print(report)
    # exit 行不一定在首行（前面可能有诊断），全文搜索
    em = re.search(r"^exit=(-?\d+)", report, re.MULTILINE)
    return int(em.group(1)) if em else 0


def cmd_logs(udid: str) -> int:
    """持续跟踪设备 BinRunner 日志（Ctrl+C 退出）。

    使用 hilog -x 轮询（非阻塞 dump）代替流式 hilog，避免 pipe 缓冲丢数据。
    seen 集合去重，保证每行只输出一次。
    """
    seen: set[str] = set()
    print(f"[binrunner] 跟踪设备 {udid} 的 BinRunner 日志，Ctrl+C 退出...", file=sys.stderr)
    try:
        while True:
            r = subprocess.run(
                hdc_cmd(udid, "shell", "hilog -x"),
                capture_output=True, timeout=10,
            )
            for line in r.stdout.decode("utf-8", errors="replace").split("\n"):
                if TAG in line and line not in seen:
                    seen.add(line)
                    print(line)
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


# ---------------- main ----------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="binrunner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", dest="udid", metavar="UDID", help="设备 UDID（或环境变量 BINRUNNER_DEVICE）")
    ap.add_argument("-p", dest="port", type=int, default=DEFAULT_PORT, help="转发端口（默认 8888）")
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("devices", help="列出已连接设备")
    sub.add_parser("forward", help="建立 hdc fport 转发（幂等）")

    p_push = sub.add_parser("push", help="推送文件/目录到 filesDir/bin/（目录递归，保持子目录结构）")
    p_push.add_argument("local")
    p_push.add_argument("remote", nargs="?", help="远端名（仅文件需要；目录用本地结构）")

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
        if os.path.isdir(args.local):
            if args.remote:
                sys.exit("目录推送不支持指定 remote 名，子目录结构由本地决定")
            push_tree(udid, args.local, args.port)
        else:
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

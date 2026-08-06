"""命令执行与日志跟踪。

依赖：config, hdc, hilog。

设备侧 stdout/stderr 经 hilog 回传，故这里用 `hilog -x` 轮询收集：
流式 `hilog` 在 pipe 模式下可能全缓冲导致小量输出读不到（历史 bug）。
"""
from __future__ import annotations

import random
import subprocess
import sys
import time

from binrunner.config import ABILITY, BUNDLE, TAG
from binrunner.hdc import hdc_cmd, run_hdc
from binrunner.hilog import parse_exit_code, parse_output, report_is_complete

# App 冷启动 + 设备侧 300ms setTimeout + 命令执行的预留时间
_STARTUP_WAIT = 1.0
# 轮询间隔
_POLL_INTERVAL = 0.5
# 单次 hilog -x 的最小超时
_MIN_POLL_TIMEOUT = 5
# br logs 的轮询间隔
_LOGS_INTERVAL = 1


def new_run_id() -> str:
    """生成 8 位十六进制执行 ID，用于多终端并发时隔离各自输出。"""
    return "".join(random.choices("0123456789abcdef", k=8))


def _dump_hilog(udid: str, timeout: float) -> str:
    """非阻塞 dump 设备日志。宽容解码：其他进程的日志可能含非 UTF-8 字节。"""
    r = subprocess.run(
        hdc_cmd(udid, "shell", "hilog -x"), capture_output=True, timeout=timeout
    )
    return r.stdout.decode("utf-8", errors="replace")


def cmd_run(udid: str, cmdline: str, timeout: int) -> int:
    """在设备上执行命令，打印报告，返回目标二进制的退出码。"""
    run_id = new_run_id()

    # 清掉旧日志，避免上次执行的报告混入（失败无妨，>>> exec 标记会兜底）
    run_hdc(udid, "shell", "hilog -r", check=False)
    run_hdc(
        udid,
        "shell",
        f"aa start -b {BUNDLE} -a {ABILITY} --ps run_id {run_id} --ps cmd '{cmdline}'",
    )

    started = False
    done = False
    report_lines: list[str] = []
    parts: dict[int, str] = {}  # 超长行的 [i/n] 分段缓存，跨轮次复用

    deadline = time.time() + timeout
    time.sleep(_STARTUP_WAIT)

    while not done:
        remain = deadline - time.time()
        if remain <= 0:
            print(f"[binrunner] 等待输出超时（{timeout}s）", file=sys.stderr)
            return 1

        output = _dump_hilog(udid, max(remain, _MIN_POLL_TIMEOUT))
        started, done = parse_output(output, started, report_lines, parts, run_id)
        if done:
            break
        # <<< END 可能被 hilog socket 丢弃 → 用报告结构完整性兜底
        if started and report_is_complete(report_lines):
            break
        time.sleep(_POLL_INTERVAL)

    if not done and not report_lines:
        print(
            "[binrunner] 没收到执行报告（App 未运行或 cmd 未触发？）", file=sys.stderr
        )
        return 1

    report = "\n".join(report_lines)
    print(report)
    return parse_exit_code(report)


def cmd_logs(udid: str) -> int:
    """持续跟踪设备 BinRunner 日志（Ctrl+C 退出）。

    seen 集合去重：hilog -x 每次 dump 整个缓冲区，已打印的行不再重复输出。
    """
    seen: set[str] = set()
    print(
        f"[binrunner] 跟踪设备 {udid} 的 BinRunner 日志，Ctrl+C 退出...",
        file=sys.stderr,
    )
    try:
        while True:
            for line in _dump_hilog(udid, timeout=10).split("\n"):
                if TAG in line and line not in seen:
                    seen.add(line)
                    print(line)
            time.sleep(_LOGS_INTERVAL)
    except KeyboardInterrupt:
        return 0

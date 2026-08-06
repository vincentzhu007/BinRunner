"""hilog 输出解析。

纯函数模块，零 I/O 零依赖（除 config 常量）—— 这是 CLI 最易出错的部分，
保持无副作用以便充分单测。

设备侧输出协议（app/entry/src/main/ets/common/BinRunner.ets）：

    [<run_id>] >>> exec <name> args=[...]   执行开始标记
    [<run_id>] <<< <报告行>                  报告内容（前缀可能被 hilog 丢弃）
    [<run_id>] <<< [i/n] <片段>              单行超 900 字符时的分段
    [<run_id>] <<< END                       报告结束标记（可能被 hilog 丢弃）
"""
from __future__ import annotations

import re

from binrunner.config import TAG

# 匹配 hilog 行中属于本 App 的部分。TAG 走 re.escape，避免其含正则元字符时误匹配。
_LINE_RE = re.compile(rf"{re.escape(TAG)}: (.*)")

# 单行超长时的分段标记：[当前/总数] 内容
_SEGMENT_RE = re.compile(r"\[(\d+)/(\d+)\] (.*)")

EXEC_MARKER = ">>> exec "
END_MARKER = "<<< END"
REPORT_PREFIX = "<<< "

# native 侧诊断日志前缀。这些行混在报告中输出，但不属于目标二进制的 stdout/stderr。
_DIAG_PREFIXES = (
    "exec ",
    "execv ",
    "memfd ",
    "resolved ",
    "hnp ",
    "opendir ",
    "probe",
    "CRASH ",
    "no executable ",
)


def is_diag_line(body: str) -> bool:
    """判断是否为 native 侧诊断日志（不应进入执行报告）。"""
    return body.startswith(_DIAG_PREFIXES)


def parse_output(
    output: str,
    started: bool,
    report_lines: list[str],
    parts: dict[int, str],
    run_id: str = "",
) -> tuple[bool, bool]:
    """解析 hilog -x 输出，就地追加报告行。返回 (started, done)。

    调用方在轮询循环中复用 started/report_lines/parts，故分段可跨轮次拼接。

    run_id 非空时仅处理带该 ID 前缀的行（多终端并发互不干扰）；
    为空则不做过滤，仅能解析无前缀日志（手动 aa start 的兼容路径）。
    """
    done = False
    id_marker = f"[{run_id}] " if run_id else ""

    for line in output.split("\n"):
        m = _LINE_RE.search(line)
        if not m:
            continue
        body = m.group(1)

        # run_id 过滤：剥离前缀后，后续解析逻辑与无前缀情形完全一致
        if id_marker:
            if not body.startswith(id_marker):
                continue
            body = body[len(id_marker):]

        # 开始标记之前的内容一律丢弃（清日志失败时的兜底）
        if not started:
            if body.startswith(EXEC_MARKER):
                started = True
            continue

        if body == END_MARKER:
            done = True
            continue

        if is_diag_line(body):
            continue

        # <<< 前缀是可选的：hilog 对部分行会丢掉它
        content = body[len(REPORT_PREFIX):] if body.startswith(REPORT_PREFIX) else body

        seg = _SEGMENT_RE.match(content)
        if not seg:
            report_lines.append(content)
            continue

        # 分段：按序号缓存，收齐后无缝拼接（分段间不插换行）
        index, total = int(seg.group(1)), int(seg.group(2))
        parts[index] = seg.group(3)
        if len(parts) == total:
            report_lines.append("".join(parts[i] for i in range(1, total + 1)))
            parts.clear()

    return started, done


def report_is_complete(lines: list[str]) -> bool:
    """报告结构是否完整：exit 码 + stdout 段 + stderr 段。

    用作 END_MARKER 被 hilog socket 丢弃时的兜底判据。
    """
    text = "\n".join(lines)
    has_exit = bool(re.search(r"^exit=-?\d+", text, re.MULTILINE))
    return has_exit and "--- stdout ---" in text and "--- stderr ---" in text


def parse_exit_code(report: str) -> int:
    """从报告中提取退出码。要求 exit= 位于行首，避免误匹配二进制自身输出。"""
    m = re.search(r"^exit=(-?\d+)", report, re.MULTILINE)
    return int(m.group(1)) if m else 0

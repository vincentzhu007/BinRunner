"""hilog 输出解析。

零 I/O 零依赖（除 config 常量）—— 这是 CLI 最易出错的部分，
保持无副作用以便充分单测。

设备侧输出协议（app/entry/src/main/ets/common/BinRunner.ets + cpp/napi_init.cpp）：

    [<run_id>] >>> exec <name> args=[...]   执行开始标记
    [<run_id>] <<< <报告行>                  报告内容（前缀可能被 hilog 丢弃）
    [<run_id>] <<< [i/n] <片段>              单行超 900 字符时的分段
    [<run_id>] <<< exit=.. timedOut=.. streamChunks=N  汇总行（N 用于判定块已收齐）
    [<run_id>] <<< END                       报告结束标记（可能被 hilog 丢弃）
    [<run_id>] STREAM <seq> <channel> <hex>  目标 stdout/stderr 的十六进制分块
    [<run_id>] DIAG <文本>                   native/ArkTS 诊断，不属于目标输出

报告行、STREAM 块与汇总行混在同一份 dump 里，`hilog -x` 单次 dump 在设备日志量大时
可达 22MB 级（见 runner._dump_argv），所以每个 HilogParser.consume() 只扫一遍 dump：
一趟完成归属过滤、诊断过滤、分段重组、快照去重并解码流块。
"""
from __future__ import annotations

import re
from collections import Counter

from binrunner.config import TAG

# 匹配 hilog 行中属于本 App 的部分。TAG 走 re.escape，避免其含正则元字符时误匹配。
_LINE_RE = re.compile(rf"{re.escape(TAG)}: (.*)")

# 单行超长时的分段标记：[当前/总数] 内容
_SEGMENT_RE = re.compile(r"\[(\d+)/(\d+)\] (.*)")

# 流块的十六进制载荷：解码后按序交给 emit
_CHUNK_RE = re.compile(r"STREAM (\d+) (stdout|stderr) ([0-9a-f]+)")

# 汇总行。timeoutSec 为可选组：v1.2.0-HAP 不输出该字段
_SUMMARY_RE = re.compile(
    r"<<< exit=-?\d+ timedOut=(?:true|false)(?: timeoutSec=\d+)? streamChunks=(\d+)"
)

# 状态行（`<<< exit=… timedOut=…`）：legacy 报告的首行与流式汇总行都以此开头。
# CLI 靠 timedOut 区分「超时」与「根本没起来」—— 旧 HAP 对后者也只回 exit=-1。
_STATUS_RE = re.compile(r"^exit=(-?\d+) timedOut=(true|false)")

EXEC_MARKER = ">>> exec "
END_MARKER = "<<< END"
REPORT_PREFIX = "<<< "

# 新 HAP 的 native/ArkTS 诊断行统一以此标记开头
DIAG_MARKER = "DIAG "

# 旧 HAP（不输出 DIAG 标记）的兜底：逐条照抄 napi_init.cpp 里 OH_LOG_INFO/WARN 的实际文本
# （含已删除的历史版本诊断）。刻意不做成 "exec "、"probe" 这类宽前缀 —— hilog 偶发丢
# `<<< ` 前缀时，目标程序自己以它们开头的 stdout 行会被整行吞掉；下列文本都是 native
# 诊断独有的措辞，目标程序不会这么打印。
_DIAG_FALLBACKS = (
    "execv blocked (",           # execve 被 SELinux 拒绝，回退内存 loader
    "exec lib",                  # "exec lib<name>.so argc=<n>"，每次执行的固定首行
    "memfd diag: ",              # 内存 loader 的 ELF header 自检
    "resolved via push dir: ",   # 命中推送目录
    "resolved via hnp: ",        # 历史版本：命中 hnp 目录
    "hnp candidate: ",           # 历史版本：hnp 候选路径枚举
    "no executable hnp binary",  # 历史版本：hnp 下无可执行文件
    "opendir ",                  # 历史版本：opendir(<orgDir>) 失败
    "probe ",                    # ProbeDir 目录枚举（probe/probe2 调试命令）
    "probe2 ",                   # ProbeExecMmap / access 探测
    "CRASH sig=",                # CrashHandler 崩溃诊断
)


def is_diag_line(body: str) -> bool:
    """判断是否为 native/ArkTS 侧诊断日志（不应进入执行报告）。

    先看新 HAP 的 DIAG 标记，再退回旧 HAP 的具体文本前缀。判断在剥离 `<<< ` 之前做：
    报告行带前缀时不会是 `DIAG ...` 或上述文本，故目标程序自己打印的同名内容不受影响。
    """
    return body.startswith(DIAG_MARKER) or body.startswith(_DIAG_FALLBACKS)


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


class HilogParser:
    """单遍解析一份 hilog -x dump，并持有整个执行周期（多轮 dump）的累积状态。

    调用方每轮把整份 dump 交给 consume()；跨轮保留的状态让重复 dump 幂等：
    分段缓存不丢、已输出过的报告行不重复、STREAM 块按序号去重。

    run_id 非空时仅处理带该 ID 前缀的行（多终端并发互不干扰），且不再要求先看到
    启动标记 —— 前缀本身已确定归属，启动行可能已被环形缓冲淘汰（#11）；
    为空则不做过滤，仅能解析无前缀日志（手动 aa start 的兼容路径）。
    """

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self.id_marker = f"[{run_id}] " if run_id else ""
        self.started = False                # 是否已见到本 run 的归属行
        self.done = False                   # 是否见到 <<< END
        self.report_lines: list[str] = []   # 供人类阅读的报告行
        self.expected: int | None = None    # 汇总行里的 streamChunks 总块数
        self.exit_code: int | None = None   # 状态行的目标退出码（-1 = 超时/没起来）
        self.timed_out: bool | None = None  # 状态行的 timedOut 标志
        self.next_sequence = 0              # 期望的下一个 STREAM 序号
        self.pending: dict[int, tuple[str, bytes]] = {}   # 乱序到达的 STREAM 块
        self.parts: dict[int, str] = {}     # 超长行的 [i/n] 分段缓存，跨轮次复用
        self.seen: Counter[str] = Counter()  # 各报告行已输出过的次数（快照去重）

    @property
    def report(self) -> str:
        return "\n".join(self.report_lines)

    def consume(self, output: str, emit) -> None:
        """解析一份 dump：报告行就地追加，流块按序交给 emit(channel, payload)。"""
        occurrences: Counter[str] = Counter()
        for line in output.split("\n"):
            m = _LINE_RE.search(line)
            if not m:
                continue
            body = m.group(1)

            # run_id 过滤：剥离前缀后，后续解析逻辑与无前缀情形完全一致
            if self.id_marker:
                if not body.startswith(self.id_marker):
                    continue
                body = body[len(self.id_marker):]

            # 开始标记之前的内容一律丢弃（清日志失败时的兜底）。
            # run_id 已过滤归属，启动标记丢失不应导致整份报告被忽略。
            if not self.started:
                if not self.run_id and not body.startswith(EXEC_MARKER):
                    continue
                self.started = True

            if body.startswith(EXEC_MARKER):
                continue

            if body.startswith("STREAM "):
                self._consume_chunk(body, emit)
                continue

            if body == END_MARKER:
                self.done = True
                continue

            if is_diag_line(body):
                continue

            # <<< 前缀是可选的：hilog 对部分行会丢掉它
            content = body[len(REPORT_PREFIX):] if body.startswith(REPORT_PREFIX) else body
            # 状态行（报告首行 / 流式汇总行）：记录退出码与 timedOut 供 CLI 翻译。
            # 只认首个匹配：legacy 报告的状态行在首行，流式模式下只有汇总行会匹配。
            if self.timed_out is None:
                status = _STATUS_RE.match(content)
                if status:
                    self.exit_code = int(status.group(1))
                    self.timed_out = status.group(2) == "true"

            summary = _SUMMARY_RE.fullmatch(body)
            if summary:
                self.expected = int(summary.group(1))

            # 快照去重：hilog -x 每轮重读整个环形缓冲，同一物理行会出现在多份 dump 里。
            # 同一份 dump 内的重复（目标确实打印了两遍）按出现次数全部保留，
            # 只丢弃"前几份 dump 已经输出过"的次数。
            occurrences[content] += 1
            if occurrences[content] <= self.seen[content]:
                continue

            seg = _SEGMENT_RE.match(content)
            if not seg:
                self.report_lines.append(content)
                continue

            # 分段：按序号缓存，收齐后无缝拼接（分段间不插换行）
            index, total = int(seg.group(1)), int(seg.group(2))
            self.parts[index] = seg.group(3)
            if len(self.parts) == total:
                self.report_lines.append("".join(self.parts[i] for i in range(1, total + 1)))
                self.parts.clear()

        self.seen |= occurrences

    def _consume_chunk(self, body: str, emit) -> None:
        """解码一个 STREAM 块：乱序的先缓存，凑齐前驱后按序号依次 emit。

        序号小于 next_sequence 的块是环形缓冲里的旧内容（重复 dump），直接忽略；
        重复 dump 因此不会重复输出。
        """
        chunk = _CHUNK_RE.fullmatch(body)
        if not chunk:
            return  # 非本协议的 STREAM 行（或载荷非法），不进报告也不报错
        sequence = int(chunk.group(1))
        try:
            payload = bytes.fromhex(chunk.group(3))
        except ValueError:
            return  # 传输中被截断的十六进制：丢掉该块，等下一轮 dump
        if sequence >= self.next_sequence:
            self.pending[sequence] = (chunk.group(2), payload)
        while self.next_sequence in self.pending:
            channel, payload = self.pending.pop(self.next_sequence)
            emit(channel, payload)
            self.next_sequence += 1

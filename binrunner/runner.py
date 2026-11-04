"""命令执行与日志跟踪。

依赖：config, hdc, hilog。

设备侧 stdout/stderr 经 hilog 回传，故这里用 `hilog -x` 轮询收集：
流式 `hilog` 在 pipe 模式下可能全缓冲导致小量输出读不到（历史 bug）。
"""
from __future__ import annotations

import codecs
import random
import re
import subprocess
import sys
import time

from binrunner.config import ABILITY, BUNDLE, TAG
from binrunner.hdc import hdc_cmd, run_hdc
from binrunner.hilog import HilogParser, parse_exit_code, report_is_complete

# App 冷启动 + 设备侧 300ms setTimeout + 命令执行的预留时间
_STARTUP_WAIT = 1.0
# 轮询间隔
_POLL_INTERVAL = 0.5
# 启动、调度及报告回传的额外等待时间
_REPORT_GRACE = 30
# 单次 hilog -x 的最大超时
_MAX_POLL_TIMEOUT = 5
# 有块却多久收不到新数据就探测设备是否已结束（探测本身 ~0.6s）。
# 远大于单轮 dump 上限(5s)+轮询间隔(0.5s)，正常抖动不会触发。
_STALL_SECONDS = 10
# br logs 的轮询间隔
_LOGS_INTERVAL = 1

# 过滤参数可用性缓存：设备 hilog 不认 -T/-e 时置 False，退回全量 dump。
_FILTERS_OK = True

# 汇总行（设备侧最终报告）里的总块数
_SUMMARY_CHUNKS_RE = re.compile(r"streamChunks=(\d+)")


def _dump_argv(run_id: str = "") -> str:
    """拼 hilog 查询命令。

    `-T TAG` 把 dump 缩到本 App 一个 tag；不带过滤时 `hilog -x` 每次重读整个
    16MB 级缓冲区，实测 22.8MB/69s（#11 的根因）。
    `-e '\\[run_id\\]'` 再缩到本次执行：否则历史运行留在缓冲区里的行会让每轮
    dump 都超时，把之后**所有** run 一起拖死。
    单引号是给设备侧 shell 的（hdc 把 argv 拼成一条命令，反斜杠会被 shell 吃掉）。
    """
    if not _FILTERS_OK:
        return "hilog -x"
    shell = f"hilog -x -T {TAG}"
    if run_id:
        shell += f" -e '\\[{run_id}\\]'"
    return shell


def new_run_id() -> str:
    """生成 8 位十六进制执行 ID，用于多终端并发时隔离各自输出。"""
    return "".join(random.choices("0123456789abcdef", k=8))


def _run_hilog(udid: str, shell: str, timeout: float) -> str:
    """执行一条 hilog 查询，宽容解码；超时保留已收到的部分并丢弃截断的末行。

    超时（hdc 进程被强杀）时保留部分输出，否则每轮都白跑、解析器永远看不到
    设备早已写好的报告（#11）。返回空串表示本轮没拿到数据。
    """
    global _FILTERS_OK
    try:
        r = subprocess.run(
            hdc_cmd(udid, "shell", shell), capture_output=True, timeout=timeout
        )
        stdout = r.stdout
        # 设备 hilog 不认过滤参数时会整体报错且 stdout 为空：退回全量 dump，
        # 慢一些但不会因此收不到报告。
        if _FILTERS_OK and r.returncode != 0:
            _FILTERS_OK = False
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or b""
        stdout = stdout[: stdout.rfind(b"\n") + 1]
    return stdout.decode("utf-8", errors="replace")


def _dump_hilog(udid: str, timeout: float, run_id: str = "") -> str:
    """非阻塞 dump 本次执行的日志（run_id 为空则只按 tag 过滤）。"""
    return _run_hilog(udid, _dump_argv(run_id), timeout)


def _probe_chunks(udid: str, run_id: str) -> int | None:
    """探测设备侧是否已结束本 run，返回汇总行的总块数（未结束返回 None）。

    本 run 的数据积压超过轮询上限时，汇总行落在缓冲区尾部、常规 dump 拿不到，
    但"设备是否已结束"决定了还要不要继续等：只有结束（且缺块）才应立即失败。
    只匹配汇总行，代价恒定（实测 ~0.6s，与缓冲区大小无关）。
    """
    shell = "hilog -x"
    if _FILTERS_OK:
        shell += f" -T {TAG} -e streamChunks"
    output = _run_hilog(udid, shell, _MAX_POLL_TIMEOUT)
    marker = f"[{run_id}] "
    for line in output.splitlines():
        if marker not in line:
            continue
        m = _SUMMARY_CHUNKS_RE.search(line)
        if m:
            return int(m.group(1))
    return None


def cli_exit_code(status_code: int, timed_out: bool | None) -> int:
    """把设备报告的状态翻译成 CLI 退出码（契约见 docs/cli-reference.md）。

    `status_code >= 0` 是目标程序的真实退出码（含 128+sig），原样透传。
    负值是设备侧元数据而非退出码：`-1` 表示进程没跑成（超时被杀或根本没起来），
    两代 HAP 的报法不同 ——

    - 新 HAP：`ResolveExecPath` 等启动失败直接报 `exit=255`（正数，走上面的透传），
      `-1` 只留给超时；
    - 旧 HAP（v1.4.x 及更早）：启动失败也只报 `exit=-1`，只能靠 `timedOut=false`
      与超时区分。

    因此这里用 `timedOut` 分流：超时 → 1（文档承诺），根本没起来 → 255。
    这样新旧两代 HAP 都给出文档承诺的退出码，且不必嗅探报告正文。
    """
    if status_code >= 0:
        return status_code
    return 1 if timed_out is not False else 255


def cmd_run(udid: str, cmdline: str, timeout: int) -> int:
    """在设备上执行命令，实时转发输出并返回目标二进制的退出码。

    报告经 hilog 回传：每轮只读本 App tag 下本 run 的行。若设备已结束却仍缺块
    （常见于输出量超过 hilog 回传带宽），立即失败而不是空等到 --timeout。
    """
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 2147483647:
        raise ValueError("执行超时必须为 1–2147483647 的整数秒")
    run_id = new_run_id()

    # run_id 隔离旧日志；不要清空其他并发任务的日志。
    run_hdc(
        udid,
        "shell",
        f"aa start -b {BUNDLE} -a {ABILITY} --ps run_id {run_id} --ps stream 1 --ps timeout_sec {timeout} --ps cmd '{cmdline}'",
    )

    # 报告行、STREAM 块、汇总行混在同一份 dump 里：单遍解析，不重复扫 22MB 级输出
    parser = HilogParser(run_id)
    decoders = {name: codecs.getincrementaldecoder("utf-8")("replace")
                for name in ("stdout", "stderr")}

    def emit(channel: str, payload: bytes, final: bool = False) -> None:
        target = sys.stdout if channel == "stdout" else sys.stderr
        target.write(decoders[channel].decode(payload, final=final))
        target.flush()

    wait_timeout = timeout + _REPORT_GRACE
    deadline = time.monotonic() + wait_timeout
    time.sleep(_STARTUP_WAIT)

    progress: tuple[int, int, int] | None = None
    last_progress_at = time.monotonic()

    def flush_decoders() -> None:
        for channel in decoders:
            emit(channel, b"", final=True)

    def device_finished() -> bool:
        """设备侧是否已结束本 run（汇总行取不到时按需探测一次）。"""
        if parser.expected is None:
            parser.expected = _probe_chunks(udid, run_id)
        return parser.expected is not None

    def report_incomplete() -> None:
        """设备已结束但缺块：冲刷解码器后报错（已显示的输出保留）。"""
        flush_decoders()
        print(
            f"[binrunner] 输出数据不完整（收到连续 {parser.next_sequence}/"
            f"{parser.expected} 块）：设备已结束，缺失部分无法通过 hilog 补齐",
            file=sys.stderr,
        )

    while not parser.done:
        now = time.monotonic()
        remain = deadline - now
        if remain <= 0:
            if device_finished():
                report_incomplete()
            else:
                flush_decoders()
                print(
                    f"[binrunner] 等待执行报告超时（{wait_timeout}s；设备执行期限 {timeout}s，"
                    f"启动和报告预留 {_REPORT_GRACE}s），设备是否已结束未知",
                    file=sys.stderr,
                )
            return 1

        parser.consume(_dump_hilog(udid, min(remain, _MAX_POLL_TIMEOUT), run_id), emit)
        if parser.expected is not None:
            # 汇总行在所有数据块之后发出，且不依赖可能丢失的 <<< END。
            if parser.next_sequence == parser.expected:
                break
            parser.done = False
        elif parser.next_sequence or parser.pending:
            # END alone cannot replace the exit status / total chunk count.
            parser.done = False
        elif parser.done:
            break

        # 有块却长时间收不到新数据：只有确认设备已结束才能判定缺块不可补齐。
        if parser.next_sequence or parser.pending:
            current = (parser.next_sequence, len(parser.pending), len(parser.report_lines))
            if current != progress:
                progress, last_progress_at = current, now
            elif now - last_progress_at >= _STALL_SECONDS:
                if device_finished():
                    report_incomplete()
                    return 1
                last_progress_at = now  # 设备仍在执行：继续等，截止时间兜底
        # <<< END 可能被 hilog socket 丢弃 → 用报告结构完整性兜底
        if parser.started and report_is_complete(parser.report_lines):
            break
        time.sleep(min(_POLL_INTERVAL, max(0, deadline - time.monotonic())))

    if not parser.done and not parser.report_lines:
        print(
            "[binrunner] 没收到执行报告（App 未运行或 cmd 未触发？）", file=sys.stderr
        )
        return 1

    flush_decoders()
    report = parser.report
    # Keep execution metadata separate from the program's stdout.
    print(report, file=sys.stderr if parser.expected is not None else sys.stdout,
          flush=True)
    # 设备侧负 exit 是报告元数据（超时 / 根本没起来），不是目标退出码；
    # 翻译规则与两代 HAP 的差异见 cli_exit_code。
    return cli_exit_code(parse_exit_code(report), parser.timed_out)


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

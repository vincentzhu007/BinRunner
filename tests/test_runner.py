"""执行期限传递、报告预留及主机轮询截止时间的回归测试。"""
import subprocess

import pytest

from binrunner import runner
from binrunner.cli import build_parser


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def execution(monkeypatch):
    clock = Clock()
    calls = []
    monkeypatch.setattr(runner.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(runner.time, "sleep", clock.sleep)
    monkeypatch.setattr(runner, "new_run_id", lambda: "a1b2c3d4")
    monkeypatch.setattr(runner, "hdc_cmd", lambda udid, *a: ["hdc", "-t", udid, *a])
    monkeypatch.setattr(runner, "_probe_chunks", lambda *a: None)
    monkeypatch.setattr(runner, "run_hdc", lambda *a, **kw: calls.append(a))
    return clock, calls


def report(exit_code, timed_out, timeout):
    prefix = "x BinRunner: [a1b2c3d4] "
    return "\n".join(prefix + line for line in [
        ">>> exec hello args=[]",
        f"<<< exit={exit_code} timedOut={timed_out} timeoutSec={timeout}",
        "<<< --- stdout ---",
        "<<< hello",
        "<<< --- stderr ---",
        "<<< END",
    ])


@pytest.mark.parametrize("seconds,exit_code,timed_out,arrival", [
    (1800, 42, "false", 45),  # 运行超过旧的 30 秒限制
    (1800, -1, "true", 1802),  # 执行超时后的报告仍可在预留窗口内收到
    (60, 0, "false", 1),
    (1, -1, "true", 2),
])
def test_execution_timeout_and_report_grace(
    execution, monkeypatch, capsys, seconds, exit_code, timed_out, arrival,
):
    clock, calls = execution
    monkeypatch.setattr(
        runner, "_dump_hilog",
        lambda *a: report(exit_code, timed_out, seconds) if clock.now >= arrival else "",
    )
    assert runner.cmd_run("device", "hello", seconds) == exit_code
    assert f"--ps timeout_sec {seconds}" in calls[0][2]
    assert "--ps run_id a1b2c3d4" in calls[0][2]
    assert "--ps cmd 'hello'" in calls[0][2]
    captured = capsys.readouterr()
    assert f"exit={exit_code} timedOut={timed_out} timeoutSec={seconds}" in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("stalled", [False, True])
def test_missing_report_respects_total_deadline(execution, monkeypatch, capsys, stalled):
    clock, _ = execution
    poll_timeouts = []

    def dump(udid, timeout, run_id=""):
        poll_timeouts.append(timeout)
        if stalled:
            clock.sleep(timeout)  # 本轮 dump 耗尽超时、一行都没拿到
        return ""

    monkeypatch.setattr(runner, "_dump_hilog", dump)
    assert runner.cmd_run("device", "hello", 2) == 1
    assert clock.now == 32
    assert all(0 < t <= 5 for t in poll_timeouts)
    error = capsys.readouterr().err
    assert "32s" in error and "设备执行期限 2s" in error and "预留 30s" in error
    assert "设备是否已结束未知" in error


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "nan", "2147483648", "oops"])
def test_cli_rejects_invalid_timeout(value):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["run", "hello", "--timeout", value])
    assert exc.value.code == 2


def test_cli_timeout_default_and_boundaries():
    parser = build_parser()
    assert parser.parse_args(["run", "hello"]).timeout == 60
    for seconds in (1, 1800, 2147483647):
        assert parser.parse_args(["run", "--timeout", str(seconds), "hello"]).timeout == seconds


@pytest.mark.parametrize("value", [0, -1, 1.5, True, 2147483648])
def test_direct_call_rejects_invalid_timeout_before_device_access(execution, value):
    _, calls = execution
    with pytest.raises(ValueError):
        runner.cmd_run("device", "hello", value)
    assert calls == []


@pytest.mark.parametrize("exit_code,timed_out,arrival", [
    (42, "false", 45),
    (-1, "true", 1802),
])
def test_streaming_preserves_device_timeout_and_report_grace(
    execution, monkeypatch, capsys, exit_code, timed_out, arrival,
):
    clock, calls = execution
    prefix = "x BinRunner: [a1b2c3d4] "
    first = prefix + ">>> exec hello args=[]\n" + prefix + "STREAM 0 stdout 68656c6c6f0a\n"

    def dump(*args):
        if clock.now < arrival:
            return first
        assert capsys.readouterr().out == "hello\n"
        return first + prefix + (
            f"<<< exit={exit_code} timedOut={timed_out} timeoutSec=1800 streamChunks=1\n"
        )

    monkeypatch.setattr(runner, "_dump_hilog", dump)
    assert runner.cmd_run("device", "hello", 1800) == exit_code
    assert "--ps timeout_sec 1800" in calls[0][2]
    assert "--ps stream 1" in calls[0][2]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"exit={exit_code} timedOut={timed_out} timeoutSec=1800" in captured.err


def test_dump_keeps_partial_output_when_hdc_times_out(monkeypatch):
    """#11：轮询超时不得丢弃已收到的日志，否则每轮白跑、永远等不到报告。

    同时锁定只 dump 本 App 的 tag —— `hilog -x` 每次都重读整个缓冲区，
    不加过滤时单次 dump 会随设备日志量变慢并持续超时。
    """
    partial = (
        b"...BinRunner: [a1b2c3d4] <<< exit=0 timedOut=false\n"
        b"...BinRunner: [a1b2c3d4] <<< cut"
    )
    seen = []

    def fake_hdc(udid, *args):
        seen.append((udid, args))
        return ["hdc", "-t", udid, *args]

    def fake_run(cmd, capture_output=False, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout, output=partial)

    monkeypatch.setattr(runner, "hdc_cmd", fake_hdc)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    text = runner._dump_hilog("device", 5)
    assert seen == [("device", ("shell", "hilog -x -T BinRunner"))]
    assert text.endswith("<<< exit=0 timedOut=false\n")
    assert "cut" not in text, "被截断的末行必须丢弃，否则污染报告"


def test_partial_dump_is_enough_to_finish(execution, monkeypatch, capsys):
    """#11：超时轮次收到的部分报告即可满足完成判据，不必等到 --timeout。"""
    clock, _ = execution
    prefix = "x BinRunner: [a1b2c3d4] "
    partial = ("\n".join(prefix + line for line in [
        ">>> exec hello args=[]",
        "<<< exit=7 timedOut=false timeoutSec=60",
        "<<< --- stdout ---",
        "<<< hello",
        "<<< --- stderr ---",
    ]) + "\n").encode()
    polls = []

    def fake_run(cmd, capture_output=False, timeout=None):
        polls.append(cmd)
        if len(polls) == 1:
            raise subprocess.TimeoutExpired(cmd, timeout, output=partial)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"")

    monkeypatch.setattr(runner, "hdc_cmd", lambda udid, *a: ["hdc", "-t", udid, *a])
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.cmd_run("device", "hello", 60) == 7
    assert "exit=7 timedOut=false timeoutSec=60" in capsys.readouterr().out
    assert clock.now < 60, "部分报告已足够完成，不应把 --timeout 空耗完"


def test_lost_chunk_fails_fast_after_summary(execution, monkeypatch, capsys):
    """#11：汇总行已到且缓冲不再变化 → 缺块不可补齐，立即失败而非空等到超时。"""
    clock, _ = execution
    prefix = "x BinRunner: [a1b2c3d4] "
    snapshot = "\n".join(prefix + line for line in [
        ">>> exec hello args=[]",
        "STREAM 0 stdout 61",
        "STREAM 2 stdout 63",  # 序号 1 被 hilog 丢弃
        "<<< exit=0 timedOut=false timeoutSec=1800 streamChunks=3",
        "<<< END",
    ])
    monkeypatch.setattr(runner, "_dump_hilog", lambda *a: snapshot)
    assert runner.cmd_run("device", "hello", 1800) == 1
    assert clock.now < 60, "缺块不可补齐时不应空等到 --timeout + 30s"
    captured = capsys.readouterr()
    assert captured.out == "a", "已收到的块仍应输出"
    assert "收到连续 1/3 块" in captured.err


def test_dump_falls_back_when_hilog_rejects_filters(monkeypatch):
    """设备 hilog 不认 -T/-e 时退回全量 dump：慢一些，但不会因此收不到报告。"""
    shells = []
    monkeypatch.setattr(runner, "_FILTERS_OK", True)  # 复原全局：本用例会把它置 False


    def fake_run(cmd, capture_output=False, timeout=None):
        shells.append(cmd[-1])
        if len(shells) == 1:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"invalid option")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"x BinRunner: [a1b2c3d4] hi\n")

    monkeypatch.setattr(runner, "hdc_cmd", lambda udid, *a: ["hdc", "-t", udid, *a])
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner._dump_hilog("device", 5, "a1b2c3d4") == ""
    assert runner._dump_hilog("device", 5, "a1b2c3d4") == "x BinRunner: [a1b2c3d4] hi\n"
    assert shells == [f"hilog -x -T {runner.TAG} -e '\\[a1b2c3d4\\]'", "hilog -x"]


def test_dump_is_scoped_to_one_run():
    """历史 run 留在缓冲区里的行不能拖慢本次轮询（否则之后所有 run 一起超时）。"""
    assert runner._dump_argv("a1b2c3d4") == f"hilog -x -T {runner.TAG} -e '\\[a1b2c3d4\\]'"
    assert runner._dump_argv() == f"hilog -x -T {runner.TAG}"  # br logs：只看 tag


def test_stalled_stream_fails_fast_once_device_finished(execution, monkeypatch, capsys):
    """设备已结束却仍缺块（输出量超过 hilog 回传带宽）→ 立即失败并给出真实块数。"""
    clock, _ = execution
    prefix = "x BinRunner: [a1b2c3d4] "
    # 汇总行在缓冲区尾部、常规 dump 取不到：expected 未知，靠探测确认设备已结束
    snapshot = "\n".join(prefix + line for line in [
        ">>> exec chat args=[]",
        "STREAM 0 stdout 61",
        ])
    monkeypatch.setattr(runner, "_dump_hilog", lambda *a: snapshot)
    monkeypatch.setattr(runner, "_probe_chunks", lambda *a: 4921)
    assert runner.cmd_run("device", "chat", 3600) == 1
    assert clock.now < 120, "确认设备已结束后不应继续空等到 3630s 截止时间"
    captured = capsys.readouterr()
    assert captured.out == "a"
    assert "收到连续 1/4921 块" in captured.err
    assert "设备已结束" in captured.err


def test_stall_while_device_running_waits_for_deadline(execution, monkeypatch, capsys):
    """探测显示设备仍在跑 → 不能误判失败，继续等到截止时间。"""
    clock, _ = execution
    prefix = "x BinRunner: [a1b2c3d4] "
    snapshot = "\n".join(prefix + line for line in [
        ">>> exec chat args=[]",
        "STREAM 0 stdout 61",
    ])
    probes = []

    def probe(udid, run_id):
        probes.append(run_id)
        return None

    monkeypatch.setattr(runner, "_dump_hilog", lambda *a: snapshot)
    monkeypatch.setattr(runner, "_probe_chunks", probe)
    assert runner.cmd_run("device", "chat", 30) == 1
    assert clock.now == 60, "应一直等到 --timeout + 预留"
    assert probes, "停滞时应探测设备是否已结束"
    assert "设备是否已结束未知" in capsys.readouterr().err

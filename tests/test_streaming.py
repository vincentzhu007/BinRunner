"""Streaming regression tests: assert output is visible before the next poll."""
import pytest

from binrunner import runner
from binrunner.hilog import StreamOutput


def log(body, run_id="12345678"):
    return f"x BinRunner: [{run_id}] {body}\n"


def chunk(sequence, channel, data):
    return log(f"STREAM {sequence} {channel} {data.hex()}")


@pytest.fixture
def setup_run(monkeypatch):
    commands = []
    monkeypatch.setattr(runner, "new_run_id", lambda: "12345678")
    monkeypatch.setattr(runner, "run_hdc", lambda *args, **kwargs: commands.append(args))
    monkeypatch.setattr(runner.time, "sleep", lambda _: None)
    monkeypatch.setattr(runner, "_probe_chunks", lambda *args: None)
    return commands


def test_output_arrives_before_exit(monkeypatch, capsys, setup_run):
    first = log(">>> exec hello args=[]") + chunk(0, "stdout", b"progress\n")
    second = first + chunk(1, "stderr", b"warning\n")
    polls = 0

    def dump(*args):
        nonlocal polls
        polls += 1
        if polls == 1:
            return first
        captured = capsys.readouterr()
        if polls == 2:
            assert captured.out == "progress\n"
            assert captured.err == ""
            return second
        assert captured.out == ""
        assert captured.err == "warning\n"
        return second + log("<<< exit=7 timedOut=false streamChunks=2") + log("<<< END")

    monkeypatch.setattr(runner, "_dump_hilog", dump)
    assert runner.cmd_run("device", "hello", 60) == 7
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exit=7" in captured.err
    assert "--ps stream 1" in setup_run[0][-1]
    assert all("hilog -r" not in str(command) for command in setup_run)


def test_utf8_partial_lines_and_lost_end(monkeypatch, capsys, setup_run):
    data = "你好\rprogress".encode()
    polls = iter([
        log(">>> exec hello args=[]") + chunk(0, "stdout", data[:2]),
        chunk(1, "stderr", b"err") + chunk(2, "stdout", data[2:]),
        log("<<< exit=0 timedOut=false streamChunks=3"),
    ])
    monkeypatch.setattr(runner, "_dump_hilog", lambda *args: next(polls))
    assert runner.cmd_run("device", "hello", 60) == 0
    captured = capsys.readouterr()
    assert captured.out == "你好\rprogress"
    assert captured.err.startswith("errexit=0")


def test_legacy_report_snapshot_deduplication(monkeypatch, capsys, setup_run):
    first = log(">>> exec hello args=[]") + log("<<< exit=42 timedOut=false")
    second = first + log("<<< --- stdout ---") + log("<<< same") * 2
    polls = iter([first, second, second + log("<<< --- stderr ---") + log("<<< END")])
    monkeypatch.setattr(runner, "_dump_hilog", lambda *args: next(polls))
    assert runner.cmd_run("device", "hello", 60) == 42
    assert capsys.readouterr().out == "exit=42 timedOut=false\n--- stdout ---\nsame\nsame\n--- stderr ---\n"


def test_sequence_reassembly_and_run_isolation():
    stream = StreamOutput("12345678")
    received = []
    emit = lambda channel, data: received.append((channel, data))
    stream.consume(chunk(1, "stdout", b"same\n"), emit)
    assert received == []
    stream.consume(log("STREAM 0 stdout 78", "other") + chunk(0, "stdout", b"same\n"), emit)
    stream.consume(chunk(0, "stdout", b"same\n") + chunk(1, "stdout", b"same\n"), emit)
    assert received == [("stdout", b"same\n"), ("stdout", b"same\n")]


def test_large_payload_preserves_control_characters():
    data = ("中文\n<<< END\nexit=99\x00\r" * 1000).encode()
    stream = StreamOutput("12345678")
    received = bytearray()
    for seq, offset in enumerate(range(0, len(data), 400)):
        stream.consume(chunk(seq, "stdout", data[offset:offset + 400]),
                       lambda channel, payload: received.extend(payload))
    assert received == data


def test_missing_chunk_does_not_report_success(monkeypatch, capsys, setup_run):
    # 起始 / last_progress / 首轮 remain / 轮末 sleep / 次轮 remain(越过截止)
    times = iter([0, 0, 0, 32, 32])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(runner, "_dump_hilog", lambda *args:
        log(">>> exec hello args=[]") + chunk(0, "stdout", b"first\n") +
        chunk(2, "stdout", b"third\n") +
        log("<<< exit=0 timedOut=false streamChunks=3") + log("<<< END"))
    assert runner.cmd_run("device", "hello", 1) == 1
    captured = capsys.readouterr()
    assert captured.out == "first\n"
    assert "输出数据不完整" in captured.err


def test_empty_stream(monkeypatch, capsys, setup_run):
    monkeypatch.setattr(runner, "_dump_hilog", lambda *args:
        log(">>> exec hello args=[]") + log("<<< exit=-1 timedOut=true streamChunks=0"))
    assert runner.cmd_run("device", "hello", 60) == -1
    assert capsys.readouterr().out == ""


def test_lost_start_marker(monkeypatch, capsys, setup_run):
    monkeypatch.setattr(runner, "_dump_hilog", lambda *args:
        chunk(0, "stdout", b"hello") + log("<<< exit=3 timedOut=false streamChunks=1"))
    assert runner.cmd_run("device", "hello", 60) == 3
    assert capsys.readouterr().out == "hello"


def test_hdc_poll_timeout_preserves_deadline(monkeypatch, capsys, setup_run):
    """单轮 dump 用满自身超时（返回空）后，主机总截止时间仍要生效。

    超时轮次保留部分输出的行为由 runner._dump_hilog 单独覆盖（#11）。
    """
    # 截止时间按 0 计算(31)，随后 hdc 卡了 30s → 首轮只剩 1s 预算
    times = iter([0, 0, 30, 32, 33])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))

    def dump(udid, timeout, run_id=""):
        assert timeout == 1, "剩余预算不足一个轮询上限时应按剩余时间截断"
        return ""

    monkeypatch.setattr(runner, "_dump_hilog", dump)
    assert runner.cmd_run("device", "hello", 1) == 1
    assert "等待执行报告超时" in capsys.readouterr().err


def test_end_without_stream_summary_cannot_succeed(monkeypatch, capsys, setup_run):
    # 同 test_missing_chunk：2 轮后越过截止时间
    times = iter([0, 0, 0, 32, 32])
    monkeypatch.setattr(runner.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(runner, "_dump_hilog", lambda *args:
        log(">>> exec hello args=[]") + chunk(0, "stdout", b"first") + log("<<< END"))
    assert runner.cmd_run("device", "hello", 1) == 1
    captured = capsys.readouterr()
    assert captured.out == "first"
    assert "等待执行报告超时" in captured.err

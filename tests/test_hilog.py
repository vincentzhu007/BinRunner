"""hilog 输出解析测试（binrunner.hilog）。

这是 CLI 最容易出错的部分（历史上出过 4 次 bug）：
  - <<< 前缀在部分行丢失
  - <<< END 被 hilog socket 丢弃
  - [i/n] 分段拼接
  - run_id 过滤（多终端并发）
日志样本取自真机 hilog -x 实际输出。
"""
from binrunner.hilog import (
    is_diag_line,
    parse_exit_code,
    parse_output,
    report_is_complete,
)


def parse(output, run_id=""):
    """便捷封装：单次解析，返回 (started, done, report_lines)。"""
    lines: list = []
    parts: dict = {}
    started, done = parse_output(output, False, lines, parts, run_id)
    return started, done, lines


# 真机 hilog -x 输出格式（带 run_id）
REAL_LOG = """\
08-05 21:19:48.030  1769  1769 I A00001/com.example.binrunner/HiLog: some unrelated line
08-05 21:19:48.042  1769  1769 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] >>> exec hello args=[]
08-05 21:19:48.069  3873  3873 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] execv blocked (Permission denied), fallback to in-memory elf loader
08-05 21:19:48.109  1769  1769 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] <<< exit=42 timedOut=false
08-05 21:19:48.109  1769  1769 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] <<< --- stdout ---
08-05 21:19:48.109  1769  1769 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] <<< hello from bundled binary!
08-05 21:19:48.109  1769  1769 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] <<< --- stderr ---
08-05 21:19:48.109  1769  1769 I A00001/com.example.binrunner/BinRunner: [a1b2c3d4] <<< END
"""


class TestDiagLine:
    """native 侧诊断日志识别（不应进入执行报告）。"""

    def test_known_diag_prefixes_are_filtered(self):
        for body in [
            "exec libhello.so argc=0",
            "execv blocked (Permission denied), fallback to in-memory elf loader",
            "memfd diag: size=<private> first=20",
            "resolved via push dir: /data/.../bin/hello",
            "hnp candidate: /data/app/bin/hello",
            "opendir /data/app/ls.org failed",
            "probe2 exec-mmap /x: OK",
            "CRASH sig=11 fault_addr=0x0",
            "no executable hnp binary for ls",
        ]:
            assert is_diag_line(body), f"应识别为诊断行: {body!r}"

    def test_report_content_is_not_filtered(self):
        for body in [
            "exit=42 timedOut=false",
            "--- stdout ---",
            "hello from bundled binary!",
            "argc=1",
            "this line goes to stderr",
        ]:
            assert not is_diag_line(body), f"不应识别为诊断行: {body!r}"


class TestParseRealDeviceLog:
    """真机日志端到端解析。"""

    def test_extracts_report_and_detects_end(self):
        started, done, lines = parse(REAL_LOG, run_id="a1b2c3d4")
        assert started
        assert done, "<<< END 应设置 done"
        assert lines == [
            "exit=42 timedOut=false",
            "--- stdout ---",
            "hello from bundled binary!",
            "--- stderr ---",
        ]

    def test_diag_lines_excluded_from_report(self):
        _, _, lines = parse(REAL_LOG, run_id="a1b2c3d4")
        assert not any("execv blocked" in ln for ln in lines)

    def test_non_binrunner_lines_ignored(self):
        _, _, lines = parse(REAL_LOG, run_id="a1b2c3d4")
        assert not any("unrelated" in ln for ln in lines)


class TestExecMarker:
    """>>> exec 之前的内容一律丢弃（清日志失败时的兜底）。"""

    def test_lines_before_exec_marker_are_dropped(self):
        log = (
            "x BinRunner: exit=99 stale output from previous run\n"
            "x BinRunner: >>> exec hello args=[]\n"
            "x BinRunner: <<< exit=0 timedOut=false\n"
        )
        started, _, lines = parse(log)
        assert started
        assert lines == ["exit=0 timedOut=false"], "旧日志不应混入本次报告"

    def test_no_exec_marker_yields_nothing(self):
        started, done, lines = parse("x BinRunner: <<< exit=0 timedOut=false\n")
        assert not started
        assert not done
        assert lines == []

    def test_exec_marker_line_itself_not_in_report(self):
        _, _, lines = parse("x BinRunner: >>> exec hello args=[]\n")
        assert lines == []


class TestPrefixStripping:
    """<<< 前缀是可选的 —— hilog 对部分行会丢掉前缀（历史 bug）。"""

    def test_lines_with_and_without_prefix_both_accepted(self):
        log = (
            "x BinRunner: >>> exec hello args=[]\n"
            "x BinRunner: <<< exit=42 timedOut=false\n"
            "x BinRunner: --- stdout ---\n"           # 前缀丢失
            "x BinRunner: hello from bundled binary!\n"  # 前缀丢失
        )
        _, _, lines = parse(log)
        assert lines == [
            "exit=42 timedOut=false",
            "--- stdout ---",
            "hello from bundled binary!",
        ]


class TestSegmentReassembly:
    """单行超 900 字符时按 [i/n] 分段，需无缝拼接。"""

    def test_segments_joined_in_order(self):
        log = (
            "x BinRunner: >>> exec big args=[]\n"
            "x BinRunner: <<< [1/3] AAA\n"
            "x BinRunner: <<< [2/3] BBB\n"
            "x BinRunner: <<< [3/3] CCC\n"
        )
        _, _, lines = parse(log)
        assert lines == ["AAABBBCCC"], "分段间不应插入换行"

    def test_out_of_order_segments_still_joined_by_index(self):
        log = (
            "x BinRunner: >>> exec big args=[]\n"
            "x BinRunner: <<< [2/3] BBB\n"
            "x BinRunner: <<< [1/3] AAA\n"
            "x BinRunner: <<< [3/3] CCC\n"
        )
        _, _, lines = parse(log)
        assert lines == ["AAABBBCCC"]

    def test_incomplete_segments_are_buffered_not_emitted(self):
        log = (
            "x BinRunner: >>> exec big args=[]\n"
            "x BinRunner: <<< [1/3] AAA\n"
        )
        lines: list = []
        parts: dict = {}
        parse_output(log, False, lines, parts, "")
        assert lines == [], "分段未收齐不应输出"
        assert parts == {1: "AAA"}, "未收齐的分段应留在缓存里"

    def test_segments_spanning_two_poll_rounds(self):
        """轮询模式下分段可能跨两次 hilog -x 调用到达。"""
        lines: list = []
        parts: dict = {}
        started, done = parse_output(
            "x BinRunner: >>> exec big args=[]\nx BinRunner: <<< [1/2] AAA\n",
            False, lines, parts, "",
        )
        assert lines == []
        # 第二轮：caller 复用同一 started/lines/parts
        started, done = parse_output("x BinRunner: <<< [2/2] BBB\n", started, lines, parts, "")
        assert lines == ["AAABBB"], "跨轮次的分段应正确拼接"
        assert parts == {}, "拼接完成后缓存应清空"
        assert not done


class TestRunIdFiltering:
    """多终端并发：只收自己 run_id 的行。"""

    CONCURRENT_LOG = (
        "x BinRunner: [aaa11111] >>> exec hello args=[]\n"
        "x BinRunner: [bbb22222] >>> exec benchmark args=[]\n"
        "x BinRunner: [aaa11111] <<< exit=42 timedOut=false\n"
        "x BinRunner: [bbb22222] <<< exit=0 timedOut=false\n"
        "x BinRunner: [aaa11111] <<< END\n"
        "x BinRunner: [bbb22222] <<< END\n"
    )

    def test_only_own_run_id_collected(self):
        _, done, lines = parse(self.CONCURRENT_LOG, run_id="aaa11111")
        assert done
        assert lines == ["exit=42 timedOut=false"]

    def test_other_session_gets_its_own_output(self):
        _, done, lines = parse(self.CONCURRENT_LOG, run_id="bbb22222")
        assert done
        assert lines == ["exit=0 timedOut=false"]

    def test_unknown_run_id_collects_nothing(self):
        started, done, lines = parse(self.CONCURRENT_LOG, run_id="deadbeef")
        assert not started
        assert not done
        assert lines == []

    def test_empty_run_id_cannot_parse_prefixed_log(self):
        """已知权衡：run_id 为空时不剥前缀，带 [id] 的日志一行都认不出。

        `>>> exec` 检查用的是 startswith，而 body 实为 "[id] >>> exec ..."，
        故 started 都无法置位。实际影响为零：cmd_run 总会生成 run_id，
        空 run_id 只用于 br logs（原样打印，不走本解析器）。
        """
        log = (
            "x BinRunner: [aaa11111] >>> exec hello args=[]\n"
            "x BinRunner: [aaa11111] <<< exit=0 timedOut=false\n"
            "x BinRunner: [aaa11111] <<< END\n"
        )
        started, done, lines = parse(log, run_id="")
        assert not started
        assert not done
        assert lines == []

    def test_empty_run_id_parses_unprefixed_log(self):
        """兼容手动 aa start（无 run_id 参数 → App 侧不加前缀）。"""
        log = (
            "x BinRunner: >>> exec hello args=[]\n"
            "x BinRunner: <<< exit=0 timedOut=false\n"
            "x BinRunner: <<< END\n"
        )
        started, done, lines = parse(log, run_id="")
        assert started
        assert done
        assert lines == ["exit=0 timedOut=false"]


class TestReportComplete:
    """<<< END 丢失时的兜底判据：报告结构是否完整。"""

    def test_full_report_is_complete(self):
        assert report_is_complete([
            "exit=42 timedOut=false",
            "--- stdout ---",
            "hello",
            "--- stderr ---",
        ])

    def test_negative_exit_code_accepted(self):
        assert report_is_complete([
            "exit=-1 timedOut=true",
            "--- stdout ---",
            "--- stderr ---",
        ])

    def test_missing_stderr_section_incomplete(self):
        assert not report_is_complete([
            "exit=42 timedOut=false",
            "--- stdout ---",
        ])

    def test_missing_exit_incomplete(self):
        assert not report_is_complete(["--- stdout ---", "--- stderr ---"])

    def test_empty_incomplete(self):
        assert not report_is_complete([])

    def test_exit_must_be_at_line_start(self):
        """exit= 出现在行中间不算（避免二进制输出里的 'exit=' 误判）。"""
        assert not report_is_complete([
            "the program said exit=42 in its output",
            "--- stdout ---",
            "--- stderr ---",
        ])


class TestParseExitCode:
    """退出码提取。"""

    def test_extracts_exit_code(self):
        assert parse_exit_code("exit=42 timedOut=false\n--- stdout ---") == 42

    def test_extracts_negative_exit_code(self):
        assert parse_exit_code("exit=-1 timedOut=true") == -1

    def test_exit_not_at_line_start_ignored(self):
        """二进制自身输出里的 exit= 不应被当成退出码。"""
        assert parse_exit_code("program printed exit=99 here") == 0

    def test_missing_exit_defaults_to_zero(self):
        assert parse_exit_code("--- stdout ---\nhello") == 0

    def test_finds_exit_on_later_line(self):
        """exit 行不一定在首行（前面可能有诊断行）。"""
        assert parse_exit_code("some diagnostic\nexit=7 timedOut=false") == 7

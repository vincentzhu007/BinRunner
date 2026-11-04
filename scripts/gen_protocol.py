#!/usr/bin/env python3
"""从 protocol/protocol.json 生成跨语言协议常量。

存在理由：协议常量曾被抄成三份（binrunner/config.py、PushServer.ets、docs），
且已经实际漂移（魔数、探针长度、在途上限、重试次数全对不上）。生成器把
「跨语言」与「host-only」两类常量分开：只有双端必须逐字节一致的值进
protocol.json，host-only 策略值留在 binrunner/config.py。

生成物入库（ArkTS 编译需要 in-tree 文件），但禁止手改：
`--check` 与 tests/test_protocol.py 会在生成物与 JSON 不一致时失败。

用法：
    python3 scripts/gen_protocol.py           # 写出两份生成物
    python3 scripts/gen_protocol.py --check   # 只校验，不一致则非零退出
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "protocol" / "protocol.json"
PY_PATH = ROOT / "binrunner" / "protocol.py"
ETS_PATH = ROOT / "app" / "entry" / "src" / "main" / "ets" / "common" / "Protocol.ets"

REGEN_CMD = "python3 scripts/gen_protocol.py"
SPEC_REL = "protocol/protocol.json"
MARKER = "DO NOT EDIT"


def load_spec(path: Path = SPEC_PATH) -> dict[str, dict]:
    """读取常量表。JSON 保持插入顺序，故生成物顺序稳定。"""
    return json.loads(path.read_text(encoding="utf-8"))["constants"]


def literal(entry: dict) -> str:
    """把条目渲染成两侧都合法的字面量。"""
    kind = entry.get("kind", "int")
    value = entry["value"]
    if kind == "int":
        return str(int(value))
    if kind == "hex":
        # 固定 8 位大写，保持与历史字面量完全一致（线上字节不变）
        return f"0x{int(value, 16):08X}"
    raise SystemExit(f"protocol.json 出现未知 kind: {kind!r}")


def render_python(spec: dict[str, dict]) -> str:
    lines = [
        '"""跨语言协议常量 —— 生成文件，请勿手改。',
        "",
        f"来源：{SPEC_REL}（唯一来源）",
        f"重新生成：{REGEN_CMD}",
        f"手改会被 {REGEN_CMD} --check 与 tests/test_protocol.py 挡住。",
        "",
        "host-only 策略常量（超时、退避、进度阈值等）在 binrunner/config.py。",
        '"""',
        "from __future__ import annotations",
        "",
    ]
    for name, entry in spec.items():
        if entry.get("comment"):
            lines.append(f"# {entry['comment']}")
        lines.append(f"{name} = {literal(entry)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_ets(spec: dict[str, dict]) -> str:
    lines = [
        "/**",
        f" * 跨语言协议常量 —— 生成文件，{MARKER}。",
        " *",
        f" * 来源：{SPEC_REL}（唯一来源）",
        f" * 重新生成：{REGEN_CMD}",
        " * 手改会被 Python 侧 parity 测试挡住。",
        " */",
        "",
    ]
    for name, entry in spec.items():
        if entry.get("comment"):
            lines.append(f"/** {entry['comment']} */")
        lines.append(f"export const {name}: number = {literal(entry)};")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render(spec: dict[str, dict] | None = None) -> dict[Path, str]:
    spec = load_spec() if spec is None else spec
    return {PY_PATH: render_python(spec), ETS_PATH: render_ets(spec)}


def stale(paths: dict[Path, str]) -> list[Path]:
    """返回内容与磁盘不一致（或缺失）的生成物路径。"""
    out = []
    for path, text in paths.items():
        try:
            current = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            out.append(path)
            continue
        if current != text:
            out.append(path)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="只校验生成物是否与 protocol.json 一致，不写文件")
    args = ap.parse_args(argv)

    paths = render()
    out_of_date = stale(paths)

    if args.check:
        if not out_of_date:
            print(f"OK: {len(paths)} 个生成物与 {SPEC_REL} 一致")
            return 0
        for path in out_of_date:
            print(f"过期: {path.relative_to(ROOT)}", file=sys.stderr)
        print(f"运行 `{REGEN_CMD}` 重新生成。", file=sys.stderr)
        return 1

    for path, text in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"写入 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

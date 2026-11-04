"""协议常量单一来源（protocol/protocol.json）的一致性测试。

存在的理由：协议常量曾被抄成三份（config.py / PushServer.ets / docs），
并且**已经实际漂移** —— 魔数、探针长度、在途上限、重试次数全对不上。
生成器消除了第三份拷贝，本测试保证两侧生成物始终与 JSON 一致、逐键取值相同，
且协议常量不会在别处再长出一份。

本模块是**仓库级**守卫：它读仓库根的 `protocol/`、`scripts/`、
`app/entry/src/main/ets/common/` 等非 Python 包文件。新增对其它仓库文件的依赖时，
必须同步更新 MANIFEST.in —— sdist 不会自动收录 Python 包之外的文件，漏了就会让
从 sdist 运行的测试套件失败。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "protocol" / "protocol.json"
PY_GEN = ROOT / "binrunner" / "protocol.py"
ETS_GEN = ROOT / "app" / "entry" / "src" / "main" / "ets" / "common" / "Protocol.ets"
GENERATOR = ROOT / "scripts" / "gen_protocol.py"

_PY_RE = re.compile(r"^([A-Z][A-Z0-9_]*) = (0x[0-9A-Fa-f]+|\d+)$", re.MULTILINE)
_ETS_RE = re.compile(
    r"^export const ([A-Z][A-Z0-9_]*): number = (0x[0-9A-Fa-f]+|\d+);$", re.MULTILINE
)


def spec() -> dict:
    """常量表：名字 → 条目。"""
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))["constants"]


def spec_values() -> dict[str, int]:
    """常量表：名字 → 整数值（十六进制字面量按 16 进制解析）。"""
    return {name: int(str(entry["value"]), 0) for name, entry in spec().items()}


def test_generated_files_are_up_to_date():
    """生成物必须与 protocol.json 同步 —— 手改生成物会在这里被挡住。"""
    r = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert r.returncode == 0, (
        "生成物与 protocol/protocol.json 不一致；"
        f"运行 `python3 scripts/gen_protocol.py`：\n{r.stdout}{r.stderr}"
    )


def test_python_side_matches_spec():
    values = spec_values()
    got = {k: int(v, 0) for k, v in _PY_RE.findall(PY_GEN.read_text(encoding="utf-8"))}
    assert got == values, "binrunner/protocol.py 与 protocol.json 不一致"


def test_arkts_side_matches_spec():
    """跨语言一致性：设备侧拿到的值必须与 CLI 逐字节相同。"""
    values = spec_values()
    got = {k: int(v, 0) for k, v in _ETS_RE.findall(ETS_GEN.read_text(encoding="utf-8"))}
    assert got == values, "Protocol.ets 与 protocol.json 不一致"


def test_both_sides_export_identical_key_sets():
    """两侧必须导出同一批键 —— 少一个就是运行时才发现的 undefined。"""
    py_keys = set(_PY_RE.findall(PY_GEN.read_text(encoding="utf-8")))
    ets_keys = set(_ETS_RE.findall(ETS_GEN.read_text(encoding="utf-8")))
    assert {k for k, _ in py_keys} == {k for k, _ in ets_keys}
    assert {k for k, _ in py_keys} == set(spec())


def test_generated_files_are_marked_do_not_edit():
    assert "DO NOT EDIT" in ETS_GEN.read_text(encoding="utf-8")
    assert "请勿手改" in PY_GEN.read_text(encoding="utf-8")


def test_magics_do_not_collide_with_v1_name_length():
    """三个魔数都必须 > 256：否则旧设备会把它们当成 v1 的 nameLen。

    这不是风格问题 —— PULL 魔数若 ≤256，旧设备会按 nameLen 解析出一个错误
    的路径并照着读盘。
    """
    v = spec_values()
    magics = {k: v[k] for k in ("RESUME_MAGIC", "PULL_MAGIC", "PULL_MAGIC_V2")}
    for key, value in magics.items():
        assert value > 256, f"{key} 必须大于 v1 nameLen 上限"
    assert len(set(magics.values())) == len(magics), "魔数之间不得重值"


def test_protocol_constants_are_not_redefined_locally():
    """协议常量不得在别处再定义一份 —— 那正是本轮漂移的成因。"""
    config = (ROOT / "binrunner" / "config.py").read_text(encoding="utf-8")
    for key in spec():
        assert not re.search(rf"^{key}\s*=", config, re.MULTILINE), (
            f"{key} 在 config.py 又定义了一份；它属于 protocol/protocol.json"
        )

    push_server = (
        ROOT / "app" / "entry" / "src" / "main" / "ets" / "common" / "PushServer.ets"
    ).read_text(encoding="utf-8")
    for key in spec():
        assert not re.search(rf"\b(const|readonly)\s+{key}\b", push_server), (
            f"{key} 在 PushServer.ets 又定义了一份；应从 './Protocol' 导入"
        )

#!/usr/bin/env python3
"""把 Python 包版本同步到 App（HAP）版本。

版本号唯一来源保持不变：``binrunner/__init__.py`` 的 ``__version__``
（pyproject.toml 经 [tool.setuptools.dynamic] 读取同一处）。

同步规则（写入 app/AppScope/app.json5）：
- ``versionName``  = ``__version__``（如 "1.1.2"）
- ``versionCode``  = ``major*1_000_000 + minor*1_000 + patch``（如 1.1.2 → 1001002）

``versionCode`` 只允许单调递增（App 升级靠 hdc install 版本比较，
回退会导致已安装设备无法覆盖安装）。

用法：
    python3 scripts/sync_app_version.py            # 默认读 binrunner/__init__.py
    python3 scripts/sync_app_version.py --version 2.0.0   # 显式指定
    python3 scripts/sync_app_version.py --check    # 只校验是否已同步，不写文件

`--check` 供 CI 使用：`hap-sign.yml` 直接调 hvigorw、**不走 build.sh**，因此不会自动同步；
不一致就失败，避免发布 versionName 与包版本不符的 HAP（那会让 `br setup` 误判「已安装」）。
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "binrunner" / "__init__.py"
APP_JSON5 = ROOT / "app" / "AppScope" / "app.json5"

_VERSION_RE = re.compile(r'__version__\s*=\s*"([^"]+)"')
_VERSION_NAME_RE = re.compile(r'("versionName"\s*:\s*")[^"]*(")')
_VERSION_NAME_VALUE_RE = re.compile(r'"versionName"\s*:\s*"([^"]*)"')
_VERSION_CODE_RE = re.compile(r'"versionCode"\s*:\s*(\d+)')


def read_current_version() -> str:
    m = _VERSION_RE.search(VERSION_FILE.read_text(encoding="utf-8"))
    if not m:
        sys.exit(f"error: 无法在 {VERSION_FILE} 中找到 __version__")
    return m.group(1)


def version_code(ver: str) -> int:
    parts = ver.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        sys.exit(
            f"error: 版本号 {ver!r} 不是 X.Y.Z 纯数字格式，无法映射 versionCode"
        )
    major, minor, patch = (int(p) for p in parts)
    return major * 1_000_000 + minor * 1_000 + patch


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--version",
        metavar="X.Y.Z",
        help="显式指定版本号（默认读取 binrunner/__init__.py 的 __version__）",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="只校验 app.json5 是否已与版本号同步，不写文件；不一致则非零退出",
    )
    args = ap.parse_args()

    ver = args.version or read_current_version()
    new_code = version_code(ver)

    if not APP_JSON5.exists():
        sys.exit(f"error: 找不到 {APP_JSON5}")
    text = APP_JSON5.read_text(encoding="utf-8")

    old_code_m = _VERSION_CODE_RE.search(text)
    if not old_code_m:
        sys.exit(f"error: 无法在 {APP_JSON5} 中找到 versionCode")
    old_code = int(old_code_m.group(1))

    if new_code < old_code:
        sys.exit(
            f"error: versionCode 会从 {old_code} 回退到 {new_code}（{ver}），"
            "已安装设备将无法覆盖安装（hdc install 拒绝降级）。请勿回退版本号。"
        )

    new_text = _VERSION_NAME_RE.sub(rf'\g<1>{ver}\g<2>', text, count=1)
    new_text = _VERSION_CODE_RE.sub(
        lambda m: f'"versionCode": {new_code}', new_text, count=1
    )

    if args.check:
        if new_text == text:
            print(f"OK: app.json5 与版本号一致（versionName={ver}, versionCode={new_code}）")
            return
        m = _VERSION_NAME_VALUE_RE.search(text)
        cur_name = m.group(1) if m else "?"
        sys.exit(
            f"error: app.json5 未与版本号同步（{APP_JSON5.relative_to(ROOT)}）\n"
            f"  当前 versionName={cur_name}, versionCode={old_code}\n"
            f"  应为 versionName={ver}, versionCode={new_code}\n"
            f"  运行 `python3 scripts/sync_app_version.py` 修复后提交（build.sh 会自动执行）"
        )

    if new_text == text:
        print(f"app.json5 已是最新（versionName={ver}, versionCode={new_code}），无需修改")
        return

    APP_JSON5.write_text(new_text, encoding="utf-8")
    print(f"app.json5 同步完成：versionName={ver}, versionCode={old_code} → {new_code}")


if __name__ == "__main__":
    main()

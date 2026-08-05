# BinRunner 打包发布方案

## 概述

将 BinRunner 打包为 `pip install binrunner` 一条命令即可用的开发者工具。
第三方开发者无需构建 HAP、无需 DevEco Studio、无需了解鸿蒙签名机制。

## 发布物

```
pip install binrunner          # 基础版 (~3MB)：执行平台 + CLI
```

| 内容 | 体积 |
|---|---|
| CLI (`br`) | ~50KB |
| HAP（ELF loader + PushServer + libhello.so） | ~1MB |
| pip 包总计 | ~2MB |

HAP 只保留 `libhello.so` 作为验证二进制。`libbenchmark.so`、`libmindspore-lite.so`、
`mobilenetv2.ms` 均不打包（第三方开发者按需自行 `br push`）。

## 基础 HAP

从当前工程保留核心链路，剥离 ML 组件：

```
entry/libs/arm64-v8a/
└── libhello.so              # 静态 hello（exit=42 验证用）

# 以下不打包（第三方开发者按需 br push）：
#   libbenchmark.so, libmindspore-lite.so, mobilenetv2.ms
```

App 代码不变，PushServer + 内存 ELF loader + NAPI 全部保留。

构建：`hvigorw assembleApp -p buildMode=debug`（debug 签名，jit prctl 必需）。

CI 构建时手动删掉 `libmindspore-lite.so` 和 `mobilenetv2.ms` 再打包，
或新增 build-profile product `release` 控制。

## pip 包结构

```
binrunner/
├── pyproject.toml
├── README.md
├── binrunner/
│   ├── __init__.py
│   ├── __main__.py            # 从 tools/binrunner.py 移入
│   └── data/
│       └── binrunner.hap      # 内嵌 HAP（CI 构建产物）
```

`pyproject.toml`：

```toml
[project]
name = "binrunner"
version = "1.0.0"
description = "Run native Linux binaries on HarmonyOS NEXT retail devices"
requires-python = ">=3.9"
dependencies = []
license = "MIT"
readme = "README.md"

[project.scripts]
br = "binrunner.__main__:main"

[tool.setuptools.package-data]
binrunner = ["data/*.hap"]
```

## 自动安装

需要 App 的命令（`br run` / `br push` / `br ls` / `br rm` / `br logs`）
在执行前自动检测，未安装时从 pip 包内提取 HAP 并 `hdc install`。

**不触发安装**的命令：`br version` / `br devices` / `br forward` — 这些只读/纯 PC 操作不需要 App。

```python
# 需要 App 的命令调用链
def cmd_run(udid, cmdline, timeout):
    ensure_app(udid)     # ← 首次自动安装
    # ... 原有逻辑

def push_file(udid, local, remote, port):
    ensure_app(udid)     # ← 同上
    # ... 原有逻辑
```

`br version` 未安装时：
```
BinRunner CLI 1.0.0
Device HAP   not installed (run `br setup` or any command to auto-install)
```

—— 不触发安装，仅提示。

### `br setup`（可选）

```bash
br setup                         # 手动预装（多设备批量、CI 脚本）
br setup --reinstall             # 强制覆盖升级
br setup --device UDID           # 指定设备
```

### `br version`

```bash
br version
# BinRunner CLI 1.0.0
# Device HAP   1.0.0 (com.example.binrunner)
```

### 其他命令不变

`br devices` / `br push` / `br run` / `br ls` / `br rm` / `br logs` / `br forward`

## 用户视角

```bash
# === 首次使用（一步到位） ===
# 1. 安装 Command Line Tools（一次性，获得 hdc）
#    华为官网 → 下载 → 解压 → PATH

# 2. 安装 BinRunner
pip install binrunner

# 3. 直接使用（需要 App 的命令首次自动安装 HAP）
br run "hello"                  # 检测未安装 → 自动 hdc install → 执行
br version                      # 仅查看版本，不触发安装

# === 日常使用 ===
aarch64-unknown-linux-ohos-clang -O2 -static myapp.c -o myapp
br push ./myapp
br run "myapp --flag=value"

# HAP 永远不动，自动升级靠 br setup --reinstall
```

## 版本与升级

| 场景 | 操作 |
|---|---|
| CLI 升级 | `pip install --upgrade binrunner` |
| HAP 升级 | `br setup --reinstall`（保留 filesDir/bin/ 下的用户文件） |
| 版本检查 | `br version` |

## CI/CD（GitHub Actions）

```yaml
name: Release
on:
  push:
    tags: ['v*']
jobs:
  release:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Build HAP (basic)
        run: |
          export DEVECO_SDK_HOME=/Applications/DevEco-Studio.app/Contents/sdk
          export PATH="$DEVECO_SDK_HOME/.../toolchains:$PATH"
          rm -f entry/libs/arm64-v8a/libbenchmark.so
          rm -f entry/libs/arm64-v8a/libmindspore-lite.so
          rm -f entry/src/main/resources/rawfile/mobilenetv2.ms
          hvigorw assembleApp -p buildMode=debug --no-daemon
          cp entry/build/default/outputs/default/entry-default-signed.hap binrunner/data/binrunner.hap
      - name: Build wheel
        run: |
          pip install build
          python -m build
      - name: Publish
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
        run: twine upload dist/*
```

## 证书管理

| 问题 | 方案 |
|---|---|
| debug 证书 1 年有效 | CI 每次构建重新签名（DevEco CLI 非交互式） |
| 过期提示 | `br setup` 安装时检测 HAP 内证书有效期，临近过期发出 warning |

## 实施文件

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | pip 包元数据 |
| `binrunner/__init__.py` | 空文件 |
| `binrunner/__main__.py` | CLI 全逻辑（含 `ensure_app()` 自动安装） |
| `binrunner/data/binrunner.hap` | 基础 HAP（CI 构建产物，gitignore） |
| `.github/workflows/release.yml` | 发布流水线 |

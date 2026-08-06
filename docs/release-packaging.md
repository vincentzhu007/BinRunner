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
| HAP（ELF loader + PushServer，无内置二进制） | ~800KB |
| `hello`（aarch64 静态 ELF） | ~800KB |
| pip 包总计 | ~2MB |

HAP 不内置任何二进制。`hello` 作为独立文件与 HAP 并列打包；
`br setup` 安装 HAP 后自动推送 `hello` 并执行，验证全链路正常。

## 基础 HAP

从当前工程保留核心链路。HAP 不内置任何二进制，
验证用 `hello` 作为独立文件与 HAP 并列打包，`br setup` 安装后自动推送并执行。

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
│   ├── __main__.py            # 从 binrunner 移入
│   └── data/
│       ├── binrunner.hap      # 内嵌 HAP（CI 构建产物）
│       └── hello              # 验证二进制（aarch64 静态 ELF）
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
br setup                         # 手动预装
br setup --reinstall             # 强制覆盖升级（保留推送文件）
# 设备选择：-t UDID（全局选项，与其他命令一致）
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

## 一键构建

```bash
export DEVECO_SDK_HOME="/path/to/sdk"
export OHOS_NDK="$DEVECO_SDK_HOME/default/openharmony/native"
./build.sh    # 编译 hello + 构建 HAP + 打包 wheel，一步到位
```

不包含任何硬编码路径，全部通过环境变量传入。

## CI/CD

GitHub Actions 工作流: [`.github/workflows/release.yml`](../.github/workflows/release.yml)，`v*` tag push 触发，调用 `./build.sh` 构建后发布 wheel 到 GitHub Release。无需 PyPI token。

```bash
git tag v1.0.0 && git push origin v1.0.0   # → 自动构建发布
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
| `binrunner/__init__.py` | 空包声明 |
| `binrunner/__main__.py` | CLI 全逻辑（`ensure_app` 自动安装、`_find_bundled` 资源查找） |
| `binrunner/data/` | 内嵌资源（HAP + hello，CI 构建产物，gitignored） |
| `examples/hello/hello.c` | hello 验证二进制源码 |
| `examples/hello/build.sh` | OHOS NDK 交叉编译脚本 |
| `.github/workflows/release.yml` | GitHub Actions 发布流水线 |

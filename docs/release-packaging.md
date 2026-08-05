# BinRunner 打包发布方案

## 概述

将 BinRunner 打包为 `pip install binrunner` 一条命令即可用的开发者工具。
第三方开发者无需构建 HAP、无需 DevEco Studio、无需了解鸿蒙签名机制。

## 发布物

```
pip install binrunner          # 基础版 (~3MB)：执行平台 + CLI
pip install binrunner-ml       # ML 版 (~20MB)：基础版 + mindspore-lite + mobilenetv2 模型
```

| 包名 | 内容 | HAP 体积 | pip 包体积 |
|---|---|---|---|
| `binrunner` | CLI + 基础 HAP（hello, benchmark, ELF loader, PushServer） | ~1.5MB | ~3MB |
| `binrunner-ml` | 基础版 + libmindspore-lite.so + mobilenetv2.ms | ~20MB | ~22MB |

## 基础 HAP

从当前工程剥离 ML 组件，保留核心链路：

```
entry/libs/arm64-v8a/
├── libhello.so              # 5KB 静态 hello（exit=42）
├── libbenchmark.so          # 568KB 动态 benchmark（基础性能测试）
└── (不含 libmindspore-lite.so ← 移至 binrunner-ml)

entry/src/main/resources/rawfile/
└── (不含 mobilenetv2.ms ← 移至 binrunner-ml)
```

App 代码不变，PushServer + 内存 ELF loader + NAPI 全部保留。

构建配置新增一个 `product` 区分：

```json5
// build-profile.json5
"products": [
  { "name": "default",  "buildMode": "debug" },   // 全量（开发）
  { "name": "release",  "buildMode": "debug" }    // 基础版（发布）
]
```

CI 构建两个 HAP：全量 → `binrunner-ml`，精简 → `binrunner`。

## pip 包结构

```
binrunner/
├── pyproject.toml
├── README.md
├── binrunner/
│   ├── __init__.py
│   ├── __main__.py            # 从 tools/binrunner.py 移入，版本化
│   └── data/
│       └── binrunner.hap      # 内嵌 HAP（CI 构建产物）
│
├── binrunner_ml/              # 独立包，依赖 binrunner
│   ├── __init__.py
│   └── data/
│       └── binrunner-ml.hap
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

[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.backends._legacy:_Backend"

[tool.setuptools.package-data]
binrunner = ["data/*.hap"]
```

## 新增命令

### `br setup`

```bash
br setup                         # 从包内提取 HAP，hdc install 到手机
br setup --check                 # 仅检查，不安装
br setup --reinstall             # 覆盖安装（保留数据）
br setup --device UDID           # 指定设备
```

逻辑：

```python
def cmd_setup(udid=None, reinstall=False, check=False):
    # 1. 找到包内 HAP
    hap_path = importlib.resources.files("binrunner.data").joinpath("binrunner.hap")
    
    # 2. 检查 hdc + 设备
    hdc = find_hdc()
    
    # 3. 检查已有版本
    installed_ver = get_installed_version(udid)
    
    # 4. 安装
    if check:
        print(f"Bundled HAP: {hap_path} ({hap_path.stat().st_size} bytes)")
        print(f"Device: {installed_ver or 'not installed'}")
    else:
        install_hap(udid, hap_path, reinstall)
        print_version_info()
```

### `br version`

```bash
br version                       # 显示 CLI 版本 + 设备端版本
# BinRunner CLI: 1.0.0
# Device HAP:    1.0.0 (com.example.binrunner)
```

## 用户视角

```bash
# === 首次使用 ===
# 1. 安装 Command Line Tools（一次性）
#    从华为官网下载，解压，配置 PATH → 获得 hdc

# 2. 安装 BinRunner
pip install binrunner

# 3. 安装到手机
br setup
# → Detected device: 4VF0225717009856
# → Installing binrunner 1.0.0...
# → OK

# 4. 编译自己的二进制
aarch64-unknown-linux-ohos-clang -O2 -static myapp.c -o myapp

# 5. 推送 + 执行
br push ./myapp
br run "myapp"
# → stdout 直接显示，退出码透传

# === 日常使用（仅步骤 4-5） ===
# HAP 不需要重新安装，二进制变更只需要 br push
```

## 版本与升级

| 场景 | 操作 |
|---|---|
| CLI 升级 | `pip install --upgrade binrunner` |
| HAP 升级 | `br setup --reinstall`（覆盖安装，保留 PushServer 推送的文件） |
| 版本检查 | `br version` |
| 回退 | `pip install binrunner==1.0.0 && br setup --reinstall` |

HAP 升级时 `filesDir/bin/` 下的用户文件保留（`bm install -r` 不删数据）。

## CI/CD 发布流水线

```
main 分支 push
  │
  ├── Build base HAP (不含 ML)
  │     └── hvigorw assembleApp -p buildMode=debug -p product=release
  │
  ├── Build full HAP (含 ML)
  │     └── hvigorw assembleApp -p buildMode=debug -p product=default
  │
  ├── Build Python wheel
  │     ├── binrunner (基础 HAP)
  │     └── binrunner-ml (全量 HAP)
  │
  ├── Upload to PyPI
  │     ├── binrunner==1.0.0
  │     └── binrunner-ml==1.0.0
  │
  └── Tag release: v1.0.0
```

GitHub Actions 模板：

```yaml
name: Release
on:
  push:
    tags: ['v*']
jobs:
  build:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - name: Build HAP
        run: |
          export DEVECO_SDK_HOME=/Applications/DevEco-Studio.app/Contents/sdk
          export PATH="$DEVECO_SDK_HOME/.../toolchains:$PATH"
          hvigorw assembleApp -p buildMode=debug -p product=release --no-daemon
      - name: Build wheel
        run: |
          pip install build
          python -m build
      - name: Publish to PyPI
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
        run: twine upload dist/*
```

## 证书管理

| 问题 | 方案 |
|---|---|
| debug 证书有效期 1 年 | CI 构建时自动续签（DevEco CLI 支持非交互签名） |
| 证书过期后 HAP 不可用 | `br setup` 检测到 HAP 签名过期时提示升级 |
| 零售机 sideload 限制 | 目前未发现限制，华为未检查 debug 证书的 device 绑定 |

## 平台适配

当前 `find_hdc()` 硬编码 macOS 路径。适配方案：

```python
import platform

def find_hdc() -> str:
    # 1. PATH
    if shutil.which("hdc"): return shutil.which("hdc")
    
    # 2. 默认安装路径
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
            "~/Library/Huawei/Sdk/default/openharmony/toolchains/hdc",
        ]
    elif system == "Linux":
        candidates = [
            "~/Huawei/Sdk/default/openharmony/toolchains/hdc",
            "/opt/Huawei/Sdk/default/openharmony/toolchains/hdc",
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\Huawei\Sdk\default\openharmony\toolchains\hdc.exe",
        ]
    
    for c in candidates:
        p = os.path.expanduser(c)
        if os.path.exists(p): return p
    
    sys.exit("hdc not found. Install HarmonyOS Command Line Tools.")
```

## 实施路线

| 阶段 | 内容 | 工作量 |
|---|---|---|
| **1. 基础 pip 包** | pyproject.toml + CLI 移入包结构 + `br setup` + `br version` | 1-2h |
| **2. 精简 HAP** | build-profile 新增 release product，剥离 ML 组件 | 30min |
| **3. CI/CD** | GitHub Actions 自动构建 + 发布到 PyPI | 1h |
| **4. 跨平台 hdc** | find_hdc 适配 macOS/Linux/Windows | 30min |
| **5. 文档** | 发布到 PyPI 的 README（面向第三方开发者） | 30min |

## 实施文件

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | pip 包元数据 |
| `binrunner/__init__.py` | 空 |
| `binrunner/__main__.py` | CLI 逻辑（从 tools/binrunner.py 迁移） |
| `binrunner/data/binrunner.hap` | 基础 HAP（CI 或本地构建） |
| `.github/workflows/release.yml` | 发布流水线 |

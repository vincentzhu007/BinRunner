---
name: develop-binrunner
description: Develop the BinRunner HarmonyOS app — build, deploy, test, add binaries, debug NAPI/ArkTS/ELF loader code. Use when working on this project, modifying the HAP, adding features, or debugging device-side issues.
---

# Develop BinRunner

## Quick reference

```bash
# Build wheel (compile hello + HAP + package)
./build.sh                        # one command, see dist/

# Build & deploy HAP only (dev mode)
# 签名配置不入库（受控 build-profile.json5 里 signingConfigs 是空数组）：
#   本机配置存档在 .build/build-profile.local.json5（gitignore），build.sh 注入后还原；
#   没有该文件时用 .build/keystore 自签材料（HAP 不可安装）。DevEco Studio 自动签名会
#   就地改写 app/build-profile.json5 —— 那是本机配置，提交前 git checkout 还原。
export PATH="/Applications/DevEco-Studio.app/Contents/tools/node/bin:/Applications/DevEco-Studio.app/Contents/tools/ohpm/bin:/Applications/DevEco-Studio.app/Contents/tools/hvigor/bin:/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
export DEVECO_SDK_HOME="/Applications/DevEco-Studio.app/Contents/sdk"
ohpm install --all
bash build.sh
hdc install -r binrunner/data/binrunner.hap

# CLI smoke test（在仓库根目录运行）
# 必须用 `python3 -m binrunner`：把目录名直接交给 python3（python3 ./binrunner）会把
# binrunner/ 目录本身放进 sys.path[0]，那里没有 binrunner 包，于是静默改用**已安装**的
# 同名包 —— 可能是另一个版本/checkout，不报任何错（最难查）
python3 -m binrunner run "hello" --timeout 20
```

## Architecture

```
binrunner/          Host CLI (Python, zero deps) — 包；入口 `python3 -m binrunner`（仓库根目录）
├── cli.py          argparse 与子命令分发
├── hdc.py          hdc 封装与设备选择
├── transfer.py     传输公共件：fmt_size、u64 编解码、open_connection、recv_exactly、远端名校验
├── push.py         push 策略：v1/v2 头部、ACK 流控、重试自愈、目录递归
├── pull.py         pull 策略：v2 续传协商、.part 原子落盘、保活
├── runner.py       执行与 hilog 轮询
├── hilog.py        单遍 hilog 解析器（HilogParser）
├── keepalive.py    传输期间屏幕保活 + fport 巡检
├── provision.py    设备侧 App 安装、版本、全链路验证
├── protocol.py     生成：跨语言协议常量（勿手改）
└── config.py       仅主机侧策略常量
protocol/protocol.json                       跨语言协议常量唯一来源（改这里，再跑生成器）
scripts/gen_protocol.py                      生成 binrunner/protocol.py + Protocol.ets
app/entry/src/main/
├── ets/entryability/EntryAbility.ets   App entry, PushServer init, cmd dispatch
├── ets/common/BinRunner.ets            Cmd router, @-expansion, logLines, ls/rm builtins
├── ets/common/PushServer.ets           TCP push/pull 协议（端口由 protocol.json 定义）
├── ets/common/Protocol.ets             生成：设备侧协议常量（勿手改）
└── cpp/napi_init.cpp                   NAPI bridge — fork + memfd + ELF loader orchestration
app/third_party/elf/src/loader.c            In-memory ELF loader (MikhailProg/elf + HMOS patches)
```

## Key workflows

### Add a new binary (packaged in HAP)

1. Cross-compile with OHOS NDK: `OHOS_NDK=... ./examples/hello/build.sh`
2. Output: `app/entry/libs/arm64-v8a/lib<name>.so`
3. Rebuild & reinstall HAP
4. Test: `br run "<name>"`

### Add a new built-in command (ArkTS)

1. Add handler in `BinRunner.run()` (like `ls`/`rm`)
2. Add subcommand to `binrunner/cli.py` 的 parser（协议常量只改 `protocol/protocol.json` 后重新生成）
3. Rebuild & test

### Modify native code (napi_init.cpp / loader.c)

1. Edit C++ files
2. Rebuild (CMake auto-detects changes)
3. Reinstall HAP
4. Test with `br run` + check hilog

### Debug device issues

```bash
# Raw log check
hdc shell "hilog -x" | grep "BinRunner:"

# Probe filesystem
br run "probe"
br run "probe2"
br ls "@/bin"
```

### Add a feature to CLI (binrunner/ 包)

1. 改对应模块：参数解析在 `binrunner/cli.py`，push/pull/执行行为在
   `push.py` / `pull.py` / `runner.py` —— 无需重新构建 HAP
2. 测试：`python3 -m binrunner <command>`（或 alias 后的 `br <command>`）
3. 同步 [docs/cli-reference.md](../../../docs/cli-reference.md)

## Conventions

| Rule | Rationale |
|---|---|
| Debug build only (`buildMode=debug`) | jit prctl requires debug signature |
| Binary in libs: `lib<name>.so` | HAP packager naming requirement |
| Pushed binary: any name, flat or subdir | PushServer creates dirs, LD_LIBRARY_PATH only root-level |
| Report via `logLines('<<<', report)` | CLI parses `<<< ` prefixed lines, ends on `<<< END` |
| 2ms delay between hilog calls | Prevents socket overflow while keeping prefix intact |
| `@` for filesDir expansion | Avoids host shell tilde expansion |
| run_id prefix in all hilog output | Concurrent execution isolation |

## Pitfalls

- **Device HAP outdated**: if a feature doesn't work, rebuild + reinstall HAP first
- **hilog socket overflow**: don't batch lines with `\n` (hilog strips prefix on continuation lines)
- **bundle libs random read**: files in HAP libs dir return bad data on lseek+read — must copy to memfd first
- **execv always fails**: EACCES on retail — the ELF loader fallback is the normal path, not an error
- **hdc install -r force-stops app**: PushServer needs app restart; `br run` auto-launches it
- **Never commit `signingConfigs`**: DevEco auto-signing rewrites `app/build-profile.json5`
  in place (local material paths + encrypted passwords); keep the machine copy in
  `.build/build-profile.local.json5` (gitignored) and `git checkout -- app/build-profile.json5`
  before committing

## Files to update together

When changing a feature end-to-end:

| Layer | Files |
|---|---|
| 跨语言协议常量 | 改 `protocol/protocol.json`，再跑 `scripts/gen_protocol.py` 生成 `binrunner/protocol.py` + `app/entry/src/main/ets/common/Protocol.ets`（生成物入库，勿手改） |
| CLI only | `binrunner/` 下相关模块 |
| CLI + built-in cmd | `binrunner/cli.py`（+ 相关模块） + `BinRunner.ets` |
| Full stack | `binrunner/` + `BinRunner.ets` + maybe `napi_init.cpp` |
| Docs | `README.md`, `docs/cli-reference.md`, `docs/transfer-spec.md`, `docs/concurrency-spec.md` |

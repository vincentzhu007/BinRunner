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
export PATH="/Applications/DevEco-Studio.app/Contents/tools/node/bin:/Applications/DevEco-Studio.app/Contents/tools/ohpm/bin:/Applications/DevEco-Studio.app/Contents/tools/hvigor/bin:/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
export DEVECO_SDK_HOME="/Applications/DevEco-Studio.app/Contents/sdk"
ohpm install --all
hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon
hdc install -r entry/build/default/outputs/default/entry-default-signed.hap

# CLI smoke test
alias br="python3 tools/binrunner.py"
br run "hello" --timeout 20
```

## Architecture

```
tools/binrunner.py          Host CLI (Python, zero deps) — hdc fport + push + run + logs
entry/src/main/
├── ets/entryability/EntryAbility.ets   App entry, PushServer init, cmd dispatch
├── ets/common/BinRunner.ets            Cmd router, @-expansion, logLines, ls/rm builtins
├── ets/common/PushServer.ets           TCP :8888, file receive protocol
└── cpp/napi_init.cpp                   NAPI bridge — fork + memfd + ELF loader orchestration
third_party/elf/src/loader.c            In-memory ELF loader (MikhailProg/elf + HMOS patches)
```

## Key workflows

### Add a new binary (packaged in HAP)

1. Cross-compile with OHOS NDK: `tools/build_hello.sh <name>`
2. Output: `entry/libs/arm64-v8a/lib<name>.so`
3. Rebuild & reinstall HAP
4. Test: `br run "<name>"`

### Add a new built-in command (ArkTS)

1. Add handler in `BinRunner.run()` (like `ls`/`rm`)
2. Add subcommand to `binrunner.py` main parser
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

### Add a feature to CLI (binrunner.py)

1. Edit `tools/binrunner.py` — no rebuild needed
2. Test: `br <command>`
3. Update [docs/cli-reference.md](../../docs/cli-reference.md)

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

## Files to update together

When changing a feature end-to-end:

| Layer | Files |
|---|---|
| CLI only | `tools/binrunner.py` |
| CLI + built-in cmd | `binrunner.py` + `BinRunner.ets` |
| Full stack | `binrunner.py` + `BinRunner.ets` + maybe `napi_init.cpp` |
| Docs | `README.md`, `docs/cli-reference.md`, `docs/push-spec.md`, `docs/concurrency-spec.md` |

#!/bin/bash
# 用 OHOS NDK 交叉编译 hello 测试二进制（静态链接，无依赖）。
# 用法: OHOS_NDK=/path/to/ohos/native ./tools/hello/build.sh
# 产物: tools/hello/hello（aarch64 ELF，静态链接）
#
# OHOS_NDK 示例路径:
#   DevEco Studio: /Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native
#   Command Line Tools: $HOME/harmonyos/sdk/default/openharmony/native
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -z "$OHOS_NDK" ]; then
  echo "请设置 OHOS_NDK 环境变量指向 OHOS native SDK 根目录"
  echo "示例: OHOS_NDK=/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native"
  exit 1
fi

CLANG="$OHOS_NDK/llvm/bin/aarch64-unknown-linux-ohos-clang"
SYSROOT="$OHOS_NDK/sysroot"

if [ ! -f "$CLANG" ]; then
  echo "找不到编译器: $CLANG"
  exit 1
fi

"$CLANG" \
  --sysroot="$SYSROOT" \
  -O2 -static \
  "$SCRIPT_DIR/hello.c" \
  -o "$SCRIPT_DIR/hello"

echo "OK: $SCRIPT_DIR/hello"

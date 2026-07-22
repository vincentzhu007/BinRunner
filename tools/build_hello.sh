#!/bin/bash
# 用 DevEco 自带的 OHOS NDK 交叉编译测试二进制。
# 用法: ./tools/build_hello.sh [输出名，默认 hello]
# 产物: entry/libs/arm64-v8a/lib<名>.so（HAP 打包要求 lib*.so 命名）
set -e

NAME="${1:-hello}"
NATIVE=/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native

if [ ! -d "$NATIVE" ]; then
  echo "未找到 OHOS native SDK，请修改脚本中的 NATIVE 路径"
  exit 1
fi

"$NATIVE/llvm/bin/aarch64-unknown-linux-ohos-clang" \
  --sysroot="$NATIVE/sysroot" \
  -O2 -static \
  "$(dirname "$0")/hello.c" \
  -o "$(dirname "$0")/../entry/libs/arm64-v8a/lib${NAME}.so"

# 同步到 hnp 源目录（如有）
if [ -d "$(dirname "$0")/../hnp/${NAME}/bin" ]; then
  cp "$(dirname "$0")/../entry/libs/arm64-v8a/lib${NAME}.so" "$(dirname "$0")/../hnp/${NAME}/bin/${NAME}"
fi

echo "OK: entry/libs/arm64-v8a/lib${NAME}.so"

#!/bin/bash
# 一键构建 BinRunner wheel 包
# 用法:
#   export DEVECO_SDK_HOME="/path/to/sdk"   # HarmonyOS SDK 根目录
#   export OHOS_NDK="$DEVECO_SDK_HOME/default/openharmony/native"
#   ./build.sh
# 产物: dist/binrunner-*.whl
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 环境变量检查
if [ -z "$DEVECO_SDK_HOME" ]; then
  echo "请设置 DEVECO_SDK_HOME 指向 HarmonyOS SDK 根目录"
  echo "  例: export DEVECO_SDK_HOME=/path/to/sdk"
  exit 1
fi
if [ -z "$OHOS_NDK" ]; then
  echo "请设置 OHOS_NDK 指向 OHOS native SDK"
  echo "  例: export OHOS_NDK=\$DEVECO_SDK_HOME/default/openharmony/native"
  exit 1
fi

export PATH="$DEVECO_SDK_HOME/default/openharmony/toolchains:$PATH"

# 检查必需工具
for cmd in ohpm hvigorw aarch64-unknown-linux-ohos-clang; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "未找到 $cmd，请确认 Command Line Tools 已安装且 PATH 正确"
    exit 1
  fi
done

echo "=== Step 1/3: Build hello binary ==="
bash tools/hello/build.sh

echo ""
echo "=== Step 2/3: Build base HAP ==="
rm -f entry/libs/arm64-v8a/libbenchmark.so
rm -f entry/libs/arm64-v8a/libmindspore-lite.so
rm -f entry/src/main/resources/rawfile/mobilenetv2.ms
ohpm install --all
hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon

echo ""
echo "=== Step 3/3: Copy artifacts & build wheel ==="
mkdir -p binrunner/data
cp entry/build/default/outputs/default/entry-default-signed.hap binrunner/data/binrunner.hap
cp tools/hello/hello binrunner/data/hello
python3 -m pip install --quiet build 2>/dev/null
python3 -m build

echo ""
ls -lh dist/*.whl
echo "Done."

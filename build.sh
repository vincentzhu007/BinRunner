#!/bin/bash
# 一键构建 BinRunner wheel 包
# 用法: ./build.sh
# 产物: dist/binrunner-*.whl
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 1. DevEco 工具链路径
export DEVECO_SDK_HOME="${DEVECO_SDK_HOME:-/Applications/DevEco-Studio.app/Contents/sdk}"
export DEVECO_TOOLS="/Applications/DevEco-Studio.app/Contents/tools"
export PATH="$DEVECO_TOOLS/node/bin:$DEVECO_TOOLS/ohpm/bin:$DEVECO_TOOLS/hvigor/bin:$DEVECO_SDK_HOME/default/openharmony/toolchains:$PATH"

echo "=== Step 1/3: Build hello binary ==="
export OHOS_NDK="$DEVECO_SDK_HOME/default/openharmony/native"
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

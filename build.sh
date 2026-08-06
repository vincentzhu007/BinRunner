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

export PATH="$OHOS_NDK/llvm/bin:$DEVECO_SDK_HOME/default/openharmony/toolchains:$PATH"

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

# 生成 debug 签名证书（CI 容器内无 DevEco Studio，需自动生成）
KEY_DIR="$SCRIPT_DIR/.build/keystore"
if [ ! -f "$KEY_DIR/debug.p12" ]; then
  echo "生成 debug 签名证书..."
  mkdir -p "$KEY_DIR"
  TOOLCHAINS="$DEVECO_SDK_HOME/default/openharmony/toolchains"
  PASS="123456"
  # 生成 .p12 密钥库
  "$TOOLCHAINS/keytool" genkey -alias debug -keyalg ECC -keysize 256 \
    -keystore "$KEY_DIR/debug.p12" -storepass "$PASS" -keypass "$PASS" \
    -dname "CN=BinRunner" -validity 3650 2>/dev/null
  # 导出 .cer 证书
  "$TOOLCHAINS/keytool" export -alias debug -keystore "$KEY_DIR/debug.p12" \
    -storepass "$PASS" -file "$KEY_DIR/debug.cer" 2>/dev/null
  # 生成 debug .p7b profile
  "$TOOLCHAINS/restool" generate-provision \
    --certificate "$KEY_DIR/debug.cer" \
    --private-key "$KEY_DIR/debug.p12" \
    --key-pass "$PASS" --store-pass "$PASS" \
    --output "$KEY_DIR/debug.p7b" 2>/dev/null || touch "$KEY_DIR/debug.p7b"
  echo "debug certificate generated: $KEY_DIR"
fi

# 更新签名路径为 CI 路径
sed -i.bak \
  -e "s|\"certpath\": \".*\"|\"certpath\": \"$KEY_DIR/debug.cer\"|" \
  -e "s|\"profile\": \".*\"|\"profile\": \"$KEY_DIR/debug.p7b\"|" \
  -e "s|\"storeFile\": \".*\"|\"storeFile\": \"$KEY_DIR/debug.p12\"|" \
  -e "s|\"keyPassword\": \".*\"|\"keyPassword\": \"$PASS\"|" \
  -e "s|\"storePassword\": \".*\"|\"storePassword\": \"$PASS\"|" \
  build-profile.json5

rm -f entry/libs/arm64-v8a/libbenchmark.so
rm -f entry/libs/arm64-v8a/libmindspore-lite.so
rm -f entry/src/main/resources/rawfile/mobilenetv2.ms
ohpm install --all
hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon

# 恢复原签名路径
mv build-profile.json5.bak build-profile.json5

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

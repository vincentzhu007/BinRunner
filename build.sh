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

# 签名证书（hvigor 期望 material 子目录结构）
KEY_DIR="$SCRIPT_DIR/.build/keystore"
MATERIAL_DIR="$KEY_DIR/material"
CI_CERT_DIR="$SCRIPT_DIR/.github/docker/certs"

if [ ! -f "$MATERIAL_DIR/debug.p12" ]; then
  mkdir -p "$MATERIAL_DIR"

  if [ -f "$CI_CERT_DIR/debug.p12" ] && [ -f "$CI_CERT_DIR/debug.cer" ]; then
    echo "使用项目 CI 签名证书..."
    cp "$CI_CERT_DIR"/* "$MATERIAL_DIR/" 2>/dev/null || true
  else
    PASS="12345678901234567890123456789012"
    echo "生成 debug 签名证书（openssl）..."
    openssl ecparam -genkey -name prime256v1 -out "$MATERIAL_DIR/debug.key" 2>/dev/null
    openssl req -new -x509 -key "$MATERIAL_DIR/debug.key" -out "$MATERIAL_DIR/debug.cer" \
      -days 3650 -subj "/CN=BinRunner CI" 2>/dev/null
    openssl pkcs12 -export -in "$MATERIAL_DIR/debug.cer" -inkey "$MATERIAL_DIR/debug.key" \
      -out "$MATERIAL_DIR/debug.p12" -passout pass:"$PASS" 2>/dev/null
    touch "$MATERIAL_DIR/debug.p7b"
  fi
  echo "debug certificate: $MATERIAL_DIR"
fi

# 更新签名路径
sed -i.bak \
  -e "s|\"certpath\": \".*\"|\"certpath\": \"$MATERIAL_DIR/debug.cer\"|" \
  -e "s|\"profile\": \".*\"|\"profile\": \"$MATERIAL_DIR/debug.p7b\"|" \
  -e "s|\"storeFile\": \".*\"|\"storeFile\": \"$MATERIAL_DIR/debug.p12\"|" \
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

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

# 签名证书：优先使用项目自带的 CI 证书，其次本地 DevEco 证书，最后尝试自动生成
KEY_DIR="$SCRIPT_DIR/.build/keystore"
if [ ! -f "$KEY_DIR/debug.p12" ]; then
  mkdir -p "$KEY_DIR"
  PASS="123456"

  # 方案 A：项目内预置 CI 证书（最可靠）
  CI_CERT_DIR="$SCRIPT_DIR/.github/docker/certs"
  if [ -f "$CI_CERT_DIR/debug.p12" ] && [ -f "$CI_CERT_DIR/debug.cer" ]; then
    echo "使用项目 CI 签名证书..."
    cp "$CI_CERT_DIR/debug.p12" "$CI_CERT_DIR/debug.cer" "$CI_CERT_DIR/debug.p7b" "$KEY_DIR/" 2>/dev/null || true

  # 方案 B：OpenSSL 生成（标准 Linux 环境）
  elif command -v openssl &>/dev/null; then
    echo "生成 debug 签名证书（openssl）..."
    openssl ecparam -genkey -name prime256v1 -out "$KEY_DIR/debug.key" 2>/dev/null
    openssl req -new -x509 -key "$KEY_DIR/debug.key" -out "$KEY_DIR/debug.cer" \
      -days 3650 -subj "/CN=BinRunner CI" 2>/dev/null
    openssl pkcs12 -export -in "$KEY_DIR/debug.cer" -inkey "$KEY_DIR/debug.key" \
      -out "$KEY_DIR/debug.p12" -passout pass:"$PASS" 2>/dev/null
    touch "$KEY_DIR/debug.p7b"

  else
    echo "WARNING: 无签名工具，尝试用 SDK keytool（需要 Java）..."
    SDK_KEYTOOL="$DEVECO_SDK_HOME/default/openharmony/toolchains/keytool"
    if [ -x "$SDK_KEYTOOL" ]; then
      "$SDK_KEYTOOL" genkey -alias debug -keyalg ECC -keysize 256 \
        -keystore "$KEY_DIR/debug.p12" -storepass "$PASS" -keypass "$PASS" \
        -dname "CN=BinRunner CI" -validity 3650 2>/dev/null || true
      "$SDK_KEYTOOL" export -alias debug -keystore "$KEY_DIR/debug.p12" \
        -storepass "$PASS" -file "$KEY_DIR/debug.cer" 2>/dev/null || true
    fi
    touch "$KEY_DIR/debug.p7b"
  fi
  echo "debug certificate: $KEY_DIR"
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

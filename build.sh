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

# 注入过签名配置的 build-profile.json5 必须还原：构建失败/中断也要还原（trap 覆盖 EXIT）。
restore_profile() {
  # 用绝对路径：脚本中途 cd app，相对路径在失败退出时会指错（历史 bug）
  if [ -f "$SCRIPT_DIR/app/build-profile.json5.bak" ]; then
    mv "$SCRIPT_DIR/app/build-profile.json5.bak" "$SCRIPT_DIR/app/build-profile.json5"
  fi
}
trap restore_profile EXIT

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
bash examples/hello/build.sh

echo ""
echo "=== Step 2/3: Build base HAP ==="

# App 版本与 Python 包版本联动：versionName/versionCode 取自 binrunner.__version__
python3 scripts/sync_app_version.py

# 签名材料（仓库不入库，运行时注入；hvigor 在 certpath 父目录下找 material/ 子目录）：
#   来源优先级：
#     1) BINRUNNER_KEYSTORE_B64 / BINRUNNER_PROFILE_B64 / BINRUNNER_CERT_B64（base64，CI/发布）
#     2) 仓库 .github/docker/certs/（本地开发回退，勿提交生产私钥）
#     3) 本机 DevEco 签名配置 .build/build-profile.local.json5（见下方本地分支）
#   密码：KEYSTORE_PWD / KEY_ALIAS / KEY_PWD
#   三者都没有时：不注入签名配置，构建照常但产出**未签名 HAP**（不可安装，仅结构验证）。
#   （历史上这里的 openssl 自签回退是假的：hvigor 内嵌签名需要真实 profile，
#     空 p7b 会在 SignHap 报 00304033，所以已删除。）
KEY_DIR="$SCRIPT_DIR/.build/keystore"
CI_CERT_DIR="$SCRIPT_DIR/.github/docker/certs"
LOCAL_PROFILE="$SCRIPT_DIR/.build/build-profile.local.json5"

if [ ! -f "$KEY_DIR/debug.p12" ]; then
  mkdir -p "$KEY_DIR" "$KEY_DIR/material"

  # 1) 从环境变量（Secrets）还原签名材料
  if [ -n "${BINRUNNER_KEYSTORE_B64:-}" ] && [ -n "${BINRUNNER_PROFILE_B64:-}" ] && [ -n "${BINRUNNER_CERT_B64:-}" ]; then
    echo "使用 Secrets 签名证书（base64 还原）..."
    echo "$BINRUNNER_KEYSTORE_B64" | base64 -d > "$KEY_DIR/debug.p12"
    echo "$BINRUNNER_PROFILE_B64"  | base64 -d > "$KEY_DIR/debug.p7b"
    echo "$BINRUNNER_CERT_B64"     | base64 -d > "$KEY_DIR/debug.cer"
  elif [ -f "$CI_CERT_DIR/debug.p12" ] && [ -f "$CI_CERT_DIR/debug.cer" ]; then
    echo "使用项目 CI 签名证书（本地回退）..."
    cp -r "$CI_CERT_DIR"/* "$KEY_DIR/" 2>/dev/null || true
  else
    echo "无可用的签名材料（Secrets / .github/docker/certs / 本机签名配置都没有）"
  fi
  cp "$KEY_DIR"/debug.* "$KEY_DIR/material/" 2>/dev/null || true
  chmod 600 "$KEY_DIR"/*.p12 2>/dev/null || true
fi
if [ -f "$KEY_DIR/debug.p12" ]; then
  echo "debug certificate: $KEY_DIR"
fi

# 签名方式分流（受控的 build-profile.json5 里 signingConfigs 为空数组，材料一律运行时注入）：
#   CI/发布（有 Secrets，真实短密码）→ 清空 signingConfigs 让 hvigor 产「未签名 HAP」，
#     再改用 hap-sign-tool 签名（hap-sign-tool 接受任意长度密码；hvigor 内嵌签名要求
#     storePassword/keyPassword ≥32 字符或 DevEco 加密串，注入短明文会报 00303116）。
#   本地（无 Secrets）→ 注入本机签名配置后走 hvigor 内嵌签名，构建结束还原受控版本。
STORE_PWD="${KEYSTORE_PWD:-}"
KEY_ALIAS_INJ="${KEY_ALIAS:-}"
KEY_PWD_INJ="${KEY_PWD:-$STORE_PWD}"

CI_SIGN=0
if [ -n "${BINRUNNER_KEYSTORE_B64:-}" ] && [ -n "${BINRUNNER_PROFILE_B64:-}" ] \
   && [ -n "${BINRUNNER_CERT_B64:-}" ] && [ -n "$STORE_PWD" ]; then
  CI_SIGN=1
  echo "CI 签名模式：构建未签名 HAP，随后用 hap-sign-tool 签名"
  python3 - <<'PYEOF'
import re
from pathlib import Path

# 受控版本本来就是空 signingConfigs；这里再清一次是防线：万一有人把本机签名配置提交
# 进来，也不让 CI 用它签名（宁可产出未签名 HAP 走 Secrets 签名）。
p = Path("app/build-profile.json5")
s = p.read_text(encoding="utf-8")
s = re.sub(r'"signingConfigs"\s*:\s*\[.*?\]', '"signingConfigs": []', s, flags=re.S)
s = re.sub(r'\s*"signingConfig"\s*:\s*"[^"]*",?', '', s)
p.write_text(s, encoding="utf-8")
PYEOF
  HAP_SIGN_TOOL="${HAP_SIGN_TOOL:-}"
  if [ -z "$HAP_SIGN_TOOL" ] && [ -n "$DEVECO_SDK_HOME" ]; then
    HAP_SIGN_TOOL="$DEVECO_SDK_HOME/default/openharmony/toolchains/lib/hap-sign-tool.jar"
  fi
  if [ -z "$HAP_SIGN_TOOL" ] || [ ! -f "$HAP_SIGN_TOOL" ]; then
    HAP_SIGN_TOOL=$(find /opt "$SCRIPT_DIR" -name hap-sign-tool.jar 2>/dev/null | head -n 1 || true)
  fi
  if [ -z "$HAP_SIGN_TOOL" ] || [ ! -f "$HAP_SIGN_TOOL" ]; then
    echo "未找到 hap-sign-tool.jar（可设 HAP_SIGN_TOOL 指定）" >&2
    exit 1
  fi
  echo "hap-sign-tool: $HAP_SIGN_TOOL"
else
  # 本地：签名材料/口令不入库 —— 构建前注入，构建结束后还原受控版本（见下方 mv *.bak）。
  #   1) .build/build-profile.local.json5（本机 DevEco 配置，已 gitignore）存在则整份采用；
  #   2) 否则若 KEYSTORE_PWD 且 $KEY_DIR 有材料（.github/docker/certs 或 Secrets 还原），
  #      按 $KEY_DIR 材料注入一份；
  #   3) 两者都没有 → 不注入，hvigor 产出未签名 HAP（不可安装，仅用于结构验证）。
  python3 - "$LOCAL_PROFILE" "$KEY_DIR" "$STORE_PWD" "$KEY_ALIAS_INJ" "$KEY_PWD_INJ" <<'PYEOF'
import re
import sys
from pathlib import Path

local_profile, key_dir, pwd, alias, key_pwd = sys.argv[1:6]
profile = Path("app/build-profile.json5")
tracked = profile.read_text(encoding="utf-8")
Path("app/build-profile.json5.bak").write_text(tracked, encoding="utf-8")
have_material = (Path(key_dir) / "debug.p12").exists()
# 受控 products 里没有 "signingConfig"（空 signingConfigs 时 assembleApp 会报 00303107），
# 注入签名配置时要把它加回去；本机配置（DevEco 写出的）自带该字段。
ref_re = re.compile(r'("products"\s*:\s*\[\s*\{\s*\n(\s*)"name"\s*:\s*"default",)')


def with_ref(text: str) -> str:
    if '"signingConfig"' in text:
        return text
    return ref_re.sub(r'\1\n\2"signingConfig": "default",', text, count=1)

if Path(local_profile).exists():
    print(f"使用本机签名配置：{local_profile}")
    text = with_ref(Path(local_profile).read_text(encoding="utf-8"))
    if pwd:
        text = re.sub(r'"storePassword"\s*:\s*"[^"]*"', f'"storePassword": "{pwd}"', text)
        text = re.sub(r'"keyPassword"\s*:\s*"[^"]*"', f'"keyPassword": "{key_pwd or pwd}"', text)
    if alias:
        text = re.sub(r'"keyAlias"\s*:\s*"[^"]*"', f'"keyAlias": "{alias}"', text)
elif pwd and have_material:
    print(f"按 {key_dir} 的材料注入签名配置（KEYSTORE_PWD 已提供）")
    block = (
        '"signingConfigs": [\n'
        "      {\n"
        '        "name": "default",\n'
        '        "type": "HarmonyOS",\n'
        '        "material": {\n'
        f'          "certpath": "{key_dir}/debug.cer",\n'
        f'          "keyAlias": "{alias or "debugKey"}",\n'
        f'          "keyPassword": "{key_pwd or pwd}",\n'
        f'          "profile": "{key_dir}/debug.p7b",\n'
        '          "signAlg": "SHA256withECDSA",\n'
        f'          "storeFile": "{key_dir}/debug.p12",\n'
        f'          "storePassword": "{pwd}"\n'
        "        }\n"
        "      }\n"
        "    ]"
    )
    text = with_ref(re.sub(r'"signingConfigs"\s*:\s*\[.*?\]', block, tracked, flags=re.S))
else:
    print(
        f"未提供签名配置（{local_profile} 不存在，也没给 KEYSTORE_PWD）："
        "本次产出未签名 HAP，不可安装，仅用于打包/结构验证"
    )
    text = tracked

profile.write_text(text, encoding="utf-8")
PYEOF
fi

rm -f app/entry/libs/arm64-v8a/libbenchmark.so
rm -f app/entry/libs/arm64-v8a/libmindspore-lite.so
rm -f app/entry/src/main/resources/rawfile/mobilenetv2.ms
cd app
ohpm install --all
hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon

if [ "$CI_SIGN" -eq 1 ]; then
  # hvigor 产出的是未签名 HAP，这里用 Secrets 还原的证书/Profile 签名。
  # 输出沿用 hvigor 的 entry-default-signed.hap 命名，Step 3 复制逻辑不变。
  echo "=== CI 签名（hap-sign-tool）==="
  UNSIGNED=entry/build/default/outputs/default/entry-default-unsigned.hap
  SIGNED_OUT=entry/build/default/outputs/default/entry-default-signed.hap
  [ -f "$UNSIGNED" ] || { echo "未找到未签名 HAP: $UNSIGNED" >&2; exit 1; }
  java -jar "$HAP_SIGN_TOOL" sign-app \
    -mode localSign \
    -keyAlias "$KEY_ALIAS_INJ" \
    -keyPwd "$KEY_PWD_INJ" \
    -appCertFile "$SCRIPT_DIR/.build/keystore/debug.cer" \
    -profileFile "$SCRIPT_DIR/.build/keystore/debug.p7b" \
    -profileSigned 1 \
    -inFile "$UNSIGNED" \
    -signAlg "${SIGN_ALG:-SHA256withECDSA}" \
    -keystoreFile "$SCRIPT_DIR/.build/keystore/debug.p12" \
    -keystorePwd "$STORE_PWD" \
    -outFile "$SIGNED_OUT" \
    -compatibleVersion 8 \
    -signCode 1
  echo "CI 签名完成：$SIGNED_OUT"
fi
cd "$SCRIPT_DIR"

echo ""
echo "=== Step 3/3: Copy artifacts & build wheel ==="
mkdir -p binrunner/data
if [ -f app/entry/build/default/outputs/default/entry-default-signed.hap ]; then
  cp app/entry/build/default/outputs/default/entry-default-signed.hap binrunner/data/binrunner.hap
else
  echo "警告：HAP 未签名（无可安装产物），wheel 内是 entry-default-unsigned.hap" >&2
  cp app/entry/build/default/outputs/default/entry-default-unsigned.hap binrunner/data/binrunner.hap
fi
cp examples/hello/hello binrunner/data/hello
python3 -m pip install --quiet build 2>/dev/null
python3 -m build

echo ""
ls -lh dist/*.whl
echo "Done."

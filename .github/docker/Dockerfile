# HarmonyOS SDK Docker image (Command Line Tools + Native SDK)
# 构建前从 https://developer.huawei.com/consumer/cn/download/ 获取下载链接
# docker build --build-arg CMD_TOOLS_URL="<url>" -t ghcr.io/vincentzhu007/binrunner-sdk .
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG CMD_TOOLS_URL

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 下载并解压，自适应 zip 内部结构（可能是 command-line-tools/ + sdk/，也可能是其他布局）
RUN test -n "$CMD_TOOLS_URL" || (echo "ERROR: --build-arg CMD_TOOLS_URL=<url> is required" && exit 1) \
    && curl -L -o /tmp/tools.zip "$CMD_TOOLS_URL" \
    && unzip -q /tmp/tools.zip -d /tmp/tools \
    && echo "=== zip contents ===" && find /tmp/tools -maxdepth 3 -type d | sort \
    && echo "=== copying command-line-tools ===" \
    && CMD_DIR=$(find /tmp/tools -maxdepth 2 -name 'bin' -type d | grep -i 'command\|cmd' | head -1 | xargs dirname) \
    && if [ -n "$CMD_DIR" ]; then cp -r "$CMD_DIR" /opt/command-line-tools; \
       else cp -r /tmp/tools/*/* /opt/command-line-tools/ 2>/dev/null || cp -r /tmp/tools/* /opt/command-line-tools/; fi \
    && echo "=== copying sdk ===" \
    && SDK_DIR=$(find /tmp/tools -maxdepth 2 -name 'sdk' -type d | head -1) \
    && if [ -n "$SDK_DIR" ]; then cp -r "$SDK_DIR" /opt/harmonyos-sdk; \
       else echo "WARNING: sdk dir not found, checking alt paths..." && find /tmp/tools -name 'aarch64*clang*' -type f 2>/dev/null | head -3; fi \
    && rm -rf /tmp/tools /tmp/tools.zip

ENV DEVECO_SDK_HOME=/opt/harmonyos-sdk
ENV OHOS_NDK=/opt/harmonyos-sdk/default/openharmony/native
ENV PATH="/opt/command-line-tools/bin:/opt/command-line-tools/hvigor/bin:${DEVECO_SDK_HOME}/default/openharmony/toolchains:${PATH}"

RUN echo "=== ohpm ===" && ohpm --version \
    && echo "=== hvigorw ===" && hvigorw --version \
    && echo "=== clang ===" && (which aarch64-unknown-linux-ohos-clang || find /opt -name 'aarch64-unknown-linux-ohos-clang' -type f) \
    && echo "=== hdc ===" && which hdc

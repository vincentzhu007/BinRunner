# HarmonyOS SDK Docker image (Command Line Tools + Native SDK)
# 构建前需从 https://developer.huawei.com/consumer/cn/download/ 获取两个下载链接
# docker build \
#   --build-arg CMD_TOOLS_URL="<commandline-tools-linux-x64-*.zip>" \
#   --build-arg NATIVE_SDK_URL="<native-linux-x64-*.zip>" \
#   -t ghcr.io/vincentzhu007/binrunner-sdk .
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG CMD_TOOLS_URL
ARG NATIVE_SDK_URL

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 1. Command Line Tools（ohpm, hvigor, hdc）
RUN test -n "$CMD_TOOLS_URL" || (echo "ERROR: --build-arg CMD_TOOLS_URL=<url> is required" && exit 1) \
    && curl -L -o /tmp/tools.zip "$CMD_TOOLS_URL" \
    && unzip -q /tmp/tools.zip -d /tmp/tools \
    && mkdir -p /opt/command-line-tools \
    && if [ -d /tmp/tools/command-line-tools ]; then \
         cp -r /tmp/tools/command-line-tools/* /opt/command-line-tools/; \
       else \
         cp -r /tmp/tools/*/* /opt/command-line-tools/ 2>/dev/null || cp -r /tmp/tools/* /opt/command-line-tools/; \
       fi \
    && rm -rf /tmp/tools /tmp/tools.zip

# 2. Native SDK（aarch64 clang, sysroot, hdc toolchains）
RUN test -n "$NATIVE_SDK_URL" || (echo "ERROR: --build-arg NATIVE_SDK_URL=<url> is required" && exit 1) \
    && curl -L -o /tmp/native.zip "$NATIVE_SDK_URL" \
    && unzip -q /tmp/native.zip -d /tmp/native \
    && mkdir -p /opt/harmonyos-sdk \
    && if [ -d /tmp/native/sdk ]; then \
         cp -r /tmp/native/sdk/* /opt/harmonyos-sdk/; \
       elif [ -d /tmp/native/*/sdk ]; then \
         cp -r /tmp/native/*/sdk/* /opt/harmonyos-sdk/; \
       else \
         cp -r /tmp/native/*/* /opt/harmonyos-sdk/ 2>/dev/null || cp -r /tmp/native/* /opt/harmonyos-sdk/; \
       fi \
    && rm -rf /tmp/native /tmp/native.zip

ENV DEVECO_SDK_HOME=/opt/harmonyos-sdk
ENV OHOS_NDK=/opt/harmonyos-sdk/default/openharmony/native
ENV PATH="/opt/command-line-tools/bin:/opt/command-line-tools/hvigor/bin:${DEVECO_SDK_HOME}/default/openharmony/toolchains:${PATH}"

RUN echo "=== ohpm ===" && ohpm --version \
    && echo "=== hvigorw ===" && hvigorw --version \
    && echo "=== clang ===" && which aarch64-unknown-linux-ohos-clang \
    && echo "=== hdc ===" && which hdc

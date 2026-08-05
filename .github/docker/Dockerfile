# HarmonyOS SDK Docker image (Command Line Tools + Native SDK)
# 构建前从 https://developer.huawei.com/consumer/cn/download/ 获取下载链接
# docker build --build-arg CMD_TOOLS_URL="<url>" -t ghcr.io/vincentzhu007/binrunner-sdk .
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG CMD_TOOLS_URL

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 下载并安装（zip 内包含 command-line-tools/ + sdk/ 两个顶层目录）
RUN test -n "$CMD_TOOLS_URL" || (echo "ERROR: --build-arg CMD_TOOLS_URL=<url> is required" && exit 1) \
    && curl -L -o /tmp/tools.zip "$CMD_TOOLS_URL" \
    && unzip -q /tmp/tools.zip -d /tmp/tools \
    && ls /tmp/tools/ \
    && cp -r /tmp/tools/command-line-tools /opt/command-line-tools \
    && cp -r /tmp/tools/sdk /opt/harmonyos-sdk \
    && rm -rf /tmp/tools /tmp/tools.zip

ENV DEVECO_SDK_HOME=/opt/harmonyos-sdk
ENV OHOS_NDK=/opt/harmonyos-sdk/default/openharmony/native
ENV PATH="/opt/command-line-tools/bin:/opt/command-line-tools/hvigor/bin:${DEVECO_SDK_HOME}/default/openharmony/toolchains:${PATH}"

RUN echo "=== ohpm ===" && ohpm --version \
    && echo "=== hvigorw ===" && hvigorw --version \
    && echo "=== clang ===" && which aarch64-unknown-linux-ohos-clang \
    && echo "=== hdc ===" && which hdc

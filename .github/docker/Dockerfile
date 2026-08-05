# HarmonyOS Command Line Tools SDK image
# 构建前需从 https://developer.huawei.com/consumer/cn/download/ 获取下载链接
# docker build --build-arg CMD_TOOLS_URL="<url>" -t ghcr.io/vincentzhu007/harmonyos-sdk .
ARG BASE_IMAGE=ubuntu:22.04
FROM ${BASE_IMAGE}

ARG CMD_TOOLS_URL

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 下载并安装 Command Line Tools（含 ohpm/hvigor/hdc/native toolchain）
RUN test -n "$CMD_TOOLS_URL" || (echo "ERROR: --build-arg CMD_TOOLS_URL=<url> is required" && exit 1) \
    && curl -L -o /tmp/tools.zip "$CMD_TOOLS_URL" \
    && unzip -q /tmp/tools.zip -d /tmp/tools \
    && ls /tmp/tools/ \
    && mkdir -p /opt/command-line-tools \
    && if [ -d /tmp/tools/command-line-tools ]; then \
         cp -r /tmp/tools/command-line-tools/* /opt/command-line-tools/; \
       else \
         cp -r /tmp/tools/*/* /opt/command-line-tools/ 2>/dev/null || cp -r /tmp/tools/* /opt/command-line-tools/; \
       fi \
    && rm -rf /tmp/tools /tmp/tools.zip

# SDK 根目录（native toolchain 在 command-line-tools 包内或需额外安装）
ENV DEVECO_SDK_HOME=/opt/harmonyos-sdk
ENV OHOS_NDK=/opt/harmonyos-sdk/default/openharmony/native

# 工具路径
ENV PATH="/opt/command-line-tools/bin:/opt/command-line-tools/hvigor/bin:${PATH}"

# 验证关键工具
RUN echo "=== ohpm ===" && which ohpm || echo "MISSING" \
    && echo "=== hvigorw ===" && which hvigorw || echo "MISSING" \
    && echo "=== hdc ===" && which hdc || echo "MISSING" \
    && echo "=== clang ===" && ( \
         find /opt/command-line-tools -name 'aarch64-unknown-linux-ohos-clang' -type f 2>/dev/null \
         || echo "(will be configured at build time)" \
       )

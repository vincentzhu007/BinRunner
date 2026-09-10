# 换新手机接入指南

## 概述

每台鸿蒙设备的 UDID 都不一样，但 BinRunner 里 **UDID 出现在两个完全不同的层面**，
只有搞清楚是哪一层卡住，才能判断"换一台手机能不能直接用"：

| 层面 | UDID 用在哪 | 谁在管 | 换手机要做什么 |
|---|---|---|---|
| ① 设备发现/选择 | `hdc` 跟哪台设备通信 | CLI（运行时动态） | **无需任何操作** |
| ② 签名 profile 白名单 | 这台设备是否**允许安装** HAP | 华为签名体系（构建时） | **必须重新登记 + 重建** |

## ① 设备发现：天然支持任意手机

CLI 代码里**零硬编码 UDID**，每次执行都实时探测在线设备：

- `br devices` → 底层 `hdc list targets`，动态枚举在线设备 UDID
- 设备选择优先级（见 `binrunner/hdc.py` 的 `pick_device`）：
  1. `-t UDID` 命令行参数
  2. `BINRUNNER_DEVICE` 环境变量
  3. 只有一台在线时自动选用；多台在线则报错并列出全部 UDID 让你选

所以"跟哪台设备通信"这层，插上任何一台开好 USB 调试的手机都能工作，与 UDID 具体值无关。

## ② 签名白名单：换手机的真正卡点

内存 ELF loader 依赖 **jit prctl**（`prctl(0x6a6974)`），该开关**只对 debug 签名应用开放**，
因此 HAP 必须 debug 签名（release 签名装得上也跑不了二进制）。

HarmonyOS NEXT 的 debug 签名 profile（`.p7b` 文件）里**内嵌了允许安装的设备 UDID 白名单**：
只有登记过的手机才能安装该 HAP。当前 CI 调试 Profile 白名单的设备 UDID 登记在
[`docs/supported-device-udids.json`](supported-device-udids.json)（与签名用 `.p7b` 保持一致）。

- 本机签名材料：DevEco Studio 自动签名会在本机改写 `app/build-profile.json5` 的
  `signingConfigs`（指向 `~/.ohos/config/default_*.p7b`，文件名带随机后缀）。该配置含
  本机路径与账号口令，**仓库里只保留空数组**；本机副本存到
  `.build/build-profile.local.json5`（已 gitignore），`./build.sh` 构建时自动注入并还原
- `pip install binrunner` 内置的 `binrunner/data/binrunner.hap` 也是某次构建的 debug 签名产物，
  其白名单只含构建时登记过的设备

**结论**：换一台**从没登记过**的新手机，`br setup` 会卡在 `bm install` 上——不是代码 bug，
是 profile 里没有这台设备的 UDID。而华为没有开放"CLI 直接登记 UDID / 改 profile"的官方通道，
自动签名是 DevEco Studio 的能力，`br` 无法替代这一步。

## 换新手机的标准流程

1. **准备设备**：新手机开启「开发者模式」和「USB 调试」，用数据线连上 PC。
2. **确认被发现**：`br devices` 能看到新设备的 UDID（等价于 `hdc list targets`）。
3. **重新自动签名**：DevEco Studio 打开工程 →
   File → Project Structure → Signing Configs → 自动签名。
   DevEco 会把当前连接的设备 UDID 自动登记进 profile 并重新生成 `.p7b`。
   > 如果自动签名没刷新，确认设备在「设备管理器」里已连接且授权过 USB 调试。
4. **重新构建 HAP**：DevEco 里 Run / Build，或命令行
   `hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon`。
5. **安装验证**：`br setup --reinstall`（首次则 `br setup`），
   它会安装 HAP、推送 `hello` 并执行，验证 安装→推送→执行 全链路。
6. **同步白名单登记**：把新 `.p7b` 的 device-ids 同步到
   [`docs/supported-device-udids.json`](supported-device-udids.json)，并更新 CI Secrets
   里的 `BINRUNNER_PROFILE_B64`（见 `.github/workflows/HAP_SIGN_SECRETS.md`「换新测试设备怎么办」）。

## 常见故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `br devices` 看不到设备 | USB 调试未开 / 未授权 / 驱动问题 | 检查开发者选项、确认 `hdc list targets`、重新插拔授权 |
| `br setup` 报 `bm install` 失败 | 设备 UDID 不在签名 profile 白名单 | 回到上面第 3 步重新自动签名并重建 |
| 多台设备在线，`br run` 报"请用 -t 指定" | 自动检测只认单设备 | `br -t <UDID> run ...` 或设 `BINRUNNER_DEVICE` |
| 装上了但二进制跑不起来 | 用了 release 签名（jit prctl 被禁） | 确认 `buildMode=debug` + debug 签名 |

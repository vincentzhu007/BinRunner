# 鸿蒙手机远程共享方案（dog 服务器 zhugd 用户访问本地手机）

日期：2026-07-23
状态：已部署并验收

## 目标

本地 Mac 通过 USB 连接鸿蒙手机（华为 LMR-AL00，序列号 4VF0225717009856），
让远程 dog 服务器（Ubuntu x64，zhugd 用户）上的 `hdc` 命令可以直接操作这台手机。

## 最终架构

```
手机 hdcd :5555 ◀──USB（hdc fport）── Mac 127.0.0.1:15555
                                          ▲
                                   ssh -R 反向隧道（经 Huawei-VPN/ppp0）
                                          ▼
dog  127.0.0.1:15555 ◀──hdc tconn── zhugd 自己的 hdc server
```

- 手机侧：`hdc tmode port 5555`（hdcd 监听 TCP 5555，跨重启持久；还原用 `hdc tmode usb`）
- Mac 侧：`hdc fport tcp:15555 tcp:5555`，通过 USB 通道把手机 5555 映射到 Mac 回环
  （手机端看到的是本机回环连接，绕开 WiFi 调试的来源/配对限制）
- 隧道：Mac 主动 `ssh -N -R 15555:127.0.0.1:15555 zhugd@dog`（dog 只监听回环，不开入站端口）
- dog 侧：zhugd 的 hdc server 通过 `hdc tconn 127.0.0.1:15555` 纳管手机，之后裸用 hdc 全命令

## 为什么不是"远程 client 直连 Mac 的 hdc server"

实测（hdc 3.2.0c）：client↔server 协议虽可走隧道连通、版本校验通过，
但 server 不向远程 client 暴露 USB 设备（list targets 恒为空，本地 client 经 TCP 弹跳则正常）。
因此改用 awesome-hdc 文档的"方式二"：让 dog 跑自己的 server，以标准 tconn 通道纳管手机。

## 组件清单

### Mac（手机所连机器）

| 组件 | 位置 | 说明 |
|---|---|---|
| watchdog 脚本 | `/Users/zgd/bin/hdc-phone-share.sh` | 每 30s 巡检：隧道进程、手机在线、fport 存在，缺失自动恢复 |
| launchd 任务 | `~/Library/LaunchAgents/com.zgd.hdc-phone-share.plist` | RunAtLoad + KeepAlive |
| 日志 | `/tmp/hdc-phone-share.log` | 隧道/fport 恢复记录 |

### dog 服务器

| 组件 | 位置 | 说明 |
|---|---|---|
| hdc 3.2.0c | `~/tool/command-line-tools/.../toolchains/hdc`（另 `~/tool/hmos-ndk/` 同版本已在 PATH） | zhugd 交互使用 |
| tconn 保活 | `~/bin/hdc-tconn-keepalive.sh` + crontab 每分钟 | 端口通但会话缺失时自动重新 tconn |
| 日志 | `~/bin/hdc-tconn-keepalive.log` | 重连记录 |

注意：hdc 命令失败时退出码也是 0，判活必须 grep `list targets` 输出，不能靠 `$?`。

## 故障自愈时序

任意环节中断（VPN 抖动、Mac 重启、手机重插、dog server 重启）：
最坏约 90 秒恢复（Mac watchdog 30s 建隧道和 fport → dog cron 60s 重建 tconn 会话）。

## 运维手册

- **查看链路状态**：Mac 上 `tail /tmp/hdc-phone-share.log`；dog 上 `hdc list targets`
- **手机彻底还原 USB 模式**：`hdc tmode usb`（会重启手机）
- **dog 上 8710/15555 被占**：隧道带 `ExitOnForwardFailure`，端口被占时 ssh 反复退出，日志可见
- **Nextin 代理**：其透明代理会劫持全局流量（曾导致局域网全不通），但到 dog 的流量走
  ppp0 明细路由，实测与 Nextin 共存无冲突

## 验收记录（2026-07-23）

- dog `hdc shell` / `hdc file send`（512KB，md5 一致，1.4MB/s）通过
- 隧道杀死 → 全链路自动恢复通过
- dog 会话删除 → cron 自动重连通过
- Nextin 开启状态下全链路通过

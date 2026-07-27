---
name: remote-hdc
description: 通过 dog 服务器远程操作 USB 连接在本机 Mac 上的鸿蒙手机（hdc 共享链路）。Use when 需要在远程 dog 服务器上用 hdc 操作手机、排查远程 hdc 链路故障（list targets 为空、tconn 掉线、隧道中断）、或恢复/验收 hdc 共享通道。
---

# Remote HDC — dog 服务器远程操作本地手机

链路：`手机 hdcd :5555 →(USB hdc fport)→ Mac 127.0.0.1:15555 →(ssh -R 反向隧道)→ dog 127.0.0.1:15555 →(hdc tconn)→ dog 本地 hdc server`

完整设计与运维手册见 [REFERENCE.md](REFERENCE.md)。

## 快速判断链路是否健康

```bash
# 在 dog 上（必须 grep 输出，hdc 失败时退出码也是 0）
hdc list targets        # 应看到 127.0.0.1:15555 已连接
```

```bash
# 在 Mac 上
tail /tmp/hdc-phone-share.log   # watchdog（每 30s）的隧道/fport 恢复记录
```

## 常见故障与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| dog 上 `list targets` 为空 | tconn 会话丢失 | 等 dog 上 cron（每分钟 `~/bin/hdc-tconn-keepalive.sh`）自动重连，或手动 `hdc tconn 127.0.0.1:15555` |
| dog 上 15555 端口不通 | Mac 侧隧道断了 | 看 `/tmp/hdc-phone-share.log`；watchdog 30s 内自动重建，否则手动 `ssh -N -R 15555:127.0.0.1:15555 zhugd@dog` |
| 隧道反复退出 | dog 上 15555 被占（带 `ExitOnForwardFailure`） | 在 dog 上查占用进程并释放端口 |
| 全链路断了 | VPN 抖动/Mac 重启/手机重插 | 自愈约 90 秒（watchdog 30s + cron 60s），无需手动 |

## 关键事实（勿踩坑）

- `hdc` 命令失败时退出码也是 0 —— 判活必须 grep `list targets` 输出，不能靠 `$?`。
- 远程 client 直连 Mac 的 hdc server 看不到 USB 设备（hdc 3.2.0c 实测），所以 dog 必须跑自己的 server 用 tconn 纳管。
- 手机还原 USB 模式：`hdc tmode usb`（会重启手机）。

# `binrunner push` 规格说明

## 概述

`binrunner push` 通过 `hdc fport` TCP 转发通道，将 PC 端文件推送到鸿蒙设备 App 沙箱的 `filesDir/bin/` 目录。支持单文件和递归目录推送。

## 命令形式

```bash
# 单文件
binrunner push <FILE> [NAME]
# 文件推送到 filesDir/bin/<NAME>，省略 NAME 时用本地文件名

# 目录（递归）
binrunner push <DIR>/
# 遍历目录树，以相对路径逐文件推送，保持子目录结构
# 目录模式不支持指定 NAME
```

## 协议

TCP 连接到设备 `127.0.0.1:8888`（经 `hdc fport` 转发至 App 内 PushServer）。

单连接单文件，小端字节序：

```
┌──────────┬──────────────┬──────────────┬──────────────────┐
│ u32      │ nameLen 字节  │ u64          │ payloadSize 字节   │
│ nameLen  │ name (UTF-8) │ payloadSize  │ payload           │
└──────────┴──────────────┴──────────────┴──────────────────┘
```

| 字段 | 类型 | 说明 |
|---|---|---|
| nameLen | u32 LE | name 的字节数，≤ 256 |
| name | bytes | 远端相对路径（如 `benchmark`、`models/net.ms`） |
| payloadSize | u64 LE | 文件内容的字节数 |
| payload | bytes | 文件原始内容 |

客户端发完即关闭连接，服务端在 close 事件中解析并落盘。

## 递归目录推送流程

### CLI 侧 (`binrunner.py`)

```
push_tree(local_dir)
  ├── os.walk(local_dir)  遍历目录树
  ├── 对每个文件计算 relpath = os.path.relpath(full, local_dir)
  │   例: mylibs/models/net.ms → "models/net.ms"
  └── 逐文件调用 _send_file(port, relpath, payload)
       ├── 转发已建立则复用，未建立则 hdc fport（幂等）
       └── 连接失败（App 未运行）→ 自动 aa start 拉 App 后重试一次
```

### PushServer 侧 (`PushServer.ets`)

```
store(chunks)
  ├── 解析协议 → name, payload
  ├── 安全校验（见下）
  ├── name 含 '/' → mkdirSync(recvDir + parent, recursive=true)
  └── 写入文件 → recvDir/name
```

## 安全校验

CLI 侧和 PushServer 侧**双重校验**，规则一致：

| 规则 | 拒绝的 name | 原因 |
|---|---|---|
| 空名 | `""` | 无效 |
| 绝对路径 | `"/etc/passwd"` | 禁止脱离 recvDir |
| Windows 分隔符 | `"a\\b"` | 平台不一致 |
| 当前目录 | `"."` | 无效 |
| 上级引用 | `".."` | 目录穿越 |
| 上级穿越 | `"a/../b"`, `"a/.."` | 目录穿越（含 `/../` 或以 `/..` 结尾） |

**允许**的 name 格式：
- `"benchmark"` — 纯文件名
- `"models/net.ms"` — 带子路径
- `"a/b/c/data.bin"` — 多层嵌套

## 设备端文件布局

```
filesDir/bin/               ← recvDir，推送根目录
├── benchmark               ← 单文件推送
├── libdep.so               ← .so 依赖（必须放根层级）
├── config.json             ← 数据文件（放根层级或子目录均可）
└── models/                 ← 子目录（目录推送自动创建）
    ├── net.ms
    └── vocab.bin
```

## `LD_LIBRARY_PATH` 与子目录

动态链接器搜索路径：

```
LD_LIBRARY_PATH = filesDir/bin : <bundle libs 目录>
```

**仅搜索 `filesDir/bin/` 根目录**，不递归搜索子目录。

这意味着 `.so` 依赖必须直接推送到根层级：

```bash
binrunner push ./libfoo.so        # ✅ 动态链接可解析
binrunner push ./libs/libfoo.so   # ❌ 动态链接找不到
```

**例外**：通过绝对路径 `@/bin/libs/libfoo.so` 显式指定的 .so 不受此限制，但一般场景不涉及。

数据文件（模型、配置等）可自由使用子目录组织：

```bash
binrunner push ./my-models/
# → @/bin/my-models/net.ms
# → @/bin/my-models/vocab.bin
```

## name 长度限制

- 协议 `nameLen` 字段为 u32（理论上 4GB），但代码限制 ≤ 256 **字节**
- 限制的是 UTF-8 编码后的字节数，不是字符数
- 子路径增大了超限风险（如 `very-long-directory-name/very-long-filename.so`），但 256 字节对正常文件名绰绰有余

## 幂等性

- `hdc fport` 重复建立无害（`ensure_forward` 检测 `127.0.0.1:8888` 已通则跳过）
- PushServer 收到同名文件直接覆盖（`OpenMode.CREATE | OpenMode.TRUNC`）
- App 未运行时自动拉 App 后重试一次

## 与打包 libs 的关系

| 维度 | 推送 bin/ | 打包 libs/ |
|---|---|---|
| 更新方式 | 免打包，hdc 推送 | 重新构建安装 HAP |
| 文件类型 | 普通文件，随机读正常 | bundle 内虚拟文件，随机读坏数据 |
| 名字格式 | 原样文件名 | 必须 `lib<name>.so` |
| loader 处理 | 先过 memfd（统一路径） | 先顺序读进 memfd 再加载 |
| 解析优先级 | **优先于** libs | 低于推送目录 |

## 实现文件

| 文件 | 职责 |
|---|---|
| [tools/binrunner.py](../tools/binrunner.py) | CLI：`push_file`、`push_tree`、`_send_file` |
| [entry/src/main/ets/common/PushServer.ets](../entry/src/main/ets/common/PushServer.ets) | 设备端 TCP server，协议解析与落盘 |
| [entry/src/main/ets/entryability/EntryAbility.ets](../entry/src/main/ets/entryability/EntryAbility.ets) | App 启动时初始化 `PushServer.recvDir` 并 start |

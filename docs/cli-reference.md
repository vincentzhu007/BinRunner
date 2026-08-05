# `br` CLI 参数参考

## 全局选项

适用于所有子命令，放在子命令之前。

| 选项 | 参数 | 默认值 | 说明 |
|---|---|---|---|
| `-t` | `UDID` | 自动检测 | 目标设备 UDID。多台设备时必须指定；仅一台时自动选用。也可用环境变量 `BINRUNNER_DEVICE` |
| `-p` | `PORT` | `8888` | PushServer TCP 转发端口。仅 `push` 子命令使用 |

hdc 不在 PATH 时自动尝试 DevEco Studio 默认路径：
`/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc`

## 子命令

### `br devices`

列出已连接设备。

```bash
br devices
# 输出:
# 4VF0225717009856

br -t 4VF0225717009856 devices  # 指定设备（效果同上）
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | — |

---

### `br forward`

建立 `hdc fport` TCP 转发，将 PC 端口映射到设备端口。

```bash
br forward
# 输出:
# OK: 127.0.0.1:8888 -> device:8888 (4VF0225717009856)

br -p 9999 forward              # 指定非默认端口
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | — |

**幂等**：已建立转发时直接返回，不重复创建。

---

### `br push`

推送文件或目录到设备 `filesDir/bin/`。自动建立转发（如需）、自动拉 App（如未运行）。

#### 推送文件

```bash
br push ./benchmark                             # 推送到 filesDir/bin/benchmark
br push ./libfoo.so libbar.so                   # 推送到 filesDir/bin/libbar.so
br push ./config.json                           # 推送到 filesDir/bin/config.json
```

#### 推送目录

```bash
br push ./mylibs/                               # 递归推送，保持子目录结构
# mylibs/benchmark      → filesDir/bin/benchmark
# mylibs/models/net.ms  → filesDir/bin/models/net.ms
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `local` | ✅ | 本地文件或目录路径 |
| `remote` | 文件时可选 | 远端名（目录模式不支持）。默认取本地文件名 |

**限制**：`remote` 名 ≤ 256 字节；拒绝 `..`、绝对路径、`\` 分隔符。

**协议**：TCP 连接到 `127.0.0.1:<port>`，单连接单文件，小端字节序。
详见 [docs/push-spec.md](push-spec.md)。

---

### `br run`

在设备上执行二进制，捕获 stdout/stderr 并打印到终端，退出码透传。

```bash
br run "hello"                                  # 执行 hello，无参数
br run "hello foo bar"                          # 执行 hello，传入 ['foo', 'bar']
br run "benchmark --modelFile=@/mobilenetv2.ms --loopCount=5"
br run "@/bin/custom-binary --flag"             # 绝对路径执行

# 特殊：列出设备目录
br ls                                           # files 根目录
br ls "@/bin"                                   # 推送文件目录
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `cmdline` | ✅ | 命令行字符串。第一个词是二进制名或绝对路径，其余为参数 |
| `--timeout` | 否（默认 60s） | 等待输出的秒数，超时后强制返回 |

**名字解析顺序**：绝对路径直通 → `filesDir/bin/<name>`（推送目录） → `libs/arm64/lib<name>.so`（打包目录）

**`@` 展开**：词首 `@/` 或等号后 `=@/` 自动展开为 `filesDir` 路径：
- `@/mobilenetv2.ms` → `/data/storage/el2/base/haps/entry/files/mobilenetv2.ms`
- `--modelFile=@/m.ms` → `--modelFile=/data/storage/.../m.ms`
- 参数中间的 `@`（如 `user@host`、`@fd`）不展开

**并发隔离**：每次执行自动生成 8 位随机 run_id，hilog 输出带 `[run_id]` 前缀，多终端互不干扰。详见 [docs/concurrency-spec.md](concurrency-spec.md)。

---

### `br ls`

列出设备目录内容（大小、修改时间、名称）。

```bash
br ls                                           # files 根目录
br ls "@/bin"                                   # 推送文件目录
br ls "@/bin/models"                            # 子目录
```

| 参数 | 必需 | 说明 |
|---|---|---|
| `path` | 否（默认 files 根目录） | 设备侧路径，支持 `@` 展开 |

**注意**：这是 ArkTS 内置命令，不走 native 二进制执行路径，不属于 NAPI fork 流程。

---

### `br logs`

持续跟踪设备 BinRunner hilog 输出，Ctrl+C 退出。

```bash
br logs                                         # 实时跟踪，显示所有 BinRunner 日志
```

| 参数 | 必需 | 说明 |
|---|---|---|
| （无） | — | — |

**实现**：`hilog -x` 轮询 + seen 集合去重，1s 间隔。不传 run_id 过滤，显示所有会话的输出。

---

## 退出码

| 场景 | 退出码 |
|---|---|
| 目标二进制正常退出 | 目标退出码（如 `hello` 返回 42） |
| 目标二进制被信号杀死 | `128 + sig` |
| 执行超时（SIGKILL） | 1（CLI 自身返回） |
| 命令未找到（ResolveExecPath 失败） | 255 |
| 设备未连接 / 参数错误 | 1-2 |

## 环境变量

| 变量 | 说明 |
|---|---|
| `BINRUNNER_DEVICE` | 目标设备 UDID，优先级低于 `-t`，高于自动检测 |
| `DEVECO_SDK_HOME` | DevEco SDK 根目录（如 `/Applications/DevEco-Studio.app/Contents/sdk`） |

## 自动行为

| 场景 | 行为 |
|---|---|
| hdc 不在 PATH | 自动查找 DevEco Studio 默认路径 |
| 仅一台设备 | 自动选用，无需 `-t` |
| `push` 时转发未建立 | 自动 `hdc fport`（幂等） |
| `push` 时 App 未运行 | 自动 `aa start` 拉 App 后重试 |
| `run` 时 App 未运行 | App 自动冷启动（通过 `aa start`） |
| 旧版 App 无 run_id | 兼容 — 不过滤，处理所有 BinRunner 行 |

## 实现文件

| 文件 | 职责 |
|---|---|
| [tools/binrunner.py](../tools/binrunner.py) | CLI 全逻辑（142 行 Python，零第三方依赖） |

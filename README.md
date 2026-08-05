# 🚀 BinRunner — 鸿蒙OS二进制执行器

在 鸿蒙NEXT 零售机上，App 沙箱内 **一切 `execve` 都被 SELinux 禁止**（libs 目录、files 目录、
memfd + `/proc/self/fd` 全部实测 EACCES）。本工程采用与 Termony 同源的**内存 ELF loader**
方案：fork 子进程后在进程内把目标 ELF 读入匿名内存、用 HarmonyOS 特有的 jit prctl
放开匿名页可执行权限（debug 签名应用可用）、直接跳转入口 —— 完全不经过 execve。

已在零售版真机（非 root）验证：参数传递、stdout/stderr 捕获、退出码透传全部正常。

## 工作原理

```
PC: hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd "hello foo bar"
        │
        ▼
EntryAbility (onCreate/onNewWant 解析 want.parameters.cmd)
        │
        ▼
napi_init.cpp: runBin → fork 子进程
        │  execv(libhello.so)                    ← 零售机必然 EACCES
        │  ↓ 失败回退
        │  memfd_create + 顺序复制 ELF            ← 关键：bundle libs 目录的文件
        │  │                                        随机读(lseek+read)会拿到坏数据，
        │  │                                        必须先顺序读进 memfd（实测坑）
        │  ↓
        │  z_entry(伪造初始栈)                     ← third_party/elf（MikhailProg/elf，
        │  │   mmap 匿名页 ← 读 ELF 段               已打 jit prctl 补丁 + "@fd" 直传补丁）
        │  │   prctl(0x6a6974) + mprotect PROT_EXEC ← debug 应用专属 jit 开关
        │  │   修正 auxv (AT_PHDR/AT_ENTRY/...)
        │  ↓
        │  z_trampo 跳转到目标入口                  ← 目标二进制开始运行
        ▼
父进程 poll 捕获 stdout/stderr，超时 SIGKILL
        │
        ▼
hilog (tag=BinRunner, 900 字符分段防截断)  →  PC: hdc shell hilog | grep BinRunner
```

## 使用步骤

### 0. 前置依赖

**方案 A — DevEco Studio（推荐，含签名 + 全套工具链）**：

从 [HarmonyOS 开发者官网](https://developer.huawei.com/consumer/cn/deveco-studio/) 下载安装。
默认路径 `/Applications/DevEco-Studio.app`，自带 hdc / hvigor / ohpm / OHOS NDK。

**方案 B — 仅 Command Line Tools（不需要 IDE，仅 CLI 操作）**：

从 [HarmonyOS SDK 下载页](https://developer.huawei.com/consumer/cn/download/) 获取
Command Line Tools 压缩包，解压后配置环境变量：

```bash
# 以 macOS 为例，Linux/Windows 路径略有不同
export DEVECO_SDK_HOME="$HOME/harmonyos/sdk"
export PATH="$DEVECO_SDK_HOME/default/openharmony/toolchains:$DEVECO_SDK_HOME/default/openharmony/toolchains/hdc:$PATH"
```

验证：

```bash
hdc version     # 设备连接工具
which ohpm      # 包管理器
which hvigorw   # 构建工具
```

> **注意**：仅 CLI 使用时只需要 hdc（`br push`/`run`/`ls`/`logs` 均通过 hdc 通信）。
> 如需构建安装 HAP，还需要 ohpm + hvigorw + OHOS NDK（或直接用 DevEco Studio）。

### 1. 签名

DevEco Studio 打开工程，File → Project Structure → Signing Configs → 自动签名。
**必须是 debug 签名**（jit prctl 只对 debug 应用开放）。

> 仅 CLI 使用（不修改 App）时无需签名 —— 直接安装预编译的 release HAP 即可。

### 2. 构建安装

```bash
# DevEco Studio 用户
export PATH="/Applications/DevEco-Studio.app/Contents/tools/node/bin:/Applications/DevEco-Studio.app/Contents/tools/ohpm/bin:/Applications/DevEco-Studio.app/Contents/tools/hvigor/bin:/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
export DEVECO_SDK_HOME="/Applications/DevEco-Studio.app/Contents/sdk"

# Command Line Tools 用户（路径按实际安装位置调整）
# export DEVECO_SDK_HOME="$HOME/harmonyos/sdk"
# export PATH="$DEVECO_SDK_HOME/default/openharmony/toolchains:$DEVECO_SDK_HOME/default/openharmony/toolchains/hdc:$PATH"

ohpm install --all
hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon
hdc install entry/build/default/outputs/default/entry-default-signed.hap
```

### 3. hdc 触发执行

```bash
hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd "hello foo bar"
hdc shell hilog | grep BinRunner
```

注意：本机 aa 用 `--ps` 传字符串参数（不是 `--es`，不同系统版本参数名不同，报错时看 `aa start -h`）。
规则：`cmd` 第一个词是二进制名或绝对路径，其余为参数。
路径约定：**`@` 展开为 App 沙箱 files 根目录**（`/data/storage/el2/base/haps/entry/files`，
仿 shell tilde 语义但用 `@`，避免 host shell 抢先展开：词首或 `--opt=@/x` 等号后生效，
`@foo` 和参数中间的 `@` 不展开）；`run`/`ls` 等所有命令统一生效。
二进制名解析顺序：绝对路径直通 → 推送目录 `@/bin/<名>` → libs 目录 `lib<名>.so`。

### 4. 免打包推送执行（host CLI，推荐）

`/data/local/tmp` 对 App 不可见（SELinux 隐藏为 ENOENT）、`hdc file send` 进不了
App 沙箱（shell uid 无权限）——两条直推路径在零售机上均**实测不可行**。可用通道是
App 内置的 TCP 推送 server（[PushServer.ets](entry/src/main/ets/common/PushServer.ets)，
App 启动即监听 :8888），配合 `hdc fport` 把文件写入 `filesDir/bin/`。

[tools/binrunner.py](tools/binrunner.py) 把转发管理、推送、触发执行、日志收集封装成
一条命令（零依赖，hdc 不在 PATH 时自动找 DevEco 默认路径）：

```bash
alias br="python3 tools/binrunner.py"          # 简短别名，推荐

br devices                                      # 列出设备（多台时 -t UDID 指定）
br push ./benchmark                             # 推送二进制（自动建立 fport，幂等）
br push ./libmindspore-lite.so                  # 动态依赖库推进同一目录
br push ./mylibs/                               # 递归推送目录（保持子目录结构）
br run "benchmark --modelFile=@/mobilenetv2.ms --loopCount=5"
# → stdout/stderr 直接打印到本地终端，二进制退出码透传为 CLI 退出码
br ls                                           # 列出 files 根目录（bin/ 子目录是推送区；加路径可列任意目录）
br logs                                         # 持续跟踪设备日志
```

持久化（追加到 `~/.zshrc` 或 `~/.bashrc`）：

```bash
echo 'alias br="python3 '"$(pwd)"'/tools/binrunner.py"' >> ~/.zshrc
```

- `@` 统一展开为沙箱 files 根目录（`run`/`ls` 均生效）；命令名给绝对路径也可直接执行，
  如 `br run "@/bin/hello a b"`
- 名字解析顺序：绝对路径直通 → **`@/bin/<name>`（推送目录优先）** → libs 目录 `lib<name>.so`
- 推送支持子目录（PushServer 自动创建父目录），目录推送示例：
  ```bash
  br push ./mylibs/                  # 保持子目录结构，如 mylibs/sub/dep.so → bin/sub/dep.so
  ```
  用 `br ls "@/bin"` 查看已推送的文件树。
- **注意**：`LD_LIBRARY_PATH` 仅含 `filesDir/bin/` 根目录（不递归搜索子目录），.so 依赖
  请直接放在根层级；子目录适合放数据文件（模型、配置等）
- 推送目录的文件是普通文件，没有 bundle libs 目录的随机读坏数据问题；loader 同样先过 memfd
- 数据文件也可通过子目录组织：`@/bin/models/xxx.ms`、`@/bin/configs/yy.json`
- 不想用 CLI 时等价的手工步骤：`hdc fport tcp:8888 tcp:8888` + `python3 tools/binrunner.py push <file或目录>` +
  `aa start --ps cmd ...` + `hilog | grep BinRunner`

已实测：推送静态 hello 执行 exit=42 正常；推送 benchmark + libmindspore-lite.so 免打包
完成 MobileNetV2 推理（AvgRunTime ≈35ms，与打包版一致）；CLI 推送/执行/日志重组/退出码
透传全部验证通过。

### 5. 多终端并发执行

BinRunner 支持多个终端同时 `br run`，各次执行互不干扰：

```
终端1: br run "hello"           ──→ 子进程 456
终端2: br run "benchmark ..."   ──→ 子进程 789
终端3: br push ./data/          ──→ PushServer TCP 连接

每个 br run 生成唯一 8 位 run_id（如 a1b2c3d4），所有 hilog 输出
带 [a1b2c3d4] 前缀，CLI 只认自己的 ID，输出互不混杂。
PushServer（TCP :8888）天然支持多连接并发。
```

- **进程隔离**：每条 `br run` 独立 fork 子进程，各自运行目标二进制，互不影响
- **输出隔离**：自动生成的 run_id 标记所有日志行，CLI 自动过滤
- **Push 并发**：多个 `br push` 可同时进行，同名文件后写覆盖
- 详见 [docs/concurrency-spec.md](docs/concurrency-spec.md)

### 6. 接入自己的二进制（打包方式，静态 / 动态均可）

```bash
# 静态（最简单）：OHOS NDK 交叉编译时加 -static，参考 tools/build_hello.sh
# 动态（已实测 mindspore benchmark）：二进制和它的 .so 依赖一起放进 libs 目录
```

- 目标二进制重命名为 `lib<名字>.so` 放入 `entry/libs/arm64-v8a/`
- **动态二进制的依赖库**（如 `libmindspore-lite.so`）放入同一目录；子进程已自动设置
  `LD_LIBRARY_PATH` 指向该目录，musl ld.so 会沿它解析 NEEDED 依赖
- 动态链接器 `/lib/ld-musl-aarch64.so.1` 由 loader 自动加载（App 可读系统 ld-musl，已实测）
- 重新打包安装即可

### 7. 数据文件（模型等）通路

`/data/local/tmp` App 读不到（SELinux），数据文件三条路：

1. **打进 HAP rawfile**（本工程示范）：放 `entry/src/main/resources/rawfile/`，
   App 启动时自动释放到 filesDir，cmd 里用 `@/xxx` 引用
2. **第 4 节的推送通道**（适合大文件/频繁更换）：`br push model.ms`，
   用 `@/bin/model.ms` 引用
3. 自建 socket 交互：App 起 server，`hdc fport` 后 PC 直连（见扩展方向）

### 8. 实测：MindSpore Lite 模型推理（已跑通）

```bash
br run "benchmark --modelFile=@/mobilenetv2.ms --loopCount=5 --warmUpLoopCount=1"
```

真机输出（零售版非 root 手机，CPU 2 线程）：

```
Model = mobilenetv2.ms, NumThreads = 2, MinRunTime = 24.259 ms,
MaxRuntime = 40.460 ms, AvgRunTime = 29.849 ms
Run Benchmark mobilenetv2.ms Success.
```

集成方式：`benchmark` 重命名为 `libbenchmark.so` + `libmindspore-lite.so` 直接放入
[entry/libs/arm64-v8a/](entry/libs/arm64-v8a/)，模型 `mobilenetv2.ms` 放 rawfile（本仓库均已内置）。

## 真机实测结论（都是踩坑换来的）

| 路径 | 结果 |
|---|---|
| `hdc shell` 执行 /data/local/tmp 二进制 | ❌ SELinux 拒绝（shell 域） |
| App execv libs 目录的 .so | ❌ EACCES（noexec） |
| App execv files 沙箱目录 | ❌ EACCES（noexec） |
| App execv memfd + /proc/self/fd | ❌ EACCES（SELinux 拒匿名 inode exec） |
| App 读 /data/local/tmp | ❌ SELinux（对 App 隐藏，opendir 报 ENOENT） |
| `hdc file send` 到 App 沙箱 files 目录 | ❌ permission denied（shell uid 无权限，dir 0700 属 App uid） |
| **hdc fport + PushServer 推送二进制到 filesDir/bin 执行** | ✅ **已实测**（静态 hello、动态 benchmark + libmindspore-lite.so 免打包跑通） |
| App 读 /lib/ld-musl-aarch64.so.1 | ✅ 可读（动态链接前提） |
| App 对任意文件 mmap PROT_EXEC | ❌ EACCES（dlopen 能工作是系统加载器的特权通道） |
| hnp 包安装（module.json5 hnpPackages） | ❌ 手机零售版无效：App 命名空间里根本没有 /data/app、/data/service/hnp（该机制只在鸿蒙 PC 有效） |
| **内存 ELF loader + jit prctl（debug 签名）** | ✅ **唯一可行** |
| 静态链接二进制（musl -static） | ✅ 稳定 |
| **动态链接二进制 + ld.so 加载 .so 依赖** | ✅ **已实测**（mindspore benchmark + libmindspore-lite.so） |
| **模型推理（MobileNetV2）** | ✅ **已实测**（AvgRunTime 29.8ms） |
| bundle libs 目录文件随机读（lseek+read） | ⚠️ 拿到坏数据导致 SIGSEGV；顺序读正常 → 必须先复制进 memfd |
| App 内访问 /proc/self/fd/N | ❌ 不可访问 → loader 打补丁支持 "@fd" 直传已打开 fd |
| 伪造栈贴近映射段底部 | ⚠️ ld.so 压栈越界，无规律 SIGSEGV（.bss 布局随构建漂移）→ 栈块必须放大缓冲区高位 |
| auxv 缺 AT_BASE / AT_EXECFN | ⚠️ musl ld.so 误判为"被直接调用"走错路径 → 必须给占位项让 loader 改写 |

## 调试命令

```bash
# probe：枚举关键目录到 hilog，排查路径问题
hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd "probe"

# probe2：动态链接可行性检测（/data/local/tmp 可读性、ld-musl 可读性、各目录 exec-mmap）
hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd "probe2"
```

## 已知限制

- **NAPI 同步调用**：runBin 阻塞 UI 线程直到二进制退出或 30s 超时；长耗时用例应改为
  napi_create_async_work 异步任务（或加大超时参数，见 index.d.ts）
- **hilog 单条截断**：报告逐行输出（单条日志无内嵌换行；hilog 会把内嵌换行拆成多条带前缀的
  行，host 无法区分，故避免）；单行超 900 字符才按 [i/n] 分段；报告以 `<<< END` 标记结束
- **loader 追踪日志**：loader 会向 stderr 打印 `loader: ...` 追踪行（与目标输出混在一起），
  正式使用时删除 third_party/elf/src/loader.c 中的 z_fdprintf 调试行
- **CPU 推理正常；GPU/NPU delegate 不可用**（App 沙箱无权访问对应驱动/服务）
- 二进制以 App uid 运行，受 App 沙箱约束（访问不了其他应用数据等）
- seccomp 存在（Termony 实测 setuid/setgid 会被杀），避免在用例里调用特权 syscall

## 扩展方向

- **TCP 回传**：App 起 socket server，`hdc fport tcp:8889 tcp:8889`（8888 已被 PushServer 占用），适合大输出/交互式用例
- **批量用例**：cmd 传用例名，App 内查表执行并汇总 exit code；PC 脚本批量驱动
- **busybox 整套工具**：静态编译 busybox 为 libbusybox.so，cmd 形式 `busybox ls -l`
  （libbusybox.so 内部按 argv[0]/argv[1] 分发 applet）

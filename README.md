# BinRunner — 非 root 鸿蒙手机（HarmonyOS NEXT 零售版）上的二进制执行器

在 NEXT 零售机上，App 沙箱内 **一切 `execve` 都被 SELinux 禁止**（libs 目录、files 目录、
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

### 1. 签名

DevEco Studio 打开工程，File → Project Structure → Signing Configs → 自动签名。
**必须是 debug 签名**（jit prctl 只对 debug 应用开放）。

### 2. 构建安装

```bash
export PATH="/Applications/DevEco-Studio.app/Contents/tools/node/bin:/Applications/DevEco-Studio.app/Contents/tools/ohpm/bin:/Applications/DevEco-Studio.app/Contents/tools/hvigor/bin:/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains:$PATH"
export DEVECO_SDK_HOME="/Applications/DevEco-Studio.app/Contents/sdk"
ohpm install --all
hvigorw assembleApp --mode project -p product=default -p buildMode=debug --no-daemon
hdc install entry/build/default/outputs/default/entry-default-signed.hap
```

注：当前工具链的 hvigor 不认识 `hnpPackages`，[entry/hvigorfile.ts](entry/hvigorfile.ts)
里注册了 `InjectHnp` 任务在签名前把 hnp 注入 HAP（hnp 机制在手机上无效，仅为兼容 PC 保留）。

### 3. hdc 触发执行

```bash
hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd "hello foo bar"
hdc shell hilog | grep BinRunner
```

注意：本机 aa 用 `--ps` 传字符串参数（不是 `--es`，不同系统版本参数名不同，报错时看 `aa start -h`）。
规则：`cmd` 第一个词是二进制名，映射为 `lib<名>.so`，其余为参数。
路径占位符：`{files}` 会被替换为 App 沙箱 files 目录（`/data/storage/el2/base/haps/entry/files`）。

### 4. 接入自己的二进制（静态 / 动态均可）

```bash
# 静态（最简单）：OHOS NDK 交叉编译时加 -static，参考 tools/build_hello.sh
# 动态（已实测 mindspore benchmark）：二进制和它的 .so 依赖一起放进 libs 目录
```

- 目标二进制重命名为 `lib<名字>.so` 放入 `entry/libs/arm64-v8a/`
- **动态二进制的依赖库**（如 `libmindspore-lite.so`）放入同一目录；子进程已自动设置
  `LD_LIBRARY_PATH` 指向该目录，musl ld.so 会沿它解析 NEEDED 依赖
- 动态链接器 `/lib/ld-musl-aarch64.so.1` 由 loader 自动加载（App 可读系统 ld-musl，已实测）
- 重新打包安装即可

### 5. 数据文件（模型等）通路

`/data/local/tmp` App 读不到（SELinux），数据文件两条路：

1. **打进 HAP rawfile**（本工程示范）：放 `entry/src/main/resources/rawfile/`，
   App 启动时自动释放到 filesDir，cmd 里用 `{files}/xxx` 引用
2. **`hdc fport` TCP 推送**（适合大文件/频繁更换）：App 起 socket server，
   `hdc fport tcp:8888 tcp:8888` 后 PC 直连写入 filesDir

### 6. 实测：MindSpore Lite 模型推理（已跑通）

```bash
hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd \
  "benchmark --modelFile={files}/mobilenetv2.ms --loopCount=5 --warmUpLoopCount=1"
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
| App 读 /data/local/tmp | ❌ SELinux |
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
- **hilog 单条截断**：已按 900 字符分段（BinRunner.ets 的 HILOG_CHUNK）
- **loader 追踪日志**：loader 会向 stderr 打印 `loader: ...` 追踪行（与目标输出混在一起），
  正式使用时删除 third_party/elf/src/loader.c 中的 z_fdprintf 调试行
- **CPU 推理正常；GPU/NPU delegate 不可用**（App 沙箱无权访问对应驱动/服务）
- 二进制以 App uid 运行，受 App 沙箱约束（访问不了其他应用数据等）
- seccomp 存在（Termony 实测 setuid/setgid 会被杀），避免在用例里调用特权 syscall

## 扩展方向

- **TCP 回传**：App 起 socket server，`hdc fport tcp:8888 tcp:8888`，适合大输出/交互式用例
- **批量用例**：cmd 传用例名，App 内查表执行并汇总 exit code；PC 脚本批量驱动
- **busybox 整套工具**：静态编译 busybox 为 libbusybox.so，cmd 形式 `busybox ls -l`
  （libbusybox.so 内部按 argv[0]/argv[1] 分发 applet）

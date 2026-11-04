# ADR-0002：BinRunner 代码梳理与重构（主机侧 + pull 对齐 push + 协议常量单一来源）

状态：已接受（2026-09-10，grilling session 产物）
范围：`binrunner/`（Python 主机侧）、`app/entry/src/main/ets/`（ArkTS）、`protocol/`（新增单一来源）、`docs/`
基线：`pytest` 163 passed；设备 `4VF0225717009856` 在线，HAP 1.4.1，签名材料在 `.build/`
关联：本轮 grilling 的词汇表与执行计划留在 `.grilling/binrunner-refactor/`（本机会话产物，未入库）；
需要长期留存的设计记录即本文件，已从会话目录提升到 `docs/adr/`。

## 背景（梳理结论，全部经实测/读码确认）

### 1. 文档推荐的开发环路跑的不是本仓库的代码

`README.md:140`、`.claude/skills/develop-binrunner` 都推荐 `alias br="python3 binrunner"`。实测：

```
$ python3 binrunner version                         # 在 ~/Code/ohos/BinRunner
BinRunner CLI 1.2.0                                 # 源码是 1.4.1
$ python3 -c "import sys; sys.path[0]='binrunner'; import binrunner; print(binrunner.__file__)"
/Users/zgd/Code/hmos/BinRunner/binrunner/__init__.py # 注意 hmos
```

`python3 <目录>` 把该目录放进 `sys.path[0]`（其内没有 `binrunner/` 子包）→ 回落到 site-packages 的
**editable 安装**，而它指向 `~/Code/hmos/BinRunner`（1.2.0 旧结账）。`which br` 同一个。
后果：本仓库源码经该路径不会被执行；设备是 1.4.1 HAP 而 CLI 是 1.2.0，与 README 警告的错配方向相反。

**正确入口是 `python3 -m binrunner`**（cwd 进 `sys.path`，命中本仓库包）。

### 2. 协议常量三份拷贝、零自动校验，且已漂移

`docs/transfer-spec.md` 的数值与 `binrunner/config.py` / `PushServer.ets` 不一致共 5 处：

| 常量 | 代码（双端一致） | docs |
|---|---|---|
| 续传魔数 | `0x42524E31`（"BRN1"） | `0x42524E32`（"BRN2"） |
| 探针长度 | 65536 | 4096（4 KiB） |
| 在途上限 | 8388608（`2*ACK_INTERVAL`） | 4 MiB |
| 重试次数 | 5 | 3 |
| 变量名 | `RESUME_PROBE_BYTES` | `RESUME_PROBE_SIZE` |

### 3. 已确认的代码缺陷

| # | 位置 | 问题 |
|---|---|---|
| B1 | `BinRunner.ets:97` | `native call failed: ...` 无 `exit=` 前缀 → `parse_exit_code` 返回 **0**：native 失败被报成成功 |
| B2 | `Index.ets:29` | 把 Promise 存进 AppStorage → 页面显示 `[object Promise]` |
| B3 | `PushServer.ets:565,575` | pull 请求头被 TCP 分片即静默 return、不发响应 → CLI 阻塞到 120s 超时 |
| B4 | `PushServer.ets:557` | pull 用同步 `readSync` while 循环 + `send()` 不 await → 阻塞 ArkTS 主线程（正是 push 路径专门规避的模式）；无流控、无续传，`pull.py` 也无 `KeepAlive` |
| B5 | `provision.py:93` | `_verify` 只看 `aa start` 返回码，却打印「全链路验证通过」 |
| B6 | `hilog.py:31` | `_DIAG_PREFIXES` 含 `"exec "`/`"probe"`；hilog 丢掉 `<<< ` 前缀时（代码明确假定会），目标程序以这些词开头的 stdout 行被静默丢弃 |
| B7 | `runner.py:225` | 超时透传 `exit=-1` → `sys.exit(-1)` → shell 看到 255，而 docs 承诺 1 |

### 4. 重复

- `_fmt_size` 在 `push.py:135` / `pull.py:44` 逐字重复。
- `_PULL_CHUNK` / `_PROGRESS_THRESHOLD` 重复 `config.PUSH_CHUNK_SIZE` / `config.PROGRESS_THRESHOLD`。
- **同一批 hilog 行被解析两遍**：`parse_output` + `StreamOutput.consume` 各一套正则，`cmd_run` 每轮 dump 都跑两遍。

## 决策

| # | 决策 | 选择 | 理由 |
|---|---|---|---|
| D1 | 重构范围 | C：文档一致性 + 已确认缺陷 + 主机侧重构 + 开发环路修复 | 漂移与缺陷都已有实测证据；结构重复是漂移的温床 |
| D2 | ArkTS 侧深度 | C：最小正确性修复 **+ pull 提升到与 push 同等成熟度**（异步分块 + ACK 流控 + `KeepAlive` + 续传） | 线上**已有** `offset`/`size` 字段与按偏移读的实现，故只需补客户端续传循环与背压；但「安全续传」还需要身份校验，因此新增 pull v2 魔数与探针（见下文 D2 细化） |
| D3 | 常量一致性 | **生成器单一来源** | 三份拷贝已实际漂移；校验测试只能防增量，生成器消除该类 |
| D4 | 生成器契约 | `protocol/protocol.json` 为唯一来源 → 生成 `binrunner/protocol.py` + `app/.../common/Protocol.ets`；**生成物入库**；`build.sh` 构建前运行生成器（写），`tests/test_protocol.py` 断言 `--check` 一致 | ArkTS 编译需要 in-tree 文件；构建时自动生成让「改 JSON 后直接构建」可用，测试侧 `--check` 挡住入库生成物漂移与手改 |
| D5 | docs 数值 | **删除硬编码数值**，改为指向单一来源 | 本轮 5 处漂移全在 docs；删掉第三份拷贝 |

### D4 生成器契约（细化）

`protocol/protocol.json` 只放**跨语言**常量；host-only 策略常量留在 `binrunner/config.py`。

| 键 | 值 | 消费方 |
|---|---|---|
| `PORT` | 8888 | `PushServer` / `cli.py` / `pull.py` / `provision.py`（`config.DEFAULT_PORT` 已删除） |
| `TRANSFER_CHUNK_SIZE` | 262144 | `push.py` / `pull.py` / `PushServer`（**两方向共用同值**） |
| `ACK_INTERVAL` | 4194304 | 双端流控 |
| `MAX_FILE_SIZE` | 4294967296 | 双端上限校验 |
| `MAX_REMOTE_NAME_BYTES` | 256 | 双端路径校验 |
| `RESUME_MAGIC` | 0x42524E31（"BRN1"） | 双端 push v2 分流 |
| `FLAG_RESUME` | 1 | 双端 |
| `RESUME_PROBE_BYTES` | 65536 | 双端续传探针 |
| `PULL_MAGIC` | 0x4C4C5550（线上字节 "PULL"） | 双端 pull v1 分流 |
| `PULL_MAGIC_V2` | 0x324C5550（线上字节 "PUL2"） | 双端 pull v2（带探针）分流 |
| `MIN_HEADER_BYTES` | 12 | 设备侧头部长度判定 |
| `MIN_HEADER_V2_BYTES` | 20 | 设备侧 v2 头部长度判定 |

留在 `config.py`（host-only）：`BUNDLE`、`ABILITY`、`TAG`、`DEVICE_ENV`、`DEVECO_HDC`、
`PROGRESS_THRESHOLD`、`ACK_TIMEOUT`、`MAX_INFLIGHT_BYTES`（由 `protocol.ACK_INTERVAL` 派生）、
`RESUME_MIN_SIZE`、`RESUME_MAX_ATTEMPTS`、`RESUME_BACKOFF`。

生成物头部带 `DO NOT EDIT` 与再生命令；`scripts/gen_protocol.py --check` 不一致即非零退出，
由 `tests/test_protocol.py` 调用；`build.sh` 则在构建 HAP 前直接运行生成器（写）——
这样「改 JSON → 直接构建」可用，而手改生成物仍会被测试挡住。
命名调整：`PUSH_CHUNK_SIZE` → `TRANSFER_CHUNK_SIZE`（pull 复用同一分块，旧名是误导）。

### D2 细化：pull 续传的线上协议变更（唯一一处，已按此实现）

push 的续传靠**探针**确认 `.part` 同源；pull 原先没有任何身份校验，只按本地长度偏移续传会
静默拼接「同名不同内容」的文件。因此 pull 新增 v2：

```
请求 v2:  u32 PULL_MAGIC_V2 | u32 nameLen | name | u64 offset | u64 size | u32 probeLen | probe
响应 v2:  u64 resumeAck | u64 totalSize | payload[resumeAck .. totalSize)
```

- **v2 另起魔数，而不是在 v1 后面追加字段** —— 这是落地时对原方案的关键修正：旧 HAP 会把追加
  的字段误读成 `nameLen`，进而按一个**错误的名字**去读盘。新魔数只会落进 push 的 `parseHeader`
  并以 `bad nameLen` 被拒，不会读到错误路径。
- 探针比对放在**设备侧**（与 push 相反）：`probe` 是客户端本地 `.part` 的头部，设备拿它与
  **源文件**头部逐字节比对；一致且 `0 < offset < totalSize` 才回 `resumeAck = offset`。
  `offset == totalSize` 回 `resumeAck = totalSize` 且不发 payload（本地残片已完整）；
  `totalSize == 0` 表示缺失/不可读，此时 `resumeAck` 也为 0。
- 客户端写 `<local>.part`，收满后 `os.replace` 原子改名，与 push 同构。v2 协商失败时把
  `_PULL_V2_OK` 置 False 并降级为 v1 **整传**，**不会**按长度盲续（v1 没有探针，无法验证同源）。
  注意「失败」的判据是**超时而非断连** —— 见下节的真机修正。
- 设备侧 `handlePull` 改为 `async`：每块 `await client.send(...)` 取得背压并让出事件循环，
  不再用同步 `while` 循环 + 不 await 的 send 占满 ArkTS 主线程（那正是 push 的 ACK 流控要规避
  的模式）。失败路径一律显式回零响应（v1 8 字节 / v2 16 字节），不再静默断连 —— 否则 CLI 会
  空等到 socket 超时。
- **已知限制**：`totalSize = 0` 同时表示「不存在」与「空文件」，故 `br pull` 一个 0 字节文件会报
  「设备上不存在」。修它需要设备侧再回一个 found 标志（又一次线上变更），不在本轮范围；
  `tests/test_pull.py` 已把该行为钉住，避免以后误以为可用。

## 影响面

- 新增：`protocol/protocol.json`、`scripts/gen_protocol.py`、`binrunner/protocol.py`（生成）、
  `app/entry/src/main/ets/common/Protocol.ets`（生成）、`binrunner/transfer.py`、`tests/test_protocol.py`、
  `tests/test_transfer.py`、`tests/test_pull.py`
- 改：`binrunner/config.py`（瘦身）、`push.py`（拆出传输层）、`pull.py`（续传+保活）、
  `runner.py`（合并解析 + 退出码）、`hilog.py`（单次解析）、`provision.py`（真实校验）、
  `PushServer.ets`（pull 异步化 + 协议头）、`BinRunner.ets`（B1）、`Index.ets`（B2）、
  `napi_init.cpp`（诊断日志加 `DIAG ` 标记 + spawn 失败退出码）、`build.sh`（构建前生成协议常量）、全部 `docs/`、`README.md`
- 不入库不变：`.grilling/` 仍是本机会话产物（本仓库未 gitignore，提交与否由用户定）

## 落地结论与验证（实现后的事实）

- **解析合并**：`parse_output` + `StreamOutput` → 单个 `HilogParser`（单遍）。快照去重语义保留
  （同一快照内重复保留、跨快照丢弃），`[i/n]` 分段缓存跨轮复用。`test_empty_stream` 的期望按新
  退出码契约从 -1 改为 1（语义「空流不算成功」未变）。
- **退出码契约（落地时修正了一处过度简化）**：`-1` 不能同时表示超时与 spawn 失败。实现改为
  设备侧 spawn 失败（ResolveExecPath / pipe / fork）报 `exit=255`，`-1` 只留给超时，CLI 再把
  负值映射为 1。两处都与 `docs/cli-reference.md` 原有承诺一致（超时 → 1，命令未找到 → 255），
  且 CLI 无需嗅探报告文本。设备实测：`hello` → 42；不存在的命令 → 255；`--timeout 3` 跑长任务 → 1。
- **新增漂移（梳理时漏掉，实现中发现）**：`docs/transfer-spec.md` 的 push v2 头**漏写了 `u32 flags`**
  （真实布局是 `magic|flags|nameLen|name|u64 size|[probeLen|probe]`）。已在文档修正。
- **`br setup` 真正验证**：`_verify` 改为调用 `runner.cmd_run` 执行 `hello` 并断言退出码 42
  （`examples/hello/hello.c` 故意返回 42），不再只看 `aa start` 的返回码。
- **测试**：242 passed（基线 163）。`TestSafeRemoteName` 的 15 个用例随实现迁到
  `tests/test_transfer.py`（覆盖未丢失）；新增 `tests/test_protocol.py`、`tests/test_pull.py`
  （pull 此前**零覆盖**）以及状态行与退出码用例。`pyflakes` 干净。
- **CI 矩阵（3.9 / 3.11 / 3.13）**：`test.yml` 覆盖 py3.9，而 `requires-python = ">=3.9"`。
  本轮改动引入过两个 **3.9 专属**缺陷，属「本地 3.12 全绿、CI 3.9 挂」类型：
  (a) `tests/test_pull.py` 的签名注解 `int | None` 在 3.9 的**定义期**就报 TypeError（测试文件
  没有 `from __future__ import annotations`，与源码约定不一致）→ 按源码约定补上该 future import；
  (b) `except TimeoutError` 在 3.9 抓不到真实 socket 超时（`socket.timeout is TimeoutError`
  在 3.9 为 False、3.10+ 为 True）—— 重构 `_read_ack` 时把原本正确的 `except socket.timeout`
  改坏了 → 在 `transfer.py` 单点定义 `SOCKET_TIMEOUT = (TimeoutError, socket.timeout)`，
  push 的 ACK 等待与 pull 的协商窗口共用。三个 leg 现均 242 passed。
- **sdist 自洽**：原无 `MANIFEST.in`，sdist 会随包发出 `tests/test*.py` 却不含
  `protocol/`、`scripts/`、`app/entry/src/main/ets/common/*.ets` → 从 sdist 跑测试 **7 个失败**。
  新增 `MANIFEST.in` 把这批仓库级输入一并发出（选择发出输入而非 `prune tests`：后者会让任何基于
  sdist 的流程**静默失去**这层漂移守卫）。已实测该 sdist 的套件在 3.12 与 3.9 上均通过。
- **真机实测**（零售设备）：push 从设备侧 5MiB `.part` 续传（`续传自 5.0 MiB`）→ 回拉 sha256 一致；
  pull 20MiB 字节一致；pull 从本地 8MiB `.part` 续传（`续传自 8.0 MiB`）sha256 一致；
  小文件 pull 一致；`br rm` 正常；`br setup --reinstall` 的全链路验证通过（exit=42）。
- **旧 HAP 兼容（真机复测后修正）**：最初我按「旧 HAP 会断开连接」建模、用 EOF 触发降级，
  单元测试因此全绿 —— 但真机旧 HAP 的行为是**静默挂住**（`fail()` 只关 fd，不 close 连接），
  于是新 CLI 每次带 `.part` 的 pull 都会白等满 `ACK_TIMEOUT`：**实测 61 秒**。用户报告的
  「新 CLI 不兼容旧 HAP」即指此事。修法是给协商窗口单独的短超时
  （`config.PULL_NEGOTIATE_TIMEOUT = 3.0`），并把「超时」与「断连」都视为不支持；
  实测 61 秒 → 5 秒（3 秒判定 + 20MiB 整传），内容 sha256 一致。
- **旧 HAP 退出码（同一轮复测发现）**：旧 HAP 对「命令没找到」也只回 `exit=-1`，原先「负值
  一律映射为 1」会让旧 HAP 下该场景报 1，与新 HAP 的 255 不一致。改为用状态行的 `timedOut`
  分流（`runner.cli_exit_code`），两代 HAP 现在都给出文档承诺的退出码（超时 1 / 未起来 255）。
- **教训**：用「对端会断开」这种未经真机确认的假设去设计降级路径，测试会跟着同一个假设走，
  于是全绿却错。降级路径必须为「对端既不响应也不断连」这种最坏情况设**上界超时**。
- **`br` 的安装源已切换（本项已处置）**：原先 `br`（`~/Tool/Miniconda3/bin/br`）是
  `~/Code/hmos/BinRunner` 的 editable 安装（1.2.0），正是上文错配的根源。用户处置：
  卸载该安装、以 `pip install -e .` 指向本仓库（1.4.1），并从任意 cwd 验证
  `br version` / `br run hello` 全链路通过。**副作用**：错误写法 `python3 binrunner`
  如今也解析到本仓库，枪已拆掉 —— 但该写法仍是错的，文档保留正确入口。
  旧结账目录本身（69MB）仍在磁盘上未删除，是否清理由用户决定。

### 收尾时的仓库级修正

- **设计记录不再放在临时目录**：本 ADR 原先只是 `.grilling/binrunner-refactor/` 下的会话产物
  （未跟踪），一次 `git clean` 就会丢掉「pull v2 为什么另起魔数」「退出码为什么用 timedOut
  分流」这些当下看不出理由的决定。已移入 `docs/adr/`，与 ADR-0001 同处。
- **`.venv/` / `venv/` 纳入 `.gitignore`**：仓库原先未覆盖仓库内虚拟环境（`.memsearch/`、
  `.build/` 都已忽略），实测在仓库里跑一次 uv 就会留下 `.venv/` 与 `uv.lock`。
- **`LICENSE` 已补**：`pyproject.toml` 早已声明 `license = "MIT"`，但仓库缺许可文件，wheel/sdist
  的元数据因此指向不存在的许可文本。

  许可**类型**来自 `pyproject.toml:7` 的既有声明（不是本轮的判断）。许可**正文**没有手写，
  而是取 GitHub 许可选择器的数据源 `github/choosealicense.com` 的 `_licenses/mit.txt`，
  仅按规定替换它自己的两个占位符：`[year]` → 2026（首个提交 2026-07-22）、`[fullname]` →
  Vincent Zhu（本仓库 git 作者统计与 GitHub owner 的实证）。这正是该文件 `how:` 字段
  所要求的操作，无自由发挥。

  取证（可复核，不是断言）：
  - 与 GitHub 模板（替换占位符后）**逐字节相同**：1068 B，
    `sha256 4f751162ebb863b7a0750d8ea44552be3170d4749b04f5ab94360ede3e12910e`，diff 为空；
  - 与 SPDX `license-list-data` 的 `text/MIT.txt` 措辞**完全相同**（署名行占位符归一后
    SequenceMatcher ratio = 1.0）。两个独立一手来源互证。

  注意方法：早先我用「凭记忆重敲的标准文本」做比对并报告 `verbatim: True` —— 那是**循环论证**
  （两边同源于我的记忆），只证明自洽、不证明符合标准。现改为取网络上的原始字节比对
  （`read` 的显示不保证字节精度，故用 `urllib` 取 bytes 并比 SHA-256）。

  打包侧实测：wheel 含 `dist-info/licenses/LICENSE`，且 wheel 与 sdist 两侧元数据均为
  `Metadata-Version: 2.4` + `License-Expression: MIT` + `License-File: LICENSE`。
  若版权主体需改为公司或并列贡献者，改 `LICENSE` 第 3 行一处即可（正文其余部分不得改动）。

- **版本号 bump 到 1.5.0（已做）**：pull 线上协议新增 v2，而 `app.json5` 的版本此前与包版本同为
  1.4.1，两者无法区分。bump 后 `__version__`、wheel/sdist 名、`app.json5`（versionCode
  1004001 → 1005000）与 `RELEASE.md` 的 v1.5.0 小节一致，并由 `sync_app_version.py --check`
  在 CI 守住。

  **bump 本身不会让设备升级**：`br setup` 只要检测到已安装 HAP 就提示跳过（**不比较版本号**），
  升级必须显式 `br setup --reinstall` —— 这是 `docs/cli-reference.md` 的既有契约，不是缺陷。
  我一度推断「bump 后用户就能拿到新协议」，**是错的**；该推断未经真机/读码确认，已按实际代码
  更正，并在 `docs/cli-reference.md` 写明「版本号不同不会自动升级」。bump 的真实收益是版本可
  区分、发布身份明确（tag/version 校验才有意义）、`br version` 能暴露两者不一致。

- **CI 护栏（已加）**：`hap-sign.yml` 直接调 hvigorw、**不走 `build.sh`**，故协议生成器与版本
  联动都不会自动执行，而它发布的正是可安装的 nightly HAP。已在该 workflow 构建前加
  `gen_protocol.py --check` 与 `sync_app_version.py --check`（后者为本轮新增的只读 `--check`
  模式），宁可失败也不发布谎报版本的产物。

- **版本不一致的提示（用户决定：加打印，原则是两者必须一致）**：`ensure_app`（run/push/pull/
  ls/rm/logs 的自动安装入口）在已安装但版本与 CLI 不一致时向 **stderr** 打一行警告，含设备版本、
  CLI 版本与两侧升级方式；**只警告、不自动升级**（自动替换已装 HAP 会越过 `br setup --reinstall`
  的显式契约，且有把可用环境换坏的风险）。`br setup` 的「已安装」分支现在区分一致/不一致：
  一致时说「无需升级」，不一致时同时给出两个版本与 `--reinstall`。
  走 **stderr** 的理由：stdout 是命令的正式输出（`br ls` / `br run` 的内容），可能被脚本捕获。
  警告只在设备触达类命令上出现，每次调用一次；`br version` 与 `br setup` 在 `_NEEDS_APP` 之前
  就返回，不会与它们自身的版本输出重复。

## 明确排除

- **不拆 `PushServer.ets` 文件**（D2 只重写 `handlePull` + 协议头，不改文件划分）。
- **不删 `probe`/`probe2` 诊断命令**（只给 native 诊断日志加 `DIAG ` 标记）。
- 不做 `br shell`、TCP 大输出回传、`LD_LIBRARY_PATH` 递归等 README「扩展方向」项。
- 仓库无 `CHANGELOG`；`RELEASE.md` 已按 AGENTS.md 补 v1.5.0 小节（见上「收尾」）。

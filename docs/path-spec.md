# 1.5.0 路径规格与迁移

版本：1.5.0。不兼容变更；对应 issue #13。

## 单一根目录

`@` 是 App 的 files 根目录（`context.filesDir`），不是系统 Shell 的语法。
所有相对文件路径都以该根目录为起点，不再隐式添加 bin/。

| 输入 | 实际位置 |
|---|---|
| `x` / `@/x` | `files/x` |
| `bin/x` / `@/bin/x` | `files/bin/x` |
| `models/x` / `@/models/x` | `files/models/x` |

- push/pull：CLI 去掉开头 `@/`，规范化 `./` 和重复分隔符后发送相对路径。
  拒绝根目录、`..`、绝对路径、反斜杠、NUL、独立的 `@` 路径段。
  目录推送保持本地目录内容的相对结构，不包含外层目录名。
- ls：默认列 files 根目录；`ls bin` 与 `ls @/bin` 等价，`ls @` 可列根。
- rm：相对路径和 `@/` 同上；拒绝删除 files 根及其别名、files 外路径、含 `..` 的路径。
  bin/ 不再是特殊保护目录，`rm bin` 会递归删除它。
- run：相对程序路径从 files/ 查找，绝对程序路径原样使用。
  裸命令名仍可回退到 HAP libs 中的 `lib<name>.so`。
  子进程 cwd 固定为 files/，因此 `run "x models/data"` 中的相对数据路径也从 files/ 读取。
  参数不会被强制改写为路径，仍保留词首 `@/` 和 `--option=@/` 展开。
  `LD_LIBRARY_PATH` 是 files/ 加 HAP libs，不递归查找子目录。

## 升级与旧文件

CLI 和 HAP 必须同步升级到 1.5.x。新 CLI 会拒绝不同主次版本的 HAP，并提示
`br setup --reinstall`。旧 CLI 没有这道检查，必须停用；不能把新 HAP 与旧 CLI 混用。
传输报文布局沿用旧版，但相对根的含义已经变化，不代表语义兼容。

升级保留设备数据，不自动搬迁、不自动清理、不覆盖旧文件。

| 旧操作（≤1.4.x） | 1.5.x 访问相同旧文件 |
|---|---|
| `push local x` | `push local bin/x` |
| `pull x local` | `pull bin/x local` |
| `rm x` | `rm bin/x` |
| `run "x"`（旧推送文件） | `run "bin/x"` |

推荐重新推送程序及动态库到 files/ 根层级。旧 bin/ 中的动态库不会自动被搜索，
旧程序的数据相对路径也必须按新 cwd 检查；不要仅改程序名就假定所有参数保持不变。

迁移单个文件（升级到 1.5.x 后执行，先检查目的位置是否已有同名文件）：

```bash
br ls @
br pull bin/x ./saved-x
br push ./saved-x x
br pull x ./verified-x
cmp ./saved-x ./verified-x
# 确认内容和业务运行正常后，才清理旧副本
br rm bin/x
```

#13 历史误写文件在 files/bin/@/bin/ 中。1.5.x 传输校验拒绝独立 @ 路径段，
因此应在升级前用匹配的 ≤1.4.x CLI/HAP 救出：

```bash
# ≤1.4.x 环境：pull 远端名仍按字面量，相对 files/bin/
br pull '@/bin/x' ./rescued-x
# 升级 CLI/HAP 到 1.5.x 后
br push ./rescued-x bin/x
br pull bin/x ./verified-x
cmp ./rescued-x ./verified-x
# 确认后只删除指定旧文件，勿递归清理整个历史目录
br rm '@/bin/@/bin/x'
```

## 验证

自动化覆盖：CLI 归一化、push/pull 实际请求路径、路径穿越拒绝、设备侧路径
解析函数及双端校验一致性、跨主次版本拒绝。设备端 helper 由 Node 23+ 执行类型擦除后的
同一份源码；它不代替 ArkTS 编译和真机文件系统验证。

真机回归需验证：推送 `x` 后可在根目录看到；`pull @/x` 内容一致；
`push ... bin/x` 可从 `@/bin/x` 读取；程序 cwd 为 files/；根目录删除被拒绝。

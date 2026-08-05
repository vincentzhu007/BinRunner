此目录存放要打包进 HAP 的二进制。

规则：
1. 必须是 OHOS/arm64 的 ELF（用 OHOS NDK 交叉编译，见 tools/build_hello.sh）
2. 文件名必须是 lib<名字>.so 形式（HAP 打包器只认 lib*.so）
   例如 busybox 编译产物应命名为 libbusybox.so
3. 零售机 libs 目录 noexec（SELinux 禁止 execv），App 通过内存 ELF loader
   （fork 子进程后 mmap 匿名页 + jit prctl + 直接跳转）绕过 execve 执行
4. 不要试图运行时把二进制复制到 files 目录再执行 —— 沙箱 files 目录同样是 noexec

已内置：libhello.so（源码 tools/hello.c）

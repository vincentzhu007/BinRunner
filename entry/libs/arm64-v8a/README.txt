此目录存放要打包进 HAP 的二进制。

规则：
1. 必须是 OHOS/arm64 的 ELF（用 OHOS NDK 交叉编译，见 ../tools/build_hello.sh）
2. 文件名必须是 lib<名字>.so 形式（HAP 打包器只认 lib*.so）
   例如 busybox 编译产物应命名为 libbusybox.so
3. 安装后位于 /data/app/el1/bundle/public/com.example.binrunner/libs/arm64/，
   该目录以可执行权限挂载，App 进程可以 execv 其中的文件
4. 不要试图运行时把二进制复制到 files 目录再执行 —— 沙箱 files 目录是 noexec 挂载

已内置：libhello.so（源码 ../tools/hello.c）

#include <napi/native_api.h>
#include <hilog/log.h>

// OHOS NDK 惯例：重定义 LOG_DOMAIN/LOG_TAG 宏，OH_LOG_XXX 系列宏会把它们作为 domain/tag
#undef LOG_DOMAIN
#undef LOG_TAG
#define LOG_DOMAIN 0x0001
#define LOG_TAG "BinRunner"

#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <elf.h>
#include <string>
#include <vector>
#include <dirent.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>
#include <unistd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>

namespace {

struct ExecResult {
    int exitCode = -1;      // -1: spawn 失败；>=0: 子进程退出码；128+sig: 被信号杀死
    bool timedOut = false;
    std::string out;
    std::string err;
};

long long NowMs()
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<long long>(ts.tv_sec) * 1000 + ts.tv_nsec / 1000000;
}

void SetNonBlocking(int fd)
{
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0) {
        fcntl(fd, F_SETFL, flags | O_NONBLOCK);
    }
}

// probe2 用：测试对指定文件做 PROT_EXEC mmap 是否被 SELinux 放行
void ProbeExecMmap(const std::string &path)
{
    int fd = open(path.c_str(), O_RDONLY);
    if (fd < 0) {
        OH_LOG_WARN(LOG_APP, "probe2 %{public}s: open failed: %{public}s", path.c_str(), strerror(errno));
        return;
    }
    void *p = mmap(nullptr, 4096, PROT_READ | PROT_EXEC, MAP_PRIVATE, fd, 0);
    OH_LOG_WARN(LOG_APP, "probe2 exec-mmap %{public}s: %{public}s", path.c_str(),
                p == MAP_FAILED ? strerror(errno) : "OK");
    if (p != MAP_FAILED) {
        munmap(p, 4096);
    }
    close(fd);
}

// 调试用：列出目录内容到 hilog，用于定位 hnp 实际安装位置
void ProbeDir(const std::string &path)
{
    DIR *dir = opendir(path.c_str());
    if (dir == nullptr) {
        OH_LOG_WARN(LOG_APP, "probe %{public}s: %{public}s", path.c_str(), strerror(errno));
        return;
    }
    std::string entries;
    struct dirent *ent;
    while ((ent = readdir(dir)) != nullptr) {
        if (ent->d_name[0] == '.') {
            continue;
        }
        entries += ent->d_name;
        entries += (ent->d_type == DT_DIR) ? "/ " : " ";
    }
    closedir(dir);
    OH_LOG_WARN(LOG_APP, "probe %{public}s: %{public}s", path.c_str(), entries.c_str());
}

// 决定从哪个路径读取 ELF（内存 loader 只需要读权限，不需要文件系统 exec 权限）：
// 0. 绝对路径（@/... 在 ArkTS 层展开后原样传来）：直接使用
// 1. 推送目录 filesBinDir/<name>（hdc fport + PushServer 免打包推入，原样文件名，优先级最高）
// 2. HAP libs 目录的 lib<name>.so（标准方式）
std::string ResolveExecPath(const std::string &binDir, const std::string &filesBinDir,
                            const std::string &name, std::string &err)
{
    if (!name.empty() && name[0] == '/') {
        if (access(name.c_str(), R_OK) == 0) {
            return name;
        }
        err = "not readable: " + name;
        return "";
    }

    if (!filesBinDir.empty()) {
        const std::string pushedPath = filesBinDir + "/" + name;
        if (access(pushedPath.c_str(), R_OK) == 0) {
            OH_LOG_INFO(LOG_APP, "resolved via push dir: %{public}s", pushedPath.c_str());
            return pushedPath;
        }
    }

    const std::string libPath = binDir + "/lib" + name + ".so";
    if (access(libPath.c_str(), R_OK) == 0) {
        return libPath;
    }

    err = "not readable: " + (filesBinDir.empty() ? "" : filesBinDir + "/" + name + ", ") + libPath;
    return "";
}

// MikhailProg/elf loader 入口：从伪造的初始栈读取 argv/env/auxv，
// 将目标 ELF 映射进匿名内存后跳转执行（完全不经过 execve）
extern "C" void z_entry(unsigned long *sp, void (*fini)(void));

// 子进程崩溃兜底：跳转后目标/ld.so 若 SIGSEGV/SIGBUS/SIGILL，
// 捕获并打印 PC 与 fault 地址（无 execve，信号处置跨跳转保留）
void CrashHandler(int sig, siginfo_t *info, void *uc)
{
    ucontext_t *ctx = static_cast<ucontext_t *>(uc);
    char buf[256];
    int n = snprintf(buf, sizeof(buf), "CRASH sig=%d fault_addr=%p pc=0x%llx sp=0x%llx x0=0x%llx\n",
                     sig, info->si_addr,
                     (unsigned long long)ctx->uc_mcontext.pc,
                     (unsigned long long)ctx->uc_mcontext.sp,
                     (unsigned long long)ctx->uc_mcontext.regs[0]);
    ssize_t unused = write(STDERR_FILENO, buf, n);
    (void)unused;
    _exit(128 + sig);
}

void InstallCrashHandler()
{
    // 跳转后 sp 指向伪造栈，若崩溃时 sp 已坏，内核无处压信号帧 → handler 不会触发。
    // 备用信号栈（SA_ONSTACK）保证崩溃一定被捕获。
    static char altStack[128 * 1024];
    stack_t ss;
    memset(&ss, 0, sizeof(ss));
    ss.ss_sp = altStack;
    ss.ss_size = sizeof(altStack);
    ss.ss_flags = 0;
    sigaltstack(&ss, nullptr);

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_sigaction = CrashHandler;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK;
    sigaction(SIGSEGV, &sa, nullptr);
    sigaction(SIGBUS, &sa, nullptr);
    sigaction(SIGILL, &sa, nullptr);
    sigaction(SIGABRT, &sa, nullptr);
}

// 在子进程内构造伪造初始栈并调用 z_entry 跳转执行目标 ELF。
// 成功则不返回；z_entry 内部出错会自行打印到 stderr 并 z_exit。
static void RunInMemory(const std::string &path, const std::vector<std::string> &args)
{
    // 栈布局与内核 execve 后一致：
    // [argc][argv0][argv1]...[NULL][env0][env1]...[NULL][auxv type,val]...[AT_NULL,0]
    // 注意：块必须放在大缓冲区的高位，sp 以下留出栈增长空间（模拟真实 exec 栈）。
    // 若 sp 贴近映射段底部，ld.so/目标程序压栈会越界 SIGSEGV（且崩得毫无规律——
    // .bss 布局随构建变化，fakeStack 有时恰好在段底附近，实测因此出现过随机崩溃）。
    constexpr size_t POOL_SLOTS = 32768;   // 256KB
    constexpr size_t HEAD_SLOTS = 2048;    // argv/env/auxv 区，16KB 足够
    static unsigned long stackPool[POOL_SLOTS] __attribute__((aligned(16)));
    unsigned long *fakeStack = &stackPool[POOL_SLOTS - HEAD_SLOTS];
    static char randomBytes[16] = {
        0x3a, 0x91, 0x5c, 0x27, 0xe4, 0x08, 0xb6, 0xd1,
        0x7f, 0x33, 0xaa, 0x1e, 0xc9, 0x65, 0xf2, 0x4b,
    };
    static const char loaderName[] = "elfloader";

    const size_t argc = 2 + args.size();
    size_t i = 0;
    fakeStack[i++] = static_cast<unsigned long>(argc);
    fakeStack[i++] = reinterpret_cast<unsigned long>(loaderName);   // argv[0]，loader 会将其移出
    fakeStack[i++] = reinterpret_cast<unsigned long>(path.c_str()); // argv[1] = 目标路径
    for (const auto &a : args) {
        fakeStack[i++] = reinterpret_cast<unsigned long>(a.c_str());
    }
    fakeStack[i++] = 0;                                             // argv 结束
    for (char **e = environ; *e != nullptr; e++) {
        fakeStack[i++] = reinterpret_cast<unsigned long>(*e);
    }
    fakeStack[i++] = 0;                                             // env 结束
    auto auxv = [&i, fakeStack](unsigned long type, unsigned long val) {
        fakeStack[i++] = type;
        fakeStack[i++] = val;
    };
    // 这几项由 loader 改写为正确值（musl 静态初始化需要 AT_PHDR 系列，占位即可）
    auxv(AT_PHDR, 0);
    auxv(AT_PHENT, 0);
    auxv(AT_PHNUM, 0);
    auxv(AT_ENTRY, 0);
    // 动态链接场景必需：AT_BASE 缺失时 musl ld.so 会误判为"被直接调用"而走错路径（实测 SIGSEGV）
    auxv(AT_BASE, 0);
    auxv(AT_EXECFN, 0);
    auxv(AT_PAGESZ, 4096);
    auxv(AT_RANDOM, reinterpret_cast<unsigned long>(randomBytes));
    auxv(AT_UID, getuid());
    auxv(AT_EUID, geteuid());
    auxv(AT_GID, getgid());
    auxv(AT_EGID, getegid());
    auxv(AT_SECURE, 0);
    auxv(AT_NULL, 0);

    z_entry(fakeStack, nullptr);
    // 不应到达
}

// 执行指定二进制，捕获 stdout/stderr，超时强杀。
ExecResult ExecBinary(const std::string &binDir, const std::string &filesBinDir,
                      const std::string &name, const std::vector<std::string> &args, int timeoutSec)
{
    ExecResult res;
    std::string resolveErr;
    const std::string path = ResolveExecPath(binDir, filesBinDir, name, resolveErr);
    if (path.empty()) {
        res.err = resolveErr;
        OH_LOG_WARN(LOG_APP, "%{public}s", res.err.c_str());
        return res;
    }

    int outPipe[2] = {-1, -1};
    int errPipe[2] = {-1, -1};
    if (pipe(outPipe) != 0 || pipe(errPipe) != 0) {
        res.err = std::string("pipe() failed: ") + strerror(errno);
        return res;
    }

    pid_t pid = fork();
    if (pid < 0) {
        res.err = std::string("fork() failed: ") + strerror(errno);
        return res;
    }

    if (pid == 0) {
        // ---- 子进程 ----
        dup2(outPipe[1], STDOUT_FILENO);
        dup2(errPipe[1], STDERR_FILENO);
        close(outPipe[0]); close(outPipe[1]);
        close(errPipe[0]); close(errPipe[1]);

        // 动态链接的用例（如 mindspore benchmark）跳转后由 musl ld.so 加载依赖，
        // 沿 LD_LIBRARY_PATH 搜索；推送目录在前（免打包推送的 .so 依赖优先），
        // 其次是 libs 目录（libmindspore_lite.so 等打包进来的）。
        // environ 会被 RunInMemory 透传给目标进程。
        const std::string ldPath = filesBinDir.empty() ? binDir : filesBinDir + ":" + binDir;
        setenv("LD_LIBRARY_PATH", ldPath.c_str(), 1);

        InstallCrashHandler();

        std::vector<char *> argv;
        argv.push_back(const_cast<char *>(path.c_str()));
        for (const auto &a : args) {
            argv.push_back(const_cast<char *>(a.c_str()));
        }
        argv.push_back(nullptr);

        execv(path.c_str(), argv.data());
        // execve 被零售机 SELinux 全面禁止 → 转内存加载执行（成功则不返回）
        OH_LOG_INFO(LOG_APP, "execv blocked (%{public}s), fallback to in-memory elf loader", strerror(errno));

        // 实测坑：bundle libs 目录里的文件顺序读正常，但随机读（lseek+read）
        // 行为异常（疑似映射/虚拟文件），loader 按段随机读会拿到坏数据导致 SIGSEGV。
        // 先顺序复制进 memfd（纯内存文件，lseek 语义正常）再让 loader 加载。
#ifdef SYS_memfd_create
        int mfd = static_cast<int>(syscall(SYS_memfd_create, "binrunner", 0));
        if (mfd >= 0) {
            int src = open(path.c_str(), O_RDONLY);
            if (src >= 0) {
                char copyBuf[8192];
                ssize_t r = 0;
                bool copyOk = true;
                while ((r = read(src, copyBuf, sizeof(copyBuf))) > 0) {
                    if (write(mfd, copyBuf, r) != r) {
                        copyOk = false;
                        break;
                    }
                }
                close(src);
                if (copyOk && r == 0) {
                    // 诊断：校验复制进 memfd 的内容是否为完整合法的 ELF
                    off_t total = lseek(mfd, 0, SEEK_END);
                    unsigned char hdr[20] = {0};
                    ssize_t got = pread(mfd, hdr, sizeof(hdr), 0);
                    OH_LOG_WARN(LOG_APP,
                        "memfd diag: size=%lld first=%{public}zd magic=%02x %02x %02x %02x class=%02x type=%02x%02x machine=%02x%02x",
                        (long long)total, got,
                        hdr[0], hdr[1], hdr[2], hdr[3], hdr[4], hdr[17], hdr[16], hdr[19], hdr[18]);
                    lseek(mfd, 0, SEEK_SET); // loader 从头读 ELF header
                    // "@" 前缀约定：loader 直接使用该 fd（沙箱内 /proc/self/fd 不可访问）
                    char fdPath[32];
                    snprintf(fdPath, sizeof(fdPath), "@%d", mfd);
                    RunInMemory(fdPath, args);
                }
            }
        }
#endif
        RunInMemory(path, args);
        _exit(127);
    }

    // ---- 父进程 ----
    close(outPipe[1]);
    close(errPipe[1]);
    SetNonBlocking(outPipe[0]);
    SetNonBlocking(errPipe[0]);

    struct pollfd fds[2] = {
        {outPipe[0], POLLIN, 0},
        {errPipe[0], POLLIN, 0},
    };
    bool openFd[2] = {true, true};
    char buf[4096];
    const long long deadline = NowMs() + static_cast<long long>(timeoutSec) * 1000;

    while (openFd[0] || openFd[1]) {
        long long remain = deadline - NowMs();
        if (remain <= 0) {
            res.timedOut = true;
            kill(pid, SIGKILL);
            break;
        }
        int n = poll(fds, 2, remain > 200 ? 200 : static_cast<int>(remain));
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        for (int i = 0; i < 2; i++) {
            if (!openFd[i]) {
                continue;
            }
            if (fds[i].revents & (POLLIN | POLLHUP | POLLERR)) {
                ssize_t r = read(fds[i].fd, buf, sizeof(buf));
                if (r > 0) {
                    (i == 0 ? res.out : res.err).append(buf, r);
                } else if (r == 0 || (r < 0 && errno != EAGAIN && errno != EINTR)) {
                    close(fds[i].fd);
                    openFd[i] = false;
                }
            }
        }
    }

    int status = 0;
    waitpid(pid, &status, 0);
    if (res.timedOut) {
        res.exitCode = -1;
    } else if (WIFEXITED(status)) {
        res.exitCode = WEXITSTATUS(status);
    } else if (WIFSIGNALED(status)) {
        res.exitCode = 128 + WTERMSIG(status);
    }
    return res;
}

// ---- NAPI 参数解析辅助 ----

std::string GetString(napi_env env, napi_value value)
{
    size_t len = 0;
    napi_get_value_string_utf8(env, value, nullptr, 0, &len);
    std::string s(len, '\0');
    size_t copied = 0;
    napi_get_value_string_utf8(env, value, &s[0], len + 1, &copied);
    s.resize(copied);
    return s;
}

void SetProp(napi_env env, napi_value obj, const char *key, napi_value value)
{
    napi_set_named_property(env, obj, key, value);
}

// runBin(binDir: string, name: string, args: string[], timeoutSec: number, filesBinDir?: string)
//   => { exitCode: number, timedOut: boolean, stdout: string, stderr: string }
// filesBinDir：PushServer 接收目录（filesDir/bin），免打包推送的二进制/依赖库放这里，可省略
napi_value RunBin(napi_env env, napi_callback_info info)
{
    size_t argc = 5;
    napi_value argv[5] = {nullptr};
    napi_get_cb_info(env, info, &argc, argv, nullptr, nullptr);
    if (argc < 4) {
        napi_throw_error(env, nullptr, "runBin requires (binDir, name, args, timeoutSec[, filesBinDir])");
        return nullptr;
    }

    std::string binDir = GetString(env, argv[0]);
    std::string name = GetString(env, argv[1]);

    std::vector<std::string> args;
    uint32_t arrLen = 0;
    napi_get_array_length(env, argv[2], &arrLen);
    for (uint32_t i = 0; i < arrLen; i++) {
        napi_value item = nullptr;
        napi_get_element(env, argv[2], i, &item);
        args.push_back(GetString(env, item));
    }

    int32_t timeoutSec = 30;
    napi_get_value_int32(env, argv[3], &timeoutSec);

    std::string filesBinDir;
    if (argc >= 5 && argv[4] != nullptr) {
        filesBinDir = GetString(env, argv[4]);
    }

    OH_LOG_INFO(LOG_APP, "exec lib%{public}s.so argc=%{public}zu", name.c_str(), args.size());

    // 隐藏调试命令：cmd = "probe" 时枚举关键目录
    if (name == "probe") {
        ProbeDir("/data/app");
        ProbeDir("/data/app/bin");
        ProbeDir(binDir);              // .../libs/arm64
        ProbeDir(binDir + "/..");      // .../libs
        ProbeDir(binDir + "/../..");   // bundle 根（沙箱视图 /data/storage/el1/bundle）
        ExecResult pr;
        pr.exitCode = 0;
        pr.out = "probe done, see hilog";
        napi_value result = nullptr;
        napi_create_object(env, &result);
        napi_value v;
        napi_create_int32(env, pr.exitCode, &v);
        SetProp(env, result, "exitCode", v);
        napi_get_boolean(env, false, &v);
        SetProp(env, result, "timedOut", v);
        napi_create_string_utf8(env, pr.out.c_str(), pr.out.size(), &v);
        SetProp(env, result, "stdout", v);
        napi_create_string_utf8(env, "", 0, &v);
        SetProp(env, result, "stderr", v);
        return result;
    }

    // 隐藏调试命令：cmd = "probe2" 检测动态链接用例（如 mindspore benchmark）的可行性：
    // 1. /data/local/tmp 可读性（hdc 推送目录，App 能否直接读）
    // 2. 系统动态链接器 /lib/ld-musl 可读性（loader 加载动态 ELF 的前提）
    // 3. 各目录文件 PROT_EXEC mmap（决定 .so 依赖允许放哪里）
    if (name == "probe2") {
        ProbeDir("/data/local/tmp");
        const char *interp = "/lib/ld-musl-aarch64.so.1";
        OH_LOG_WARN(LOG_APP, "probe2 access(%{public}s, R_OK): %{public}s",
                    interp, access(interp, R_OK) == 0 ? "OK" : strerror(errno));
        // 对照组：libs 目录（dlopen 已证明可行）
        ProbeExecMmap(binDir + "/libhello.so");
        // 沙箱 files/cache 目录（模型和第三方 .so 若必须放沙箱，需要这里能 exec-mmap）
        const std::string cacheFile = "/data/storage/el2/base/cache/.probe_tmp";
        int tfd = open(cacheFile.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (tfd >= 0) {
            char zeros[4096] = {0};
            ssize_t w = write(tfd, zeros, sizeof(zeros));
            (void)w;
            close(tfd);
        }
        ProbeExecMmap(cacheFile);
        const std::string filesFile = "/data/storage/el2/base/haps/entry/files/.probe_tmp";
        tfd = open(filesFile.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (tfd >= 0) {
            char zeros[4096] = {0};
            ssize_t w = write(tfd, zeros, sizeof(zeros));
            (void)w;
            close(tfd);
        }
        ProbeExecMmap(filesFile);
        ExecResult pr;
        pr.exitCode = 0;
        pr.out = "probe2 done, see hilog";
        napi_value result = nullptr;
        napi_create_object(env, &result);
        napi_value v;
        napi_create_int32(env, pr.exitCode, &v);
        SetProp(env, result, "exitCode", v);
        napi_get_boolean(env, false, &v);
        SetProp(env, result, "timedOut", v);
        napi_create_string_utf8(env, pr.out.c_str(), pr.out.size(), &v);
        SetProp(env, result, "stdout", v);
        napi_create_string_utf8(env, "", 0, &v);
        SetProp(env, result, "stderr", v);
        return result;
    }

    ExecResult r = ExecBinary(binDir, filesBinDir, name, args, timeoutSec);

    napi_value result = nullptr;
    napi_create_object(env, &result);

    napi_value v;
    napi_create_int32(env, r.exitCode, &v);
    SetProp(env, result, "exitCode", v);
    napi_get_boolean(env, r.timedOut, &v);
    SetProp(env, result, "timedOut", v);
    napi_create_string_utf8(env, r.out.c_str(), r.out.size(), &v);
    SetProp(env, result, "stdout", v);
    napi_create_string_utf8(env, r.err.c_str(), r.err.size(), &v);
    SetProp(env, result, "stderr", v);
    return result;
}

napi_value Init(napi_env env, napi_value exports)
{
    napi_property_descriptor desc[] = {
        {"runBin", nullptr, RunBin, nullptr, nullptr, nullptr, napi_default, nullptr},
    };
    napi_define_properties(env, exports, sizeof(desc) / sizeof(desc[0]), desc);
    return exports;
}

} // namespace

extern "C" __attribute__((constructor)) void RegisterEntryModule()
{
    static napi_module demoModule = {
        .nm_version = 1,
        .nm_flags = 0,
        .nm_filename = nullptr,
        .nm_register_func = Init,
        .nm_modname = "entry",
        .nm_priv = nullptr,
        .reserved = {0},
    };
    napi_module_register(&demoModule);
}

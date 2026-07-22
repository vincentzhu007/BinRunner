export interface RunResult {
  exitCode: number;   // -1: 启动失败/超时被杀；>=0: 进程退出码；128+sig: 被信号杀死
  timedOut: boolean;
  stdout: string;
  stderr: string;
}

/**
 * 执行指定二进制：读取 binDir 下的 lib<name>.so，fork 子进程后优先 execv；
 * execve 被禁（零售机）时自动切换到内存 ELF loader（不经 execve，直接匿名内存映射+跳转）。
 * @param binDir     安装后的 native lib 目录，例如 context.getApplicationContext().bundleCodeDir + '/libs/arm64'
 * @param name       二进制名（不带 lib 前缀和 .so 后缀）
 * @param args       参数列表
 * @param timeoutSec 超时秒数，超时后 SIGKILL
 */
export const runBin: (binDir: string, name: string, args: string[], timeoutSec: number) => RunResult;

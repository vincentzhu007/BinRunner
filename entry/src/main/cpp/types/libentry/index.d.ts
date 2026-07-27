export interface RunResult {
  exitCode: number;   // -1: 启动失败/超时被杀；>=0: 进程退出码；128+sig: 被信号杀死
  timedOut: boolean;
  stdout: string;
  stderr: string;
}

/**
 * 执行指定二进制：优先读取 filesBinDir 下的 <name>（PushServer 免打包推送目录），
 * 其次 binDir 下的 lib<name>.so；fork 子进程后优先 execv；
 * execve 被禁（零售机）时自动切换到内存 ELF loader（不经 execve，直接匿名内存映射+跳转）。
 * @param binDir      安装后的 native lib 目录，例如 context.getApplicationContext().bundleCodeDir + '/libs/arm64'
 * @param name        二进制名：绝对路径直通；否则推送目录找原样文件名、libs 目录找 lib<name>.so
 * @param args        参数列表
 * @param timeoutSec  超时秒数，超时后 SIGKILL
 * @param filesBinDir PushServer 接收目录（filesDir + '/bin'），推送二进制及其 .so 依赖放这里，可省略
 */
export const runBin: (binDir: string, name: string, args: string[], timeoutSec: number, filesBinDir?: string) => RunResult;

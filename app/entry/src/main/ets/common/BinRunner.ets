import { runBin, RunResult } from 'libentry.so';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { fileIo } from '@kit.CoreFileKit';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';
// hilog 单条日志过长会被截断，超长行分段输出
const HILOG_CHUNK = 900;

/**
 * 逐行输出报告到 hilog，行间加微小延迟避免 socket 缓冲区溢出。
 * 不使用批量合并：hilog 内嵌 \\n 只会被拆为纯文本续行（不带 TAG/前缀），
 * CLI 侧无法解析。因此每行单独调用 hilog.info，延迟控制写入速率。
 */
async function logLines(prefix: string, text: string): Promise<void> {
  for (const line of text.split('\n')) {
    if (line.length <= HILOG_CHUNK) {
      hilog.info(DOMAIN, TAG, '%{public}s %{public}s', prefix, line);
    } else {
      const total = Math.ceil(line.length / HILOG_CHUNK);
      for (let i = 0; i < total; i++) {
        hilog.info(DOMAIN, TAG, '%{public}s [%{public}d/%{public}d] %{public}s',
          prefix, i + 1, total, line.substring(i * HILOG_CHUNK, (i + 1) * HILOG_CHUNK));
      }
    }
    // 微小延迟：限制 hilog socket 写入速率，防止缓冲区溢出
    await new Promise<void>(r => setTimeout(r, 2));
  }
}

// 路径展开：@ 展开为沙箱 files 根目录（仿 shell tilde 语义但用 @，避免 host shell 抢先展开 ~）。
// 两种词首位置生效：词首（@/x）、等号后（--opt=@/x，CLI 参数的通行惯例）。
// 按词处理而非全串替换，避免误伤参数中间的 @（如 user@host、loader 的 @fd 字面量）。
function expandHome(token: string, filesDir: string): string {
  if (token === '@') {
    return filesDir;
  }
  if (token.startsWith('@/')) {
    return filesDir + token.slice(1);
  }
  const eq = token.indexOf('=@/');
  if (eq >= 0) {
    return token.slice(0, eq + 1) + filesDir + token.slice(eq + 2);
  }
  if (token.endsWith('=@')) {
    return token.slice(0, -1) + filesDir;
  }
  return token;
}

export class BinRunner {
  /** native lib 安装目录，由 EntryAbility 初始化（bundleCodeDir + '/libs/arm64'） */
  static binDir: string = '';
  /** App 沙箱 files 目录（context.filesDir），沙箱数据根；cmd 中 @ 占位符会展开为它 */
  static filesDir: string = '';
  /** PushServer 推送目录（filesDir + '/bin'），免打包推送的二进制/依赖库放这里，名字解析优先于 binDir */
  static filesBinDir: string = '';

  /**
   * 执行一条命令，例如 run('hello foo bar') 会执行 libhello.so 并传入 ['foo', 'bar']。
   * 返回完整报告文本，同时分段写入 hilog（tag = BinRunner）。
   * @param runId 可选执行 ID，多终端并发时用于区分输出（CLI 自动生成）。
   */
  static async run(cmdLine: string, runId: string = ''): Promise<string> {
    const parts = cmdLine.split(/\s+/).filter((s) => s.length > 0)
      .map((t) => expandHome(t, BinRunner.filesDir));
    if (parts.length === 0) {
      return 'empty command';
    }
    const name = parts[0];
    const args = parts.slice(1);

    const prefix = runId.length > 0 ? `[${runId}] ` : '';
    hilog.info(DOMAIN, TAG, '%{public}s>>> exec %{public}s args=%{public}s', prefix, name, JSON.stringify(args));

    let report: string;
    // 隐藏命令：ls [path]（默认沙箱 files 根目录，推送的二进制在 bin/ 子目录），列目录到 host CLI
    if (name === 'ls') {
      report = BinRunner.listDir(args[0] ?? BinRunner.filesDir);
    } else if (name === 'rm') {
      // 隐藏命令：rm <path> — 删除 filesDir 下的文件或目录
      report = BinRunner.removePath(args[0] ?? '');
    } else {
      try {
        const r: RunResult = await runBin(BinRunner.binDir, name, args, 30, BinRunner.filesBinDir);
        report = `exit=${r.exitCode} timedOut=${r.timedOut}\n` +
          `--- stdout ---\n${r.stdout}\n--- stderr ---\n${r.stderr}`;
      } catch (e) {
        report = `native call failed: ${JSON.stringify(e)}`;
      }
    }

    await logLines(prefix + '<<<', report);
    // 结束标记：host CLI 以此判定报告输出完毕（不再需要 idle 超时猜测）
    hilog.info(DOMAIN, TAG, '%{public}s<<< END', prefix);
    return report;
  }

  /** 列目录（大小 + 修改时间 + 名称），供隐藏命令 ls 使用 */
  private static listDir(path: string): string {
    let names: string[];
    try {
      names = fileIo.listFileSync(path).sort();
    } catch (e) {
      return `exit=1\nls ${path}: ${JSON.stringify(e)}`;
    }
    const lines: string[] = [`exit=0`, `${path}:`, `total ${names.length}`];
    for (const n of names) {
      try {
        const st = fileIo.statSync(`${path}/${n}`);
        const dir = st.isDirectory();
        const mtime = new Date(Number(st.mtime) * 1000).toLocaleString();
        lines.push(`${dir ? 'd' : '-'} ${String(st.size).padStart(12)} ${mtime} ${n}${dir ? '/' : ''}`);
      } catch {
        lines.push(`? ${'?'.padStart(12)} ${'-'.repeat(19)} ${n}`);
      }
    }
    return lines.join('\n');
  }

  /** 递归删除文件或目录。相对路径默认基于 filesBinDir（推送目录），
   *  绝对路径或 @/ 前缀保持原样。空路径或试图删除根目录时拒绝。 */
  private static removePath(path: string): string {
    if (path.length === 0) {
      return `exit=1\nrm: path required (use br ls to see files)`;
    }
    // 路径解析：@/ 或 / 开头用 filesDir 展开，否则默认 filesBinDir
    let target: string;
    if (path.startsWith('@/')) {
      target = `${BinRunner.filesDir}/${path.slice(2)}`;
    } else if (path.startsWith('/')) {
      target = path;
    } else {
      target = `${BinRunner.filesBinDir}/${path}`;
    }
    // 安全检查：禁止删除 filesDir 或 filesBinDir 根目录
    if (target === BinRunner.filesDir || target === BinRunner.filesBinDir) {
      return `exit=1\nrm: refusing to delete root directory`;
    }
    try {
      if (!fileIo.accessSync(target)) {
        return `exit=1\nrm ${path}: no such file or directory`;
      }
      const st = fileIo.statSync(target);
      if (st.isDirectory()) {
        fileIo.rmdirSync(target);
      } else {
        fileIo.unlinkSync(target);
      }
      return `exit=0\nrm ${path}: removed`;
    } catch (e) {
      return `exit=1\nrm ${path}: ${JSON.stringify(e)}`;
    }
  }
}

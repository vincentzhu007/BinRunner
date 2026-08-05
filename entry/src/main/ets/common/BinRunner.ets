import { runBin, RunResult } from 'libentry.so';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { fileIo } from '@kit.CoreFileKit';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';
// hilog 单条日志过长会被截断，超长行分段输出
const HILOG_CHUNK = 900;

// 逐行输出：单条日志内不出现换行（hilog 会把内嵌换行拆成多条带前缀的行，host 端无法区分）。
// 仅当单行本身超长度上限时才按 [i/n] 分段，分段间无换行、直接拼接还原。
function logLines(prefix: string, text: string): void {
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
   * 执行一条命令，例如 run('hello foo bar') 会执行 libhello.so 并传入 ['foo', 'bar']
   * 返回完整报告文本，同时分段写入 hilog（tag = BinRunner）
   */
  static async run(cmdLine: string): Promise<string> {
    const parts = cmdLine.split(/\s+/).filter((s) => s.length > 0)
      .map((t) => expandHome(t, BinRunner.filesDir));
    if (parts.length === 0) {
      return 'empty command';
    }
    const name = parts[0];
    const args = parts.slice(1);

    hilog.info(DOMAIN, TAG, '>>> exec %{public}s args=%{public}s', name, JSON.stringify(args));

    let report: string;
    // 隐藏命令：ls [path]（默认沙箱 files 根目录，推送的二进制在 bin/ 子目录），列目录到 host CLI
    if (name === 'ls') {
      report = BinRunner.listDir(args[0] ?? BinRunner.filesDir);
    } else {
      try {
        const r: RunResult = await runBin(BinRunner.binDir, name, args, 30, BinRunner.filesBinDir);
        report = `exit=${r.exitCode} timedOut=${r.timedOut}\n` +
          `--- stdout ---\n${r.stdout}\n--- stderr ---\n${r.stderr}`;
      } catch (e) {
        report = `native call failed: ${JSON.stringify(e)}`;
      }
    }

    logLines('<<<', report);
    // 结束标记：host CLI 以此判定报告输出完毕（不再需要 idle 超时猜测）
    hilog.info(DOMAIN, TAG, '<<< END');
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
}

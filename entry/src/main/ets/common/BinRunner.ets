import { runBin, RunResult } from 'libentry.so';
import { hilog } from '@kit.PerformanceAnalysisKit';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';
// hilog 单条日志过长会被截断，分段输出
const HILOG_CHUNK = 900;

function logLong(prefix: string, text: string): void {
  if (text.length === 0) {
    hilog.info(DOMAIN, TAG, '%{public}s <empty>', prefix);
    return;
  }
  const total = Math.ceil(text.length / HILOG_CHUNK);
  for (let i = 0; i < total; i++) {
    hilog.info(DOMAIN, TAG, '%{public}s [%{public}d/%{public}d] %{public}s',
      prefix, i + 1, total, text.substring(i * HILOG_CHUNK, (i + 1) * HILOG_CHUNK));
  }
}

export class BinRunner {
  /** native lib 安装目录，由 EntryAbility 初始化（bundleCodeDir + '/libs/arm64'） */
  static binDir: string = '';
  /** App 沙箱 files 目录（context.filesDir），用于模型等数据文件；cmd 中 {files} 占位符会被替换为它 */
  static filesDir: string = '';

  /**
   * 执行一条命令，例如 run('hello foo bar') 会执行 libhello.so 并传入 ['foo', 'bar']
   * 返回完整报告文本，同时分段写入 hilog（tag = BinRunner）
   */
  static run(cmdLine: string): string {
    cmdLine = cmdLine.replace(/\{files\}/g, BinRunner.filesDir);
    const parts = cmdLine.split(/\s+/).filter((s) => s.length > 0);
    if (parts.length === 0) {
      return 'empty command';
    }
    const name = parts[0];
    const args = parts.slice(1);

    hilog.info(DOMAIN, TAG, '>>> exec lib%{public}s.so args=%{public}s', name, JSON.stringify(args));

    let report: string;
    try {
      const r: RunResult = runBin(BinRunner.binDir, name, args, 30);
      report = `exit=${r.exitCode} timedOut=${r.timedOut}\n` +
        `--- stdout ---\n${r.stdout}\n--- stderr ---\n${r.stderr}`;
    } catch (e) {
      report = `native call failed: ${JSON.stringify(e)}`;
    }

    logLong('<<<', report);
    return report;
  }
}

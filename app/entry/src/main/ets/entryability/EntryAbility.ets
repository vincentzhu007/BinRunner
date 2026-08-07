import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { fileIo } from '@kit.CoreFileKit';
import { BinRunner } from '../common/BinRunner';
import { PushServer } from '../common/PushServer';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    // 安装后 native lib 目录形如 /data/app/el1/bundle/public/<bundleName>/libs/arm64
    BinRunner.binDir = `${this.context.getApplicationContext().bundleCodeDir}/libs/arm64`;
    BinRunner.filesDir = this.context.filesDir;
    BinRunner.filesBinDir = `${this.context.filesDir}/bin`;
    hilog.info(DOMAIN, TAG, 'binDir = %{public}s', BinRunner.binDir);
    PushServer.recvDir = BinRunner.filesBinDir;
    PushServer.start();
    // 让出主线程再释放模型：同步 IO 会阻塞事件循环，期间 PushServer 的
    // message 回调停摆，正在进行的大文件推送会因接收缓冲区填满而断连。
    setTimeout(() => this.extractModel(), 0);
    this.scheduleRun(want);
  }

  // 把 rawfile 里的模型释放到 filesDir，供二进制通过 @/xxx.ms 路径读取。
  // 分块写 + 每块间让出事件循环，避免长时间占住主线程（见 onCreate 注释）。
  private extractModel(): void {
    const modelName = 'mobilenetv2.ms';
    const dst = `${this.context.filesDir}/${modelName}`;
    try {
      if (fileIo.accessSync(dst)) {
        hilog.info(DOMAIN, TAG, 'model already at %{public}s', dst);
        return;
      }
      const content = this.context.resourceManager.getRawFileContentSync(modelName);
      const fd = fileIo.openSync(dst, fileIo.OpenMode.READ_WRITE | fileIo.OpenMode.CREATE);
      const total = content.length;
      const chunk = 1048576;  // 1MiB/次，单次写盘耗时可忽略
      let off = 0;

      const writeNext = (): void => {
        try {
          const end = Math.min(off + chunk, total);
          fileIo.writeSync(fd.fd, content.buffer.slice(off, end));
          off = end;
          if (off < total) {
            setTimeout(writeNext, 0);  // 让出主线程，PushServer 回调得以插入
            return;
          }
          fileIo.closeSync(fd);
          hilog.info(DOMAIN, TAG, 'model extracted to %{public}s (%{public}d bytes)', dst, total);
        } catch (e) {
          try {
            fileIo.closeSync(fd);
          } catch (err) {
            // 已在失败路径，忽略
          }
          hilog.error(DOMAIN, TAG, 'extractModel write failed: %{public}s', JSON.stringify(e));
        }
      };
      writeNext();
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'extractModel failed: %{public}s', JSON.stringify(e));
    }
  }

  onNewWant(want: Want, launchParam: AbilityConstant.LaunchParam): void {
    // App 已在后台时，aa start 的参数通过 onNewWant 送达
    this.scheduleRun(want);
  }

  private scheduleRun(want: Want): void {
    const cmd = want?.parameters?.cmd as string | undefined;
    if (!cmd) {
      return;
    }
    const runId = (want?.parameters?.run_id as string | undefined) ?? '';
    AppStorage.setOrCreate('lastCmd', cmd);
    // 延迟执行，让页面先加载完，避免冷启动时阻塞首帧
    setTimeout(async () => {
      const output = await BinRunner.run(cmd, runId);
      AppStorage.setOrCreate('output', output);
    }, 300);
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        hilog.error(DOMAIN, TAG, 'loadContent failed: %{public}s', JSON.stringify(err));
      }
    });
  }
}

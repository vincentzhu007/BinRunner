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
    this.extractModel();
    this.scheduleRun(want);
  }

  // 把 rawfile 里的模型释放到 filesDir，供二进制通过 @/xxx.ms 路径读取
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
      fileIo.writeSync(fd.fd, content.buffer);
      fileIo.closeSync(fd);
      hilog.info(DOMAIN, TAG, 'model extracted to %{public}s (%{public}d bytes)', dst, content.length);
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
    AppStorage.setOrCreate('lastCmd', cmd);
    // 延迟执行，让页面先加载完，避免冷启动时阻塞首帧
    setTimeout(() => {
      const output = BinRunner.run(cmd);
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

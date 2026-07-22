import { hapTasks } from '@ohos/hvigor-ohos-plugin';
import { hvigor, getNode, HvigorNode } from '@ohos/hvigor';
import { execFileSync } from 'child_process';
import * as path from 'path';
import * as fs from 'fs';

// 当前工具链的 PackageHap 不支持 hnpPackages 自动打包，
// 在 PackageHap 之后、SignHap 之前把 hnp 注入 unsigned hap，再交给 hvigor 正常签名。
hvigor.nodesEvaluated(() => {
  const node: HvigorNode | undefined = getNode(__filename);
  if (!node) {
    return;
  }
  const hnpRelPath = 'hnp/arm64-v8a/hello.hnp';

  const resolveTaskName = (base: string): string => {
    return node.getTaskByName(`default@${base}`) ? `default@${base}` : base;
  };

  node.registerTask({
    name: 'InjectHnp',
    run: (taskContext) => {
      const moduleDir: string = taskContext.modulePath;
      const hap = path.join(moduleDir, 'build/default/outputs/default/entry-default-unsigned.hap');
      const hnpAbs = path.join(moduleDir, hnpRelPath);
      console.log(`InjectHnp: hap=${hap} exists=${fs.existsSync(hap)}, hnp=${hnpAbs} exists=${fs.existsSync(hnpAbs)}`);
      if (fs.existsSync(hap) && fs.existsSync(hnpAbs)) {
        execFileSync('zip', [hap, hnpRelPath], { cwd: moduleDir, stdio: 'inherit' });
      }
    },
    dependencies: [resolveTaskName('PackageHap')],
    postDependencies: [resolveTaskName('SignHap')]
  });
});

export default {
  system: hapTasks,
  plugins: []
}

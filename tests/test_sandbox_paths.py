"""执行设备端实际路径解析代码，与 CLI 的路径校验做交叉验证。需要 Node >=23。"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from binrunner.transfer import is_safe_remote_name


def test_device_paths_and_transfer_validation(tmp_path):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node >=23 required for device path helper test')
    major = int(subprocess.check_output([node, '--version'], text=True).strip()[1:].split('.')[0])
    if major < 23:
        pytest.skip('Node >=23 required for TypeScript type stripping')
    source = Path(__file__).resolve().parents[1] / 'app/entry/src/main/ets/common/SandboxPaths.ets'
    helper = tmp_path / 'paths.mts'
    helper.write_text(source.read_text())
    names = ['x', 'bin/x', '@/x', '../x', 'a/../x', 'a//x', 'a/./x', 'a/@/x', 'a\x00b', 'a\\b', 'user@host']
    script = tmp_path / 'check.mts'
    script.write_text('''import assert from 'node:assert/strict';
import {resolveSandboxPath, isSafeTransferName} from './paths.mts';
const root = '/sandbox/files';
for (const path of ['x', './x', '@/x']) assert.equal(resolveSandboxPath(path, root), root+'/x');
for (const path of ['bin/x', '@/bin/x']) assert.equal(resolveSandboxPath(path, root), root+'/bin/x');
for (const path of ['@', '@/', '.', './', '/sandbox/files/']) assert.equal(resolveSandboxPath(path, root), root);
for (const path of ['', '../x', '@/../x', 'bin/../']) assert.throws(()=>resolveSandboxPath(path, root));
console.log(JSON.stringify(''' + json.dumps(names) + '''.map(isSafeTransferName)));
''')
    result = subprocess.run([node, str(script)], text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [is_safe_remote_name(n) for n in names]

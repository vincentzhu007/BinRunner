/** 传输协议只接受相对 files/ 的规范路径；@/ 在 CLI 解析。 */
export function isSafeTransferName(name: string): boolean {
  if (name.length === 0 || name.startsWith('/') || name.includes('\\') || name.includes('\u0000')) {
    return false;
  }
  return name.split('/').every((part: string) =>
    part !== '' && part !== '.' && part !== '..' && part !== '@');
}

/** ls/rm 的相对路径统一从 files/ 开始。绝对路径保留，删除范围由调用方检查。 */
export function resolveSandboxPath(path: string, filesDir: string): string {
  if (path.length === 0 || path.includes('\\') || path.includes('\u0000')) {
    throw new Error('invalid path');
  }
  if (path === '@') {
    return filesDir;
  }
  if (path.startsWith('@/')) {
    path = filesDir + '/' + path.slice(2);
  } else if (!path.startsWith('/')) {
    path = filesDir + '/' + path;
  }
  const parts = path.split('/');
  if (parts.some((part: string) => part === '..')) {
    throw new Error('parent traversal is not allowed');
  }
  return '/' + parts.filter((part: string) => part !== '' && part !== '.').join('/');
}

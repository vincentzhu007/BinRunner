import { socket } from '@kit.NetworkKit';
import { fileIo } from '@kit.CoreFileKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { util } from '@kit.ArkTS';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';

/**
 * 推送通道：零售机上 /data/local/tmp 对 App 不可见（SELinux 隐藏），
 * hdc file send 也进不了 App 沙箱（shell uid 无权限），唯一可用的
 * 带外传输是 `hdc fport tcp:8888 tcp:8888` + TCP。
 *
 * 协议（单连接单文件，小端）：
 *   u32 nameLen | name 字节 | u64 payloadSize | payload 字节
 * 客户端发完即关连接，服务端在 close 事件里解析并写入 <recvDir>/<name>。
 * PC 端见 tools/binrunner.py push。
 */
export class PushServer {
  private static server: socket.TCPSocketServer | null = null;
  /** 接收目录（EntryAbility 初始化为 filesDir + '/bin'） */
  static recvDir: string = '';
  static readonly PORT = 8888;

  static start(): void {
    if (PushServer.server) {
      return;
    }
    try {
      if (!fileIo.accessSync(PushServer.recvDir)) {
        fileIo.mkdirSync(PushServer.recvDir, true);
      }
    } catch (e) {
      hilog.error(DOMAIN, TAG, 'mkdir %{public}s failed: %{public}s', PushServer.recvDir, JSON.stringify(e));
      return;
    }

    const srv = socket.constructTCPSocketServerInstance();
    srv.on('connect', (client: socket.TCPSocketConnection) => {
      PushServer.handleClient(client);
    });
    srv.listen({ address: '0.0.0.0', port: PushServer.PORT, family: 1 })
      .then(() => {
        PushServer.server = srv;
        hilog.info(DOMAIN, TAG, 'push server listening on :%{public}d -> %{public}s',
          PushServer.PORT, PushServer.recvDir);
      })
      .catch((e: Error) => {
        hilog.error(DOMAIN, TAG, 'listen failed: %{public}s', JSON.stringify(e));
      });
  }

  private static handleClient(client: socket.TCPSocketConnection): void {
    // 小文件直接整体攒在内存（benchmark + 依赖库也就 MB 级）
    const chunks: ArrayBuffer[] = [];
    let total = 0;
    let remote = 'client';

    client.getRemoteAddress()
      .then((addr) => { remote = addr.address; })
      .catch(() => {});

    client.on('message', (info: socket.SocketMessageInfo) => {
      chunks.push(info.message);
      total += info.message.byteLength;
    });

    client.on('close', () => {
      const err = PushServer.store(chunks, total);
      hilog.info(DOMAIN, TAG, 'push %{public}s: %{public}s', remote,
        err.length > 0 ? `FAIL ${err}` : `OK (${total} bytes)`);
    });

    client.on('error', (e: Error) => {
      hilog.error(DOMAIN, TAG, 'push %{public}s error: %{public}s', remote, JSON.stringify(e));
    });
  }

  /** 解析协议并落盘，返回空串表示成功，否则为错误描述 */
  private static store(chunks: ArrayBuffer[], total: number): string {
    if (total < 4) {
      return 'too short';
    }
    const buf = new Uint8Array(total);
    let off = 0;
    for (const c of chunks) {
      buf.set(new Uint8Array(c), off);
      off += c.byteLength;
    }
    const view = new DataView(buf.buffer);
    const nameLen = view.getUint32(0, true);
    if (nameLen === 0 || nameLen > 256 || total < 4 + nameLen + 8) {
      return `bad nameLen ${nameLen}`;
    }
    const name = util.TextDecoder.create().decode(buf.subarray(4, 4 + nameLen));
    // 防目录穿越：禁止绝对路径、上级引用、空名，允许子目录（如 subdir/file.so）
    if (name.length === 0 || name.startsWith('/') || name.includes('\\') ||
        name === '.' || name === '..' || name.includes('/../') || name.endsWith('/..')) {
      return `bad name ${name}`;
    }
    // 确保父目录存在
    const lastSlash = name.lastIndexOf('/');
    if (lastSlash >= 0) {
      const parentDir = `${PushServer.recvDir}/${name.substring(0, lastSlash)}`;
      try {
        if (!fileIo.accessSync(parentDir)) {
          fileIo.mkdirSync(parentDir, true);
        }
      } catch (e) {
        return `mkdir ${parentDir}: ${JSON.stringify(e)}`;
      }
    }
    // Number 精度足够（< 2^53）
    const size = view.getUint32(4 + nameLen, true) + view.getUint32(4 + nameLen + 4, true) * 0x100000000;
    const payloadOff = 4 + nameLen + 8;
    if (total !== payloadOff + size) {
      return `size mismatch header=${size} actual=${total - payloadOff}`;
    }
    const dst = `${PushServer.recvDir}/${name}`;
    try {
      const fd = fileIo.openSync(dst, fileIo.OpenMode.READ_WRITE | fileIo.OpenMode.CREATE | fileIo.OpenMode.TRUNC);
      fileIo.writeSync(fd.fd, buf.buffer.slice(payloadOff));
      fileIo.closeSync(fd);
      hilog.info(DOMAIN, TAG, 'saved %{public}s (%{public}d bytes)', dst, size);
      return '';
    } catch (e) {
      return JSON.stringify(e);
    }
  }
}

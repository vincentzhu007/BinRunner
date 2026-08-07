import { socket } from '@kit.NetworkKit';
import { fileIo } from '@kit.CoreFileKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { util } from '@kit.ArkTS';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';

/** PULL 协议魔数（小端），与 CLI 侧 config.PULL_MAGIC 一致 */
const PULL_MAGIC = 0x4c4c5550;
/** PULL 回传分块大小 */
const PULL_CHUNK = 262144;
/** 单文件上限 1GiB，与 CLI 侧 config.MAX_FILE_SIZE 保持一致 */
const MAX_FILE_SIZE = 1073741824;
/** 远端名上限（字节），与 CLI 侧 MAX_REMOTE_NAME_BYTES 一致 */
const MAX_NAME_BYTES = 256;
/** 协议头最小长度：u32 nameLen + u64 payloadSize */
const MIN_HEADER_BYTES = 12;
/**
 * 每落盘满这么多字节回一个 ACK，与 CLI 侧 ACK_INTERVAL 一致。
 *
 * ACK 是流控的核心：ArkTS 单线程，主线程一旦被同步操作阻塞，
 * message 回调就停摆；客户端若无视这点全速发送，内核接收缓冲区
 * 填满后连接会被重置（实测 64MB 在 33MB 处 Broken pipe）。
 * 客户端凭 ACK 限制在途字节数，阻塞期间自然停下等待。
 */
const ACK_INTERVAL = 4194304;

/**
 * 续传协议魔数，与 CLI 侧 config.RESUME_MAGIC 一致。
 *
 * 旧协议头首字段是 nameLen（1..256）；新协议以此魔数开头，
 * 故设备可只看首 4 字节就判断对端协议版本，双向兼容。
 */
const RESUME_MAGIC = 0x42524E31;

/** 头部 flags 位：客户端愿意从已有偏移继续 */
const FLAG_RESUME = 1;

/** 续传校验的探针字节数，与 CLI 侧 RESUME_PROBE_BYTES 一致 */
const RESUME_PROBE_BYTES = 65536;

/** 新协议头最小长度：magic + flags + nameLen + u64 size */
const MIN_HEADER_V2_BYTES = 20;

/**
 * 接收状态机。
 *
 * 流式落盘的关键：header 到齐即打开文件，后续 message 事件直接写盘，
 * 不在内存里累积 payload —— 否则 1GB 文件会 OOM。
 */
enum RecvState {
  HEADER,   // 头部未收齐，继续在内存里攒
  BODY,     // 头部已解析，文件已打开，边收边写
  DONE,     // payload 已收满
  FAILED,   // 出错，忽略后续数据
}

/**
 * 推送通道：零售机上 /data/local/tmp 对 App 不可见（SELinux 隐藏），
 * hdc file send 也进不了 App 沙箱（shell uid 无权限），唯一可用的
 * 带外传输是 `hdc fport tcp:8888 tcp:8888` + TCP。
 *
 * 协议（单连接单文件，小端）。首 4 字节区分版本：
 *
 * v1（旧，首字段是 nameLen 1..256）：
 *   PC → 设备:  u32 nameLen | name | u64 payloadSize | payload
 *
 * v2（续传，首字段是 RESUME_MAGIC）：
 *   PC → 设备:  u32 magic | u32 flags | u32 nameLen | name | u64 size
 *               [flags & FLAG_RESUME 时附 u32 probeLen | probe 字节]
 *   设备 → PC:  u64 resumeFrom（已有字节数，0 = 从头）
 *   PC → 设备:  payload 自 resumeFrom 起的剩余部分
 *
 * 两版都以 u64 累计 ACK 做流控（每 ACK_INTERVAL 字节一个）。
 * payload 流式写盘，支持至 1GiB 的文件。PC 端见 binrunner/push.py。
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

  /**
   * 校验远端名，拒绝目录穿越。允许子路径（如 models/net.ms）。
   * 规则必须与 CLI 侧 is_safe_remote_name 一致 —— 任一端放行都会造成沙箱逃逸。
   */
  static isSafeName(name: string): boolean {
    if (name.length === 0) {
      return false;
    }
    if (name.startsWith('/') || name.includes('\\')) {
      return false;
    }
    if (name === '.' || name === '..') {
      return false;
    }
    if (name.includes('/../') || name.endsWith('/..')) {
      return false;
    }
    return true;
  }

  /** 确保父目录存在。返回空串表示成功，否则为错误描述。 */
  private static ensureParentDir(name: string): string {
    const lastSlash = name.lastIndexOf('/');
    if (lastSlash < 0) {
      return '';
    }
    const parentDir = `${PushServer.recvDir}/${name.substring(0, lastSlash)}`;
    try {
      if (!fileIo.accessSync(parentDir)) {
        fileIo.mkdirSync(parentDir, true);
      }
      return '';
    } catch (e) {
      return `mkdir ${parentDir}: ${JSON.stringify(e)}`;
    }
  }

  /**
   * 半成品文件路径。传输期间数据都写在这里，收满才 rename 为正式名 ——
   * 这样正式名下永远不会出现不完整的文件（否则可能被当作二进制执行）。
   */
  static partPath(name: string): string {
    return `${PushServer.recvDir}/${name}.part`;
  }

  static finalPath(name: string): string {
    return `${PushServer.recvDir}/${name}`;
  }

  /**
   * 判断已有的 .part 能否用于续传。
   *
   * 校验两件事：已有长度不超过声明大小；文件头部与客户端探针一致。
   * 只比头部而非全文 —— 设备侧读全文会长时间阻塞主线程，而头部
   * 足以挡住"同名不同内容"（重新编译的二进制头部必变）。
   *
   * 返回可续传的字节数，0 表示需从头开始。
   */
  private static probeResumable(name: string, declaredSize: number,
                                probe: Uint8Array): number {
    const path = PushServer.partPath(name);
    let existing = 0;
    try {
      if (!fileIo.accessSync(path)) {
        return 0;
      }
      existing = fileIo.statSync(path).size;
    } catch (e) {
      return 0;
    }
    if (existing === 0 || existing >= declaredSize) {
      // 空文件无意义；已达或超过声明大小说明是上次的残留或换了内容，重来
      return 0;
    }
    if (probe.length === 0) {
      return existing;  // 客户端未给探针，信任长度
    }
    // 比对头部：读出与探针等长的前缀逐字节比
    const want = Math.min(probe.length, existing);
    try {
      const fd = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY);
      const buf = new ArrayBuffer(want);
      const got = fileIo.readSync(fd.fd, buf, { offset: 0, length: want });
      fileIo.closeSync(fd);
      if (got !== want) {
        return 0;
      }
      const head = new Uint8Array(buf);
      for (let i = 0; i < want; i++) {
        if (head[i] !== probe[i]) {
          hilog.warn(DOMAIN, TAG, 'resume probe mismatch at %{public}d for %{public}s', i, name);
          return 0;  // 内容变了，不能续
        }
      }
      return existing;
    } catch (e) {
      return 0;
    }
  }

  private static handleClient(client: socket.TCPSocketConnection): void {
    let state: RecvState = RecvState.HEADER;
    // 仅在 HEADER 阶段用于攒头部字节，进入 BODY 后即释放
    let headerChunks: ArrayBuffer[] = [];
    let headerTotal = 0;

    let fd: fileIo.File | null = null;
    let name = '';
    let expectedSize = 0;
    let written = 0;
    let ackedAt = 0;   // 上次回 ACK 时的 written 值
    let failReason = '';
    let remote = 'client';
    // 续传：本次从该偏移开始接收，写盘位置为 resumeFrom + 已收字节
    let resumeFrom = 0;

    client.getRemoteAddress()
      .then((addr: socket.NetAddress) => { remote = addr.address; })
      .catch(() => {});

    const fail = (reason: string): void => {
      state = RecvState.FAILED;
      failReason = reason;
      if (fd !== null) {
        try {
          fileIo.closeSync(fd);
        } catch (e) {
          // 关闭失败无关键影响，已在失败路径上
        }
        fd = null;
      }
      // 半成品保留为 .part，供下次续传；正式名不出现不完整文件，
      // 避免被误当作可执行二进制加载。
      if (name.length > 0 && written + resumeFrom > 0) {
        hilog.info(DOMAIN, TAG, 'push %{public}s: kept partial %{public}d bytes for resume',
          name, resumeFrom + written);
      } else if (name.length > 0) {
        // 一个字节都没落盘，删掉空壳
        try {
          fileIo.unlinkSync(PushServer.partPath(name));
        } catch (e) {
          // 文件可能尚未创建，忽略
        }
      }
    };

    /** 发送 u64 小端值。ACK 与续传偏移共用同一编码。 */
    const sendU64 = (value: number, what: string): void => {
      const buf = new ArrayBuffer(8);
      const view = new DataView(buf);
      view.setUint32(0, value % 4294967296, true);
      view.setUint32(4, Math.floor(value / 4294967296), true);
      client.send({ data: buf })
        .catch((e: Error) => {
          hilog.warn(DOMAIN, TAG, '%{public}s send failed at %{public}d: %{public}s',
            what, value, JSON.stringify(e));
        });
    };

    /** 告知客户端可从哪个偏移继续（v2 协议，头部解析后立即发一次）。 */
    const sendResumeOffset = (): void => {
      sendU64(resumeFrom, 'resume-offset');
    };

    /**
     * payload 收满：关文件、rename 为正式名、回收尾 ACK。
     *
     * rename 是原子的，故正式名下要么不存在、要么是完整文件 ——
     * 中断的传输只会留下 .part，不会被误当作可执行二进制。
     */
    const finishFile = (): void => {
      state = RecvState.DONE;
      if (fd !== null) {
        try {
          fileIo.closeSync(fd);
        } catch (e) {
          fail(`close ${name}: ${JSON.stringify(e)}`);
          return;
        }
        fd = null;
      }
      try {
        const target = PushServer.finalPath(name);
        if (fileIo.accessSync(target)) {
          fileIo.unlinkSync(target);  // rename 不覆盖已存在的目标
        }
        fileIo.renameSync(PushServer.partPath(name), target);
      } catch (e) {
        fail(`rename ${name}: ${JSON.stringify(e)}`);
        return;
      }
      sendU64(expectedSize, 'ack');  // 收尾 ACK 用总量，与客户端预期一致
    };

    /**
     * 头部收齐后解析并打开目标文件。返回 payload 起始偏移，-1 表示失败/未齐。
     *
     * 首 4 字节是 RESUME_MAGIC 则走 v2（支持续传），否则按 v1 解析 ——
     * v1 首字段是 nameLen（1..256），与魔数值域不重叠。
     */
    const parseHeader = (buf: Uint8Array): number => {
      const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
      const first = view.getUint32(0, true);
      const isV2 = first === RESUME_MAGIC;

      let off = 0;
      let flags = 0;
      if (isV2) {
        if (buf.length < MIN_HEADER_V2_BYTES) {
          return -1;  // 继续等
        }
        flags = view.getUint32(4, true);
        off = 8;
      }

      const nameLen = view.getUint32(off, true);
      if (nameLen === 0 || nameLen > MAX_NAME_BYTES) {
        fail(`bad nameLen ${nameLen}`);
        return -1;
      }
      off += 4;
      if (buf.length < off + nameLen + 8) {
        return -1;  // 头部仍未收齐，继续等
      }

      const parsed = util.TextDecoder.create().decodeToString(buf.subarray(off, off + nameLen));
      if (!PushServer.isSafeName(parsed)) {
        fail(`bad name ${parsed}`);
        return -1;
      }
      off += nameLen;

      // Number 精度足够（< 2^53）
      const size = view.getUint32(off, true)
        + view.getUint32(off + 4, true) * 4294967296;
      if (size > MAX_FILE_SIZE) {
        fail(`file too large: ${size} > ${MAX_FILE_SIZE}`);
        return -1;
      }
      off += 8;

      // v2 且请求续传：读探针，据此决定可续偏移
      let probe = new Uint8Array(0);
      const wantResume = isV2 && (flags & FLAG_RESUME) !== 0;
      if (wantResume) {
        if (buf.length < off + 4) {
          return -1;
        }
        const probeLen = view.getUint32(off, true);
        if (probeLen > RESUME_PROBE_BYTES) {
          fail(`probe too large: ${probeLen}`);
          return -1;
        }
        off += 4;
        if (buf.length < off + probeLen) {
          return -1;  // 探针未收齐
        }
        probe = buf.subarray(off, off + probeLen);
        off += probeLen;
      }

      const mkdirErr = PushServer.ensureParentDir(parsed);
      if (mkdirErr.length > 0) {
        fail(mkdirErr);
        return -1;
      }

      // 先确定续传偏移，再决定以何种模式打开
      name = parsed;
      resumeFrom = wantResume ? PushServer.probeResumable(parsed, size, probe) : 0;

      const partFile = PushServer.partPath(parsed);
      try {
        if (resumeFrom > 0) {
          // 追加模式：保留已有前缀，从末尾继续写
          fd = fileIo.openSync(partFile,
            fileIo.OpenMode.READ_WRITE | fileIo.OpenMode.APPEND);
        } else {
          fd = fileIo.openSync(partFile,
            fileIo.OpenMode.READ_WRITE | fileIo.OpenMode.CREATE | fileIo.OpenMode.TRUNC);
        }
      } catch (e) {
        fail(`open ${partFile}: ${JSON.stringify(e)}`);
        return -1;
      }

      expectedSize = size;
      state = RecvState.BODY;

      if (isV2) {
        // 告知客户端从哪继续（v1 客户端不读这个，故只对 v2 发）
        sendResumeOffset();
        if (resumeFrom > 0) {
          hilog.info(DOMAIN, TAG, 'push %{public}s: resuming from %{public}d/%{public}d',
            parsed, resumeFrom, size);
        }
      }

      // 已收满（续传时剩余为 0）需立即收尾
      if (resumeFrom === expectedSize) {
        finishFile();
      }
      return off;
    };

    /**
     * 写入 payload 片段，超出声明大小的部分丢弃并报错。
     *
     * written 只计本次连接收到的字节；落盘总量是 resumeFrom + written，
     * ACK 与完成判定都用总量 —— 客户端视角始终是整个文件的进度。
     */
    const writeBody = (data: Uint8Array): void => {
      if (data.length === 0 || fd === null) {
        return;
      }
      const total = resumeFrom + written;
      const remaining = expectedSize - total;
      if (data.length > remaining) {
        fail(`size overflow: got more than declared ${expectedSize}`);
        return;
      }
      try {
        // buffer 需按 byteOffset/length 切片：Uint8Array 可能是大 buffer 的视图
        fileIo.writeSync(fd.fd,
          data.buffer.slice(data.byteOffset, data.byteOffset + data.length));
        written += data.length;
      } catch (e) {
        fail(`write ${name}: ${JSON.stringify(e)}`);
        return;
      }
      if (resumeFrom + written === expectedSize) {
        finishFile();
        return;
      }
      // 每满一个区间回一次，而非每包都回：减少小包往返开销
      if (written - ackedAt >= ACK_INTERVAL) {
        ackedAt = written;
        sendU64(resumeFrom + written, 'ack');
      }
    };

    client.on('message', (info: socket.SocketMessageInfo) => {
      if (state === RecvState.FAILED) {
        return;
      }
      const incoming = new Uint8Array(info.message);

      if (state === RecvState.HEADER) {
        headerChunks.push(info.message);
        headerTotal += info.message.byteLength;
        if (headerTotal < MIN_HEADER_BYTES) {
          return;  // 连 nameLen + size 都不够，继续等
        }
        // 合并已攒的头部字节
        const _merged = new Uint8Array(headerTotal);
        let _off = 0;
        for (const c of headerChunks) {
          _merged.set(new Uint8Array(c), _off);
          _off += c.byteLength;
        }
        // PULL 分流：首 4 字节匹配 PULL_MAGIC → 走 pull 处理器
        const _view = new DataView(_merged.buffer, _merged.byteOffset, _merged.byteLength);
        if (_view.getUint32(0, true) === PULL_MAGIC) {
          headerChunks = [];
          headerTotal = 0;
          handlePull(client, _merged, remote);
          return;
        }

        const payloadOff = parseHeader(_merged);
        if (payloadOff < 0) {
          return;  // 失败或头部未齐
        }
        headerChunks = [];  // 释放头部缓存，后续走流式
        headerTotal = 0;
        // 与头部同包到达的 payload 部分立即写入
        writeBody(_merged.subarray(payloadOff));
        return;
      }

      if (state === RecvState.BODY) {
        writeBody(incoming);
        return;
      }

      // DONE 之后仍有数据 = 客户端发多了
      fail(`unexpected data after ${expectedSize} bytes`);
    });

    client.on('close', () => {
      if (state === RecvState.DONE) {
        hilog.info(DOMAIN, TAG, 'push %{public}s: OK %{public}s (%{public}d bytes)',
          remote, name, expectedSize);
        return;
      }
      if (state === RecvState.FAILED) {
        hilog.error(DOMAIN, TAG, 'push %{public}s: FAIL %{public}s', remote, failReason);
        return;
      }
      // 连接提前断开：payload 不完整。.part 保留，下次可续
      const detail = state === RecvState.HEADER
        ? `incomplete header (${headerTotal} bytes)`
        : `truncated: ${resumeFrom + written}/${expectedSize} bytes`;
      fail(detail);
      hilog.error(DOMAIN, TAG, 'push %{public}s: FAIL %{public}s', remote, detail);
    });

    client.on('error', (e: Error) => {
      hilog.error(DOMAIN, TAG, 'push %{public}s error: %{public}s', remote, JSON.stringify(e));
      if (state !== RecvState.DONE) {
        fail(`socket error: ${JSON.stringify(e)}`);
      }
    });
  }
}

/**
 * PULL 协议处理器：从设备读取文件并回传给 PC。
 *
 * 请求格式（PC → 设备）：
 *   u32 PULL_MAGIC | u32 nameLen | name | u64 offset | u64 size
 *
 * 响应格式（设备 → PC）：
 *   u64 totalSize（0 = 错误/不存在）| 文件内容分块流
 *
 * 安全规则与 push 一致：拒绝目录穿越、绝对路径、上级引用。
 */
function handlePull(
  client: socket.TCPSocketConnection,
  headerBuf: Uint8Array,
  remote: string
): void {
  const view = new DataView(headerBuf.buffer, headerBuf.byteOffset, headerBuf.byteLength);
  let off = 4;

  if (headerBuf.length < off + 4) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: header too short', remote);
    return;
  }
  const nameLen = view.getUint32(off, true);
  if (nameLen === 0 || nameLen > MAX_NAME_BYTES) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: bad nameLen %{public}d', remote, nameLen);
    return;
  }
  off += 4;
  if (headerBuf.length < off + nameLen + 16) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: header incomplete', remote);
    return;
  }

  const name = util.TextDecoder.create().decodeToString(
    headerBuf.subarray(off, off + nameLen)
  );
  if (!PushServer.isSafeName(name)) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: bad name %{public}s', remote, name);
    return;
  }
  off += nameLen;

  const offset = view.getUint32(off, true)
    + view.getUint32(off + 4, true) * 4294967296;
  off += 8;
  const reqSize = view.getUint32(off, true)
    + view.getUint32(off + 4, true) * 4294967296;

  const path = PushServer.recvDir + '/' + name;
  let fd: fileIo.File | null = null;
  try {
    if (!fileIo.accessSync(path)) {
      hilog.warn(DOMAIN, TAG, 'pull %{public}s: not found %{public}s', remote, name);
      const errBuf = new ArrayBuffer(8);
      new DataView(errBuf).setUint32(4, 0, true);
      client.send({ data: errBuf }).catch(() => {});
      return;
    }

    fd = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY);
    const totalSize = fileIo.statSync(path).size;
    let toRead = reqSize > 0 ? Math.min(reqSize, totalSize - offset) : totalSize - offset;
    if (toRead <= 0 || offset >= totalSize) {
      toRead = 0;
    }

    // 发送头部：u64 totalSize
    const headBuf = new ArrayBuffer(8);
    const headView = new DataView(headBuf);
    headView.setUint32(0, toRead % 4294967296, true);
    headView.setUint32(4, Math.floor(toRead / 4294967296), true);
    client.send({ data: headBuf }).catch(() => {});

    if (toRead === 0) {
      fileIo.closeSync(fd);
      hilog.info(DOMAIN, TAG, 'pull %{public}s: OK (empty)', remote, name);
      return;
    }

    // 分块读取并发送
    let readPos = offset;
    let remaining = toRead;

    while (remaining > 0) {
      const chunkSize = Math.min(PULL_CHUNK, remaining);
      const readBuf = new ArrayBuffer(chunkSize);
      const got = fileIo.readSync(fd.fd, readBuf, { offset: readPos, length: chunkSize });
      if (got <= 0) {
        break;
      }
      const sendBuf = got === chunkSize ? readBuf : readBuf.slice(0, got);
      client.send({ data: sendBuf }).catch(() => {});
      readPos += got;
      remaining -= got;
    }

    fileIo.closeSync(fd);
    fd = null;
    hilog.info(DOMAIN, TAG, 'pull %{public}s: OK %{public}s (%{public}d bytes)',
      remote, name, toRead);
  } catch (e) {
    if (fd !== null) {
      try {
        fileIo.closeSync(fd);
      } catch (err) {
        /* 已在失败路径 */
      }
    }
    hilog.error(DOMAIN, TAG, 'pull %{public}s: FAIL %{public}s: %{public}s',
      remote, name, JSON.stringify(e));
  }
}

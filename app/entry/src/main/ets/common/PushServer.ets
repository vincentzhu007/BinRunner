import { isSafeTransferName } from './SandboxPaths';
import { socket } from '@kit.NetworkKit';
import { fileIo } from '@kit.CoreFileKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { util } from '@kit.ArkTS';
import {
  PORT,
  PULL_MAGIC,
  PULL_MAGIC_V2,
  TRANSFER_CHUNK_SIZE,
  MAX_FILE_SIZE,
  ACK_INTERVAL,
  RESUME_MAGIC,
  FLAG_RESUME,
  RESUME_PROBE_BYTES,
  MIN_HEADER_BYTES,
  MIN_HEADER_V2_BYTES,
  MAX_REMOTE_NAME_BYTES
} from './Protocol';

const DOMAIN = 0x0001;
const TAG = 'BinRunner';

/** 活跃连接上限：CLI 重试可能留下未关闭连接，超限拒绝新连接 */
const MAX_ACTIVE_CONNECTIONS = 4;

/**
 * ACK 流控（ACK_INTERVAL 定义在 Protocol.ets）。
 *
 * ACK 是流控的核心：ArkTS 单线程，主线程一旦被同步操作阻塞，
 * message 回调就停摆；客户端若无视这点全速发送，内核接收缓冲区
 * 填满后连接会被重置（实测 64MB 在 33MB 处 Broken pipe）。
 * 客户端凭 ACK 限制在途字节数，阻塞期间自然停下等待。
 *
 * pull 方向同理但角色相反：设备是发送方，必须 await 每个 send 让出
 * 事件循环并借 socket promise 做背压，否则同步循环会把整文件塞进
 * 发送队列，同样是上面那种爆缓冲。
 */

/**
 * 接收状态机。
 *
 * 流式落盘的关键：header 到齐即打开文件，后续 message 事件直接写盘，
 * 不在内存里累积 payload —— 否则数 GiB 文件会 OOM。
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
 * payload 流式写盘，支持至 4GiB 的文件。PC 端见 binrunner/push.py。
 */
export class PushServer {
  private static server: socket.TCPSocketServer | null = null;
  /** 活跃连接数（防泄漏上限 MAX_ACTIVE_CONNECTIONS） */
  private static activeConnections = 0;
  /** 接收目录（EntryAbility 初始化为 filesDir） */
  static recvDir: string = '';

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
    srv.listen({ address: '0.0.0.0', port: PORT, family: 1 })
      .then(() => {
        PushServer.server = srv;
        hilog.info(DOMAIN, TAG, 'push server listening on :%{public}d -> %{public}s',
          PORT, PushServer.recvDir);
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
    return isSafeTransferName(name);
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
    // 连接防泄漏：CLI 重试会建新连接，若旧连接因异常未关闭而累积，
    // 单线程事件循环会被拖垮。超限时直接拒绝新连接（CLI 侧会重试）。
    if (PushServer.activeConnections >= MAX_ACTIVE_CONNECTIONS) {
      hilog.warn(DOMAIN, TAG, 'push: too many active connections (%{public}d), rejecting',
        PushServer.activeConnections);
      try {
        client.close();
      } catch (e) {
        hilog.warn(DOMAIN, TAG, 'push: close rejected client failed: %{public}s', JSON.stringify(e));
      }
      return;
    }
    PushServer.activeConnections++;
    let state: RecvState = RecvState.HEADER;
    // pull 分流状态：0 = 未判定；1/2 = 已判定为 PULL v1/v2。
    // 非 0 后本连接不再走 push 状态机（close/error 也不按 push 误报）。
    let pullMode = 0;
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
      client.send({ data: encodeU64(value) })
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
      if (nameLen === 0 || nameLen > MAX_REMOTE_NAME_BYTES) {
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
      const size = readU64(view, off);
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
        if (pullMode !== 0) {
          return;  // pull 期间客户端不会继续发；多余数据一律忽略
        }
        headerChunks.push(info.message);
        headerTotal += info.message.byteLength;
        if (headerTotal < 4) {
          return;  // 连请求类型都判不出，继续等
        }
        // 合并已攒字节。头部很小（v2 最多 256 字节名 + 64KiB 探针），合并不成问题
        const merged = new Uint8Array(headerTotal);
        let mergedOff = 0;
        for (const c of headerChunks) {
          merged.set(new Uint8Array(c), mergedOff);
          mergedOff += c.byteLength;
        }
        // 首 4 字节分流：PULL / PUL2 走 pull 处理器，其余按 push 解析。
        // TCP 不保证一次 message 一个 sendall，pull 头可能被拆包 —— 必须
        // 攒到 pullHeaderReady 判定收齐才交给 handlePull，否则字段不全会
        // 被误判成非法请求；聚合是必要的，因为 pull 头需按 nameLen/probeLen
        // 计算总长，缺一个字节都算不出边界。
        const first = new DataView(merged.buffer, merged.byteOffset, merged.byteLength)
          .getUint32(0, true);
        if (first === PULL_MAGIC || first === PULL_MAGIC_V2) {
          const mode = first === PULL_MAGIC_V2 ? 2 : 1;
          if (!pullHeaderReady(merged, mode)) {
            return;  // 请求头未收齐，继续攒
          }
          pullMode = mode;
          headerChunks = [];
          headerTotal = 0;
          handlePull(client, merged, remote, mode)
            .catch((e: Error) => {
              hilog.error(DOMAIN, TAG, 'pull %{public}s: handler failed: %{public}s',
                remote, JSON.stringify(e));
            });
          return;
        }

        if (headerTotal < MIN_HEADER_BYTES) {
          return;  // 连 nameLen + size 都不够，继续等
        }
        const payloadOff = parseHeader(merged);
        if (payloadOff < 0) {
          return;  // 失败或头部未齐
        }
        headerChunks = [];  // 释放头部缓存，后续走流式
        headerTotal = 0;
        // 与头部同包到达的 payload 部分立即写入
        writeBody(merged.subarray(payloadOff));
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
      PushServer.activeConnections = Math.max(0, PushServer.activeConnections - 1);
      if (state === RecvState.DONE) {
        hilog.info(DOMAIN, TAG, 'push %{public}s: OK %{public}s (%{public}d bytes)',
          remote, name, expectedSize);
        return;
      }
      if (state === RecvState.FAILED) {
        hilog.error(DOMAIN, TAG, 'push %{public}s: FAIL %{public}s', remote, failReason);
        return;
      }
      // pull 的结果由 handlePull 自己记录。此处若继续按 push 状态机
      // 报 "incomplete header"，会把一次成功的 pull 误报成失败的 push。
      if (pullMode !== 0) {
        return;
      }
      // 连上就断、一个字节都没发：这是 CLI 的端口健康探测（hdc.port_open 建连即关），
      // 不是失败的传输。若按失败记录，每次 br push/pull 前都会留下一条假的
      // "FAIL incomplete header (0 bytes)"，把真正的失败淹没在噪声里。
      if (state === RecvState.HEADER && headerTotal === 0) {
        hilog.info(DOMAIN, TAG, 'push %{public}s: 空连接（端口探测），忽略', remote);
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
      if (pullMode !== 0) {
        // pull 的失败由 handlePull 记录，不套 push 状态机
        hilog.warn(DOMAIN, TAG, 'pull %{public}s: socket error: %{public}s', remote, JSON.stringify(e));
        return;
      }
      hilog.error(DOMAIN, TAG, 'push %{public}s error: %{public}s', remote, JSON.stringify(e));
      if (state !== RecvState.DONE) {
        fail(`socket error: ${JSON.stringify(e)}`);
      }
    });
  }
}

/** 读 u64 小端。Number 精度足够：协议里的值都 < 2^53。 */
function readU64(view: DataView, off: number): number {
  return view.getUint32(off, true) + view.getUint32(off + 4, true) * 4294967296;
}

/** 写 u64 小端。 */
function writeU64(view: DataView, off: number, value: number): void {
  view.setUint32(off, value % 4294967296, true);
  view.setUint32(off + 4, Math.floor(value / 4294967296), true);
}

/** 编码单个 u64 小端。ACK、续传偏移、PULL 响应头共用同一编码。 */
function encodeU64(value: number): ArrayBuffer {
  const buf = new ArrayBuffer(8);
  writeU64(new DataView(buf), 0, value);
  return buf;
}

/** 编码两个连续 u64（PULL v2 响应头：resumeAck | totalSize）。 */
function encodeU64Pair(a: number, b: number): ArrayBuffer {
  const buf = new ArrayBuffer(16);
  const view = new DataView(buf);
  writeU64(view, 0, a);
  writeU64(view, 8, b);
  return buf;
}

/**
 * PULL 请求头是否已收齐（或已足以判定非法）。
 *
 * 返回 false 表示还得继续攒。TCP 不保证一次 message 一个 sendall，
 * 头部被拆包时若提前交给 handlePull，缺字段会被误判成「文件不存在」。
 * nameLen/probeLen 越界属于「已判定非法」：返回 true 让 handlePull 统一
 * 发零响应 —— 不能在这里静默丢弃，否则 CLI 会阻塞到 120s 超时。
 *
 * 请求布局：
 *   v1: u32 PULL_MAGIC    | u32 nameLen | name | u64 offset | u64 size
 *   v2: u32 PULL_MAGIC_V2 | u32 nameLen | name | u64 offset | u64 size | u32 probeLen | probe
 */
function pullHeaderReady(buf: Uint8Array, mode: number): boolean {
  if (buf.length < 8) {
    return false;  // nameLen 尚未到齐
  }
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength);
  const nameLen = view.getUint32(4, true);
  if (nameLen === 0 || nameLen > MAX_REMOTE_NAME_BYTES) {
    return true;  // 非法，交给 handlePull 发零响应
  }
  // v2 在 size 之后多一个 u32 probeLen
  const headerEnd = (mode === 2 ? 28 : 24) + nameLen;
  if (buf.length < headerEnd) {
    return false;
  }
  if (mode !== 2) {
    return true;
  }
  const probeLen = view.getUint32(headerEnd - 4, true);
  if (probeLen > RESUME_PROBE_BYTES) {
    return true;  // 非法，交给 handlePull 发零响应
  }
  return buf.length >= headerEnd + probeLen;
}

/**
 * v2 续传：比对源文件头部与客户端探针，决定回传起始偏移（resumeAck）。
 *
 * 返回 0 = 客户端从头重传；offset = 探针匹配，可从该处续；
 * totalSize = 客户端 .part 已等于整个文件，无需回传数据，只需收尾。
 * 只读文件头而非全文：源文件可能数 GiB，全读同样是主线程阻塞源。
 */
function pullResumeAck(fd: fileIo.File, totalSize: number, offset: number,
                       probe: Uint8Array): number {
  if (offset === totalSize) {
    return totalSize;  // .part 已完整
  }
  if (offset <= 0 || offset > totalSize || probe.length === 0) {
    return 0;  // 未声明续传 / 远端变小 / 无探针，一律从头
  }
  const want = Math.min(probe.length, totalSize);
  const headBuf = new ArrayBuffer(want);
  const got = fileIo.readSync(fd.fd, headBuf, { offset: 0, length: want });
  if (got !== want) {
    return 0;
  }
  const head = new Uint8Array(headBuf);
  for (let i = 0; i < want; i++) {
    if (head[i] !== probe[i]) {
      return 0;  // 内容变了，不能续
    }
  }
  return offset;
}

/**
 * PULL 协议处理器：从设备读取文件并回传给 PC。
 *
 * 响应布局：
 *   v1: u64 totalSize（0 = 错误/不存在，否则为后续字节数）| 自 offset 起的内容流
 *   v2: u64 resumeAck | u64 totalSize（0 = 错误/不存在）| payload[resumeAck .. totalSize)
 *
 * 安全规则与 push 一致：拒绝目录穿越、绝对路径、上级引用。
 *
 * 全部改为 async：await 每个 send 让出事件循环并借 socket promise 做背压，
 * 否则同步 while 会把整个文件塞进发送队列，主线程阻塞（见顶部 ACK 流控注释）。
 * 每条「请求非法 / 文件不存在」失败路径都必须显式回零响应 —— CLI 只认响应头，
 * 静默 return 会让它卡到 120s socket 超时。
 */
async function handlePull(
  client: socket.TCPSocketConnection,
  headerBuf: Uint8Array,
  remote: string,
  mode: number
): Promise<void> {
  const view = new DataView(headerBuf.buffer, headerBuf.byteOffset, headerBuf.byteLength);
  let off = 4;

  /**
   * 发零响应：v1 = u64 totalSize 0；v2 = u64 resumeAck 0 | u64 totalSize 0。
   * ArrayBuffer 按规范初始化为全零，正好对应「设备上不存在」。
   */
  const sendZeroResponse = async (): Promise<void> => {
    try {
      await client.send({ data: new ArrayBuffer(mode === 2 ? 16 : 8) });
    } catch (e) {
      hilog.warn(DOMAIN, TAG, 'pull %{public}s: zero response send failed: %{public}s',
        remote, JSON.stringify(e));
    }
  };

  if (headerBuf.length < off + 4) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: header too short', remote);
    await sendZeroResponse();
    return;
  }
  const nameLen = view.getUint32(off, true);
  if (nameLen === 0 || nameLen > MAX_REMOTE_NAME_BYTES) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: bad nameLen %{public}d', remote, nameLen);
    await sendZeroResponse();
    return;
  }
  off += 4;
  // 尾部至少 offset + size（v2 再多一个 u32 probeLen）
  if (headerBuf.length < off + nameLen + 16 + (mode === 2 ? 4 : 0)) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: header incomplete', remote);
    await sendZeroResponse();
    return;
  }

  const name = util.TextDecoder.create().decodeToString(
    headerBuf.subarray(off, off + nameLen)
  );
  if (!PushServer.isSafeName(name)) {
    hilog.error(DOMAIN, TAG, 'pull %{public}s: bad name %{public}s', remote, name);
    await sendZeroResponse();
    return;
  }
  off += nameLen;

  const offset = readU64(view, off);
  off += 8;
  // v1 用它限制回传长度；v2 预留未用
  const reqSize = readU64(view, off);
  off += 8;

  let probe = new Uint8Array(0);
  if (mode === 2) {
    const probeLen = view.getUint32(off, true);
    if (probeLen > RESUME_PROBE_BYTES) {
      hilog.error(DOMAIN, TAG, 'pull %{public}s: probe too large: %{public}d', remote, probeLen);
      await sendZeroResponse();
      return;
    }
    off += 4;
    if (headerBuf.length < off + probeLen) {
      hilog.error(DOMAIN, TAG, 'pull %{public}s: probe incomplete', remote);
      await sendZeroResponse();
      return;
    }
    probe = headerBuf.subarray(off, off + probeLen);
    off += probeLen;
  }

  const path = `${PushServer.recvDir}/${name}`;
  let fd: fileIo.File | null = null;
  try {
    if (!fileIo.accessSync(path)) {
      hilog.warn(DOMAIN, TAG, 'pull %{public}s: not found %{public}s', remote, name);
      await sendZeroResponse();
      return;
    }

    fd = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY);
    const totalSize = fileIo.statSync(path).size;

    // 回传起始偏移与响应头。v2 的 totalSize 是源文件全量；v1 的是后续字节数。
    let start: number;
    let toSend: number;
    let head: ArrayBuffer;
    if (mode === 2) {
      start = pullResumeAck(fd, totalSize, offset, probe);
      toSend = totalSize - start;
      head = encodeU64Pair(start, totalSize);
    } else {
      let toRead = reqSize > 0 ? Math.min(reqSize, totalSize - offset) : totalSize - offset;
      if (toRead <= 0 || offset >= totalSize) {
        toRead = 0;
      }
      start = offset;
      toSend = toRead;
      head = encodeU64(toRead);
    }
    await client.send({ data: head });

    if (toSend <= 0) {
      fileIo.closeSync(fd);
      fd = null;
      hilog.info(DOMAIN, TAG, 'pull %{public}s: OK %{public}s (empty)', remote, name);
      return;
    }

    // 分块读取发送。await 每个 send 是关键：socket promise 提供背压，
    // 并在块间让出事件循环，传输途中仍能处理其它连接 / runBin。
    let readPos = start;
    let left = toSend;
    while (left > 0) {
      const chunkSize = Math.min(TRANSFER_CHUNK_SIZE, left);
      const readBuf = new ArrayBuffer(chunkSize);
      const got = fileIo.readSync(fd.fd, readBuf, { offset: readPos, length: chunkSize });
      if (got <= 0) {
        hilog.warn(DOMAIN, TAG, 'pull %{public}s: short read at %{public}d', remote, readPos);
        break;
      }
      const sendBuf = got === chunkSize ? readBuf : readBuf.slice(0, got);
      await client.send({ data: sendBuf });
      readPos += got;
      left -= got;
    }

    fileIo.closeSync(fd);
    fd = null;
    hilog.info(DOMAIN, TAG, 'pull %{public}s: OK %{public}s (%{public}d bytes)',
      remote, name, toSend);
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
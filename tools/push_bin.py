#!/usr/bin/env python3
"""免打包推送二进制/依赖库/数据文件到 BinRunner（经 hdc fport TCP 通道）。

零售机上 /data/local/tmp 对 App 不可见、hdc file send 进不了 App 沙箱，
所以走 App 内 PushServer（TCP :8888）写入 filesDir/bin/。

用法:
  hdc fport tcp:8888 tcp:8888        # 先建转发（保持运行）
  python3 tools/push_bin.py ./benchmark              # 远端名 = 本地文件名
  python3 tools/push_bin.py ./libbenchmark.so benchmark   # 指定远端名
  python3 tools/push_bin.py -p 9999 ./x.so y.so      # 自定义端口

推送后执行（推送目录名字解析优先于打包 libs）:
  hdc shell aa start -b com.example.binrunner -a EntryAbility --ps cmd "benchmark --help"
"""
import socket
import struct
import sys


def push(local_path: str, remote_name: str, port: int) -> None:
    with open(local_path, "rb") as f:
        payload = f.read()
    name = remote_name.encode()
    if len(name) > 256 or "/" in remote_name or remote_name in (".", ".."):
        raise ValueError(f"非法远端名: {remote_name!r}")
    header = struct.pack("<I", len(name)) + name + struct.pack("<Q", len(payload))
    with socket.create_connection(("127.0.0.1", port), timeout=30) as s:
        s.sendall(header + payload)
    print(f"OK: {local_path} -> filesDir/bin/{remote_name} ({len(payload)} bytes)")


def main() -> None:
    argv = sys.argv[1:]
    port = 8888
    if argv[:2] and argv[0] == "-p":
        port = int(argv[1])
        argv = argv[2:]
    if not (1 <= len(argv) <= 2):
        sys.exit(__doc__)
    local = argv[0]
    remote = argv[1] if len(argv) == 2 else local.rsplit("/", 1)[-1]
    push(local, remote, port)


if __name__ == "__main__":
    main()

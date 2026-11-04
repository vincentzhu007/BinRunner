"""长传输期间的后台保活（push 与 pull 共用）。

背景（真机实测，doc §7.2）：
  1. 屏幕熄灭后 BinRunner 的 EntryAbility 进后台，PushServer 的 8888 监听
     被系统挂起，br push/pull 表现为「连接建立但无响应 / Connection refused」。
  2. hdc fport 隧道可能被系统回收（本地端口仍被残留进程占用），表现为
     传输中途 Connection refused。

keepalive 线程在传输期间：
  - 定期 power-shell wakeup 点亮屏幕；
  - 用 `hdc fport ls` 确认隧道真实存在，丢失即 force 重建。

用法（push 与 pull 都包住各自的传输/重试区间）：
    with KeepAlive(udid, port) as ka:
        push_file(...)
"""

from __future__ import annotations

import threading
import time

from binrunner.hdc import ensure_forward, run_hdc, wakeup_screen

# 唤醒间隔：屏幕熄灯策略约 30s，取 10s 留足余量且不给 hdc 施加过多压力
_WAKEUP_INTERVAL = 10.0
# fport 巡检间隔
_FPORT_CHECK_INTERVAL = 5.0


class KeepAlive:
    """传输期间的屏幕保活 + fport 守护。上下文管理器，退出时自动停止。"""

    def __init__(self, udid: str, port: int) -> None:
        self._udid = udid
        self._port = port
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "KeepAlive":
        # 先做一次准备：点亮屏幕、确保隧道健康
        wakeup_screen(self._udid)
        ensure_forward(self._udid, self._port, force=True)
        self._thread = threading.Thread(
            target=self._loop, name="br-keepalive", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def _loop(self) -> None:
        last_fport_check = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            wakeup_screen(self._udid)
            if now - last_fport_check >= _FPORT_CHECK_INTERVAL:
                last_fport_check = now
                try:
                    out = run_hdc(
                        self._udid, "fport", "ls", check=False, timeout=10
                    ).stdout
                    rule = f"tcp:{self._port} tcp:{self._port}"
                    if rule not in out:
                        # 隧道丢失：强制重建（本地端口探测会误判残留进程）
                        ensure_forward(self._udid, self._port, force=True)
                except Exception:
                    pass
            self._stop.wait(_WAKEUP_INTERVAL)

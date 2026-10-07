"""进程内事件总线：采集器发布，SSE 连接订阅。同时统计在线页面数，供“关页面即退出”使用。"""

from __future__ import annotations

import queue
import threading
import time


class EventBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.subs: list[queue.Queue] = []
        self.ever_connected = False
        self.last_disconnect = time.time()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=500)
        with self.lock:
            self.subs.append(q)
            self.ever_connected = True
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)
            if not self.subs:
                self.last_disconnect = time.time()

    def clients(self) -> int:
        with self.lock:
            return len(self.subs)

    def publish(self, kind: str, data):
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass

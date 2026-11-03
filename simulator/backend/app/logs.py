"""日志存储：环形缓冲 + 敏感信息脱敏 + 查询/导出。"""
from __future__ import annotations

import csv
import io
import json
import re
import threading
from collections import deque
from typing import Any

MAX_LOGS = 20000
SENSITIVE_KEY = re.compile(r"(pass|secret|token|private_?key|credential)", re.I)
REDACTED = "***REDACTED***"


def sanitize(obj: Any) -> Any:
    """递归脱敏：命中敏感键名的值替换为 ***REDACTED***。"""
    if isinstance(obj, dict):
        return {k: (REDACTED if SENSITIVE_KEY.search(str(k)) else sanitize(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, str) and ("BEGIN PRIVATE KEY" in obj or "BEGIN RSA PRIVATE KEY" in obj):
        return REDACTED
    return obj


class LogStore:
    """进程级日志存储（线程安全）。单条结构：
    {ts, deviceId, direction, topic, qos, msgType, cmdId, payload, result, error, latencyMs}
    direction: in(设备收到) / out(设备发出) / evt(状态事件)
    """

    def __init__(self, maxlen: int = MAX_LOGS):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.seq = 0

    def add(self, *, device: str, direction: str, topic: str = "", qos: int | None = None,
            msg_type: str | None = None, cmd_id: str | None = None,
            payload: Any = None, result: str = "ok", error: str | None = None,
            latency_ms: int | None = None) -> dict:
        with self._lock:
            self.seq += 1
            rec = {
                "seq": self.seq,
                "ts": int(__import__("time").time() * 1000),
                "deviceId": device,
                "direction": direction,
                "topic": topic,
                "qos": qos,
                "msgType": msg_type,
                "cmdId": cmd_id,
                "payload": sanitize(payload),
                "result": result,
                "error": error,
                "latencyMs": latency_ms,
            }
            self._buf.append(rec)
            return rec

    def query(self, *, device: str | None = None, direction: str | None = None,
              msg_type: str | None = None, q: str | None = None,
              limit: int = 200, before_seq: int | None = None) -> list[dict]:
        """按时间倒序返回（最新在前）。"""
        with self._lock:
            items = list(self._buf)
        if before_seq is not None:
            items = [r for r in items if r["seq"] < before_seq]
        if device:
            items = [r for r in items if r["deviceId"] == device]
        if direction:
            items = [r for r in items if r["direction"] == direction]
        if msg_type:
            items = [r for r in items if (r["msgType"] or "") == msg_type]
        if q:
            needle = q.lower()
            items = [r for r in items if needle in str(r).lower()]
        return list(reversed(items[-limit:]))

    def export_json(self, **kw) -> str:
        recs = self.query(limit=kw.pop("limit", 5000), **kw)
        return json.dumps(recs, ensure_ascii=False, indent=1)

    def export_csv(self, **kw) -> str:
        recs = self.query(limit=kw.pop("limit", 5000), **kw)
        cols = ["seq", "ts", "deviceId", "direction", "topic", "qos",
                "msgType", "cmdId", "result", "error", "latencyMs", "payload"]
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        for r in recs:
            w.writerow([r.get(c) if c != "payload" else json.dumps(r.get("payload"), ensure_ascii=False)
                        for c in cols])
        return out.getvalue()


LOGS = LogStore()

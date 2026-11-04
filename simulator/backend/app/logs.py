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
RAW_MAX = 4000                     # 原始报文截断长度（超长截断并标注字节数）
SENSITIVE_KEY = re.compile(r"(pass|secret|token|private_?key|credential)", re.I)
REDACTED = "***REDACTED***"

# 数据方向（设备视角）：out=上行（设备→平台）、in=下行（平台→设备）、evt=状态事件
DIRECTION_CN = {"out": "上行(设备→平台)", "in": "下行(平台→设备)", "evt": "事件"}


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
    {ts, deviceId, direction, topic, qos, msgType, cmdId, payload, raw, result, error, latencyMs}
    direction: out(上行，设备发出) / in(下行，设备收到) / evt(状态事件)
    payload: 结构化解析后的内容（已脱敏）；raw: 原始报文文本（截断至 RAW_MAX）
    """

    def __init__(self, maxlen: int = MAX_LOGS):
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.seq = 0

    def add(self, *, device: str, direction: str, topic: str = "", qos: int | None = None,
            msg_type: str | None = None, cmd_id: str | None = None,
            payload: Any = None, raw: str | None = None,
            result: str = "ok", error: str | None = None,
            latency_ms: int | None = None) -> dict:
        with self._lock:
            self.seq += 1
            rec = {
                "seq": self.seq,
                "ts": int(__import__("time").time() * 1000),
                "deviceId": device,
                "direction": direction,
                "directionCN": DIRECTION_CN.get(direction, direction),
                "topic": topic,
                "qos": qos,
                "msgType": msg_type,
                "cmdId": cmd_id,
                "payload": sanitize(payload),
                "raw": (raw[:RAW_MAX] + f"…(截断,共{len(raw)}B)") if raw and len(raw) > RAW_MAX else raw,
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
        cols = ["seq", "ts", "deviceId", "direction", "directionCN", "topic", "qos",
                "msgType", "cmdId", "result", "error", "latencyMs", "payload", "raw"]
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        for r in recs:
            row = []
            for c in cols:
                v = r.get(c)
                if c in ("payload", "raw") and not isinstance(v, str):
                    v = json.dumps(v, ensure_ascii=False)
                row.append(v)
            w.writerow(row)
        return out.getvalue()


LOGS = LogStore()

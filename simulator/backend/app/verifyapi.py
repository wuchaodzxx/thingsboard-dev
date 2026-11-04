"""平台侧验证控制台 REST（P5）：封装 ThingsBoard REST 完成需求第四章 7 项验证。
所有判定基于 TB 实际存储/响应，不依赖模拟器自身日志。

需求映射：
  7.1 设备注册/连接/认证状态  → GET  /api/verify/devices
  7.2 最新遥测与历史数据      → GET  /api/verify/telemetry/latest | /telemetry/history
  7.3 共享属性下发与应用验证  → POST /api/verify/attributes + GET /api/verify/attributes
  7.4 数据召测                → POST /api/verify/rpc (method=readRealtimeData 等)
  7.5 远程控制                → POST /api/verify/rpc (method=setActivePowerTarget 等)
  7.6 指令响应与执行数据      → GET  /api/verify/ledger（平台 cmdStatus + 模拟器台账）
  7.7 发送 vs 接收完整性比对  → POST /api/verify/compare
"""
from __future__ import annotations

import os
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .logs import LOGS

router = APIRouter(prefix="/api/verify", tags=["verify"])

TB_URL = os.environ.get("TB_URL", "http://localhost:18080")
TB_USER = os.environ.get("TB_USER", "tenant@thingsboard.org")
TB_PASS = os.environ.get("TB_PASS", "tenant")


def _tb():
    from certmgr.tb_client import TBClient  # 局部导入：避免模块加载即登录
    tb = TBClient(TB_URL, TB_USER, TB_PASS)
    tb.login()
    return tb


def _dev_id(tb, device: str) -> str:
    d = tb.get_device(device)
    if not d:
        raise HTTPException(404, f"TB 中不存在设备 {device}")
    return d["id"]["id"]


# ---------------- 7.1 注册/连接/认证状态 ----------------
@router.get("/devices")
def verify_devices():
    tb = _tb()
    out = []
    for d in tb._req("GET", "/api/tenant/devices", params={"pageSize": 100, "page": 0})["data"]:
        did = d["id"]["id"]
        # 本 TB 版本列表/详情接口均不含 active，仅 /api/device/info/{id} 返回
        active = tb._req("GET", f"/api/device/info/{did}").get("active")
        out.append({
            "name": d["name"],
            "devType": d.get("type"),
            "active": active,                                # TB 侧在线状态
            "credentialsType": tb.get_credentials(did)["credentialsType"],  # X509_CERTIFICATE=双向TLS
            "createdTs": d.get("createdTs"),
            "deviceId": did,
        })
    return {"devices": out}


# ---------------- 7.2 最新遥测与历史 ----------------
@router.get("/telemetry/latest")
def telemetry_latest(device: str, keys: str = ""):
    tb = _tb()
    did = _dev_id(tb, device)
    return {"device": device, "latest": tb.get_latest_timeseries(did, keys)}


@router.get("/telemetry/history")
def telemetry_history(device: str, keys: str, startTs: int = Query(...),
                      endTs: int = Query(...), limit: int = Query(1000, le=10000)):
    tb = _tb()
    did = _dev_id(tb, device)
    hist = tb.get_history_timeseries(did, keys, startTs, endTs, limit=limit)
    return {"device": device, "startTs": startTs, "endTs": endTs,
            "series": {k: len(v) for k, v in hist.items()}, "data": hist}


# ---------------- 7.3 共享属性下发 + 设备应用回执 ----------------
class AttrReq(BaseModel):
    device: str
    payload: dict


@router.post("/attributes")
def save_attributes(req: AttrReq):
    tb = _tb()
    did = _dev_id(tb, req.device)
    tb.save_shared_attributes(did, req.payload)
    return {"device": req.device, "saved": req.payload}


@router.get("/attributes")
def get_attributes(device: str):
    tb = _tb()
    did = _dev_id(tb, device)
    return {"device": device, "clientScope": tb.get_attributes(did)}


# ---------------- 7.4/7.5 召测与远程控制 ----------------
class RpcReq(BaseModel):
    device: str
    method: str
    env: dict                       # {cmdId, issuedAt, expireAt, params}


@router.post("/rpc")
def send_rpc(req: RpcReq):
    tb = _tb()
    did = _dev_id(tb, req.device)
    resp = tb.send_two_way_rpc(did, req.method, req.env)
    return {"device": req.device, "method": req.method, "response": resp}


# ---------------- 7.6 指令响应与执行数据 ----------------
@router.get("/ledger")
def cmd_ledger(device: str, limit: int = Query(50, le=500)):
    """平台侧 cmdState/actualP 最新值 + 模拟器指令台账（双端对照）。"""
    tb = _tb()
    did = _dev_id(tb, device)
    latest = tb.get_latest_timeseries(did, "cmdState,cmdId,actualP")
    plat = {k: latest[k][0] for k in latest if latest.get(k)}
    from .main import manager  # 模拟器台账（进程内存）
    dev = manager.devices.get(device)
    ledger = [{"cmdId": k, **v} for k, v in
              sorted(dev.ledger.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:limit]] \
        if dev else []
    return {"device": device, "platformLatest": plat, "deviceLedger": ledger}


# ---------------- 7.7 发送 vs 接收完整性比对 ----------------
class CompareReq(BaseModel):
    device: str
    windowSec: int = 300            # 比对窗口（秒）
    msgType: str = "M03"            # 比对的报文类型
    tolerance: float = 0.5          # 数值容差（同 ts 的 p 值应完全一致，容差仅防浮点）


@router.post("/compare")
def compare(req: CompareReq):
    """发送侧 = 模拟器日志 result=sent 的原始 payload（含 ts/values）；
    接收侧 = TB 历史库窗口内点。按 ts 对齐逐点核对。"""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - req.windowSec * 1000

    sent: dict[int, dict] = {}
    for rec in LOGS.query(device=req.device, direction="out", msg_type=req.msgType, limit=2000):
        p = rec.get("payload")
        ts = p.get("ts") if isinstance(p, dict) else None
        if rec.get("result") == "sent" and isinstance(ts, int) and start_ms <= ts <= now_ms:
            sent[ts] = p.get("values", {})

    tb = _tb()
    did = _dev_id(tb, req.device)
    keys = ",".join(sent[next(iter(sent))].keys()) if sent else "p"
    hist = tb.get_history_timeseries(did, keys, start_ms, now_ms, limit=10000)
    recv_ts: dict[int, dict] = {}
    for k, pts in hist.items():
        for pt in pts:
            recv_ts.setdefault(pt["ts"], {})[k] = float(pt["value"])

    sent_ts = set(sent)
    recv_only = set(recv_ts) - sent_ts
    missing = sorted(sent_ts - set(recv_ts))
    mismatch = []
    for ts in sorted(sent_ts & set(recv_ts)):
        for k, v in sent[ts].items():
            if k in recv_ts[ts] and abs(float(v) - recv_ts[ts][k]) > req.tolerance:
                mismatch.append({"ts": ts, "key": k, "sent": v, "received": recv_ts[ts][k]})
    ok = not missing and not mismatch
    return {
        "device": req.device, "msgType": req.msgType, "windowSec": req.windowSec,
        "sentCount": len(sent_ts), "receivedCount": len(sent_ts & set(recv_ts)),
        "missingTs": missing, "platformOnlyTs": sorted(recv_only)[:20],
        "valueMismatch": mismatch[:20],
        "verdict": "PASS" if ok else "FAIL",
    }

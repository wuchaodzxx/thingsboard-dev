"""VPP 设备模拟器后端 REST（端口 19000）。"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import certapi, verifyapi
from .certapi import SECRETS_DIR  # noqa: F401  (校验 certmgr 路径可导入)
from .devices import DeviceConfig, DeviceManager
from .logs import LOGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

WORKSPACE = Path(__file__).resolve().parents[3]
CA_FILE = str(SECRETS_DIR / "root" / "ca.pem")
DEVICE_CERT_DIR = str(SECRETS_DIR / "devices")
MQTT_HOST = os.environ.get("SIM_MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("SIM_MQTT_PORT", "18885"))
TB_URL = os.environ.get("TB_URL", "http://localhost:18080")

app = FastAPI(title="VPP Device Simulator", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(certapi.router)
app.include_router(verifyapi.router)

manager = DeviceManager(mqtt_host=MQTT_HOST, mqtt_port=MQTT_PORT, ca_file=CA_FILE,
                        cert_dir=DEVICE_CERT_DIR, workspace_root=str(WORKSPACE))
_started = time.time()


@app.on_event("shutdown")
def _on_shutdown():
    manager.shutdown_all()


# ---------------- 基础 ----------------
@app.get("/api/health")
def health():
    return {"status": "ok", "uptimeSec": int(time.time() - _started),
            "mqtt": {"host": MQTT_HOST, "port": MQTT_PORT}, "tbUrl": TB_URL,
            "devices": len(manager.devices)}


# ---------------- 设备管理 ----------------
class CreateDeviceReq(BaseModel):
    name: str
    devType: str = "pv"          # pv | pcs | flex
    capKw: float = 100.0
    autoRegister: bool = True    # 签发证书（缺省时）并注册到 TB
    autoConnect: bool = True


@app.post("/api/devices")
def create_device(req: CreateDeviceReq):
    if not req.name or "/" in req.name or len(req.name) > 48:
        raise HTTPException(400, "非法设备名")
    if req.devType not in ("pv", "pcs", "flex"):
        raise HTTPException(400, "devType 必须为 pv|pcs|flex")
    if req.name in manager.devices:
        raise HTTPException(409, f"设备已存在 {req.name}")
    register_note = "skipped"
    if req.autoRegister:
        pki = certapi._pki()
        have = any(c["name"] == req.name and c["kind"] == "device" for c in pki.list_certs())
        if not have:
            pki.issue_device_cert(req.name)
            register_note = "cert-issued"
        else:
            register_note = "cert-exists"
        try:
            certapi._tb().register_device_with_cert(req.name, pki.cert_pem(req.name), req.devType)
            register_note += "+tb-registered"
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"TB 注册失败: {e}") from e
    cfg = DeviceConfig.defaults_for(req.devType, req.name, req.capKw)
    dev = manager.create(cfg, auto_start=req.autoConnect)
    return {"created": req.name, "register": register_note, "status": dev.status()}


@app.get("/api/devices")
def list_devices():
    return manager.summary()


@app.get("/api/devices/{name}")
def device_detail(name: str):
    dev = manager.get(name)
    if not dev:
        raise HTTPException(404, f"设备不存在 {name}")
    return {"status": dev.status(), "cfg": {k: getattr(dev.cfg, k) for k in vars(dev.cfg) if not k.startswith("_") and dev.cfg.__dataclass_fields__.get(k)}}


@app.delete("/api/devices/{name}")
def delete_device(name: str):
    if not manager.delete(name):
        raise HTTPException(404, f"设备不存在 {name}")
    return {"deleted": name}


@app.post("/api/devices/{name}/connect")
def connect_device(name: str):
    dev = manager.get(name) or _err404(name)
    dev.start_connect()
    return {"name": name, "connecting": True}


@app.post("/api/devices/{name}/disconnect")
def disconnect_device(name: str):
    dev = manager.get(name) or _err404(name)
    dev.disconnect("api")
    return {"name": name, "connected": False}


def _err404(name: str):
    raise HTTPException(404, f"设备不存在 {name}")


class InjectReq(BaseModel):
    mode: str                    # disconnect|delay|duplicate|badJson|missingField|outOfRange|rejectCmd|failCmd
    durationSec: float = 10      # disconnect/delay 生效时长
    count: int = 1               # duplicate/badJson/missingField/outOfRange/rejectCmd/failCmd 次数
    ms: int = 300                # delay 毫秒
    key: str = "p"               # outOfRange 字段
    value: float = 99999.0       # outOfRange 值
    field: str = "p"             # missingField 字段


@app.post("/api/devices/{name}/inject")
def inject(name: str, req: InjectReq):
    dev = manager.get(name) or _err404(name)
    try:
        return {"name": name, "active": dev.inject(req.mode, duration_sec=req.durationSec,
                                                   count=req.count, ms=req.ms,
                                                   key=req.key, value=req.value,
                                                   field=req.field)}
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class CurveReq(BaseModel):
    capKw: float | None = None
    rampRate: float | None = None
    socMin: float | None = None
    socMax: float | None = None
    chgPMax: float | None = None
    disPMax: float | None = None


@app.post("/api/devices/{name}/curve")
def adjust_curve(name: str, req: CurveReq):
    dev = manager.get(name) or _err404(name)
    c = dev.cfg
    data = req.model_dump(exclude_none=True)
    for k, v in data.items():
        if v <= 0 and k != "socMin":
            raise HTTPException(400, f"{k} 必须为正数")
        setattr(c, k, v)
    if c.devType in ("pv", "pcs"):
        c.pMax = c.capKw
        c.pMin = -c.capKw if c.devType == "pcs" else 0.0
    if c.devType == "pcs":
        c.chgPMax, c.disPMax = (data.get("chgPMax", c.chgPMax), data.get("disPMax", c.disPMax))
    return {"name": name, "cfg": {k: getattr(c, k) for k in data} | {"pMin": c.pMin, "pMax": c.pMax}}


@app.get("/api/devices/{name}/sent")
def device_sent(name: str, limit: int = Query(100, le=1000)):
    _dev = manager.get(name) or _err404(name)
    return LOGS.query(device=name, direction="out", limit=limit)


@app.get("/api/devices/{name}/ledger")
def device_ledger(name: str, limit: int = Query(100, le=1000)):
    dev = manager.get(name) or _err404(name)
    items = sorted(dev.ledger.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:limit]
    return [{"cmdId": k, **v} for k, v in items]


# ---------------- 日志 ----------------
@app.get("/api/debug/threads")
def debug_threads():
    """诊断端点：输出进程内全部 Python 线程栈（用于定位线程停摆）。"""
    import threading
    import traceback
    frames = sys._current_frames()
    out = {}
    for t in threading.enumerate():
        fr = frames.get(t.ident)
        out[t.name] = "".join(traceback.format_stack(fr)).strip() if fr else "<no frame>"
    return out


@app.get("/api/logs")
def logs(device: str | None = None, direction: str | None = None,
         msgType: str | None = None, q: str | None = None,
         limit: int = Query(200, le=2000), beforeSeq: int | None = None):
    return LOGS.query(device=device, direction=direction, msg_type=msgType,
                      q=q, limit=limit, before_seq=beforeSeq)


@app.get("/api/logs/export")
def logs_export(device: str | None = None, direction: str | None = None,
                msgType: str | None = None, q: str | None = None,
                fmt: str = "json"):
    if fmt == "csv":
        return PlainTextResponse(LOGS.export_csv(device=device, direction=direction,
                                                 msg_type=msgType, q=q),
                                 media_type="text/csv; charset=utf-8")
    return PlainTextResponse(LOGS.export_json(device=device, direction=direction,
                                              msg_type=msgType, q=q),
                             media_type="application/json; charset=utf-8")


# ---------------- 前端静态资源（P4b 构建产物存在时挂载） ----------------
_frontend = Path(__file__).resolve().parents[2] / "frontend"
if (_frontend / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


def json_default(_o):
    raise TypeError


@app.exception_handler(Exception)
async def unhandled(_req, exc):
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


def main():
    import uvicorn
    port = int(os.environ.get("SIM_PORT", "19000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()

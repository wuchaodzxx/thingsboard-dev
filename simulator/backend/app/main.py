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
from .certapi import STORE_DIR     # 导入证书存放目录（证书工具线下同步）
from .devices import DeviceConfig, DeviceManager
from .logs import LOGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

WORKSPACE = Path(__file__).resolve().parents[3]
CA_FILE = str(STORE_DIR / "ca.pem")            # 平台根证书（证书工具导出后线下导入）
DEVICE_CERT_DIR = str(STORE_DIR / "devices")   # 设备证书目录（cert.pem/key.pem）
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
    devType: str = "pv"          # pv | pcs | load | wind
    capKw: float = 100.0
    loadType: int = 1            # devType=load 时生效：负荷分类码（docs/01 §8.5：1~7,99）
    autoConnect: bool = True     # 证书未导入时连接线程会自动重试


LOAD_TYPE_CODES = (1, 2, 3, 4, 5, 6, 7, 99)   # 负荷分类编码（docs/01 §8.5）


@app.post("/api/devices")
def create_device(req: CreateDeviceReq):
    if not req.name or "/" in req.name or len(req.name) > 48:
        raise HTTPException(400, "非法设备名")
    if req.devType not in ("pv", "pcs", "load", "wind"):
        raise HTTPException(400, "devType 必须为 pv|pcs|load|wind")
    if req.devType == "load" and req.loadType not in LOAD_TYPE_CODES:
        raise HTTPException(400, "loadType 必须为 1~7 或 99（编码见 docs/01 §8.5）")
    if req.name in manager.devices:      # 幂等复用：同名设备不重复创建
        dev = manager.get(req.name)
        return {"created": req.name, "status": dev.status()}
    # 模拟器不签发证书：设备创建后，先在「证书导入」页导入证书工具线下同步的证书，
    # 再点设备行「注册」（TB X.509 建档）与「连接」。
    cfg = DeviceConfig.defaults_for(req.devType, req.name, req.capKw)
    if req.devType == "load":
        cfg.loadType = req.loadType
    dev = manager.create(cfg, auto_start=req.autoConnect)
    return {"created": req.name, "status": dev.status()}


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
    if c.devType in ("pv", "pcs", "wind"):
        c.pMax = c.capKw
        c.pMin = -c.capKw if c.devType == "pcs" else 0.0
    elif c.devType == "load":
        c.pMin, c.pMax = -c.capKw, 0.0
    if c.devType == "pcs":
        c.chgPMax, c.disPMax = (data.get("chgPMax", c.chgPMax), data.get("disPMax", c.disPMax))
    return {"name": name, "cfg": {k: getattr(c, k) for k in data} | {"pMin": c.pMin, "pMax": c.pMax}}


# ---------------- 负荷特征模拟配置（devType=load/wind） ----------------
class LoadProfileReq(BaseModel):
    """负荷特征参数：空字段保持不变。load 设备与 wind 设备共用（wind 仅 windSpeedAvg 生效）。"""
    loadType: int | None = None        # 负荷分类码（docs/01 §8.5 编码表：1~7,99）
    baseRatio: float | None = None     # 基线负荷率 0~1（0/空=按 loadType 特征曲线）
    noiseAmp: float | None = None      # 随机波动幅度 0~0.3（空=默认 0.08）
    dutyOnMin: int | None = None       # 占空启停：运行分钟（0=不启用）
    dutyOffMin: int | None = None      # 占空启停：停运分钟
    windSpeedAvg: float | None = None  # wind 平均风速 m/s（1~25）


@app.post("/api/devices/{name}/load-profile")
def set_load_profile(name: str, req: LoadProfileReq):
    """配置负荷特征模拟参数（负荷曲线形状/基线/波动/占空启停/风速）。"""
    dev = manager.get(name) or _err404(name)
    c = dev.cfg
    if c.devType not in ("load", "wind"):
        raise HTTPException(400, "仅 load/wind 类型设备支持负荷特征配置")
    d = req.model_dump(exclude_none=True)
    if "loadType" in d and d["loadType"] not in LOAD_TYPE_CODES:
        raise HTTPException(400, "loadType 必须为 1~7 或 99")
    if "baseRatio" in d and not (0 < d["baseRatio"] <= 1):
        raise HTTPException(400, "baseRatio 必须为 (0,1]")
    if "noiseAmp" in d and not (0 <= d["noiseAmp"] <= 0.3):
        raise HTTPException(400, "noiseAmp 必须为 0~0.3")
    if "windSpeedAvg" in d and not (1 <= d["windSpeedAvg"] <= 25):
        raise HTTPException(400, "windSpeedAvg 必须为 1~25 m/s")
    on, off = d.get("dutyOnMin", c.dutyOnMin), d.get("dutyOffMin", c.dutyOffMin)
    if on > 0 and off <= 0 or off > 0 and on <= 0:
        raise HTTPException(400, "占空启停 dutyOnMin/dutyOffMin 必须同时 >0（或同时 0 关闭）")
    if "loadType" in d:
        c.loadType = d["loadType"]
    if "baseRatio" in d:
        c.loadBaseRatio = d["baseRatio"]
    if "noiseAmp" in d:
        c.loadNoiseAmp = d["noiseAmp"]
    if "dutyOnMin" in d:
        c.dutyOnMin = d["dutyOnMin"]
    if "dutyOffMin" in d:
        c.dutyOffMin = d["dutyOffMin"]
    if "windSpeedAvg" in d:
        c.windSpeedAvg = d["windSpeedAvg"]
    return {"name": name, "devType": c.devType,
            "loadProfile": {"loadType": c.loadType, "baseRatio": c.loadBaseRatio or None,
                            "noiseAmp": c.loadNoiseAmp or None,
                            "dutyOnMin": c.dutyOnMin, "dutyOffMin": c.dutyOffMin,
                            "windSpeedAvg": c.windSpeedAvg}}


@app.get("/api/devices/{name}/load-profile")
def get_load_profile(name: str):
    dev = manager.get(name) or _err404(name)
    c = dev.cfg
    return {"name": name, "devType": c.devType,
            "loadProfile": {"loadType": c.loadType, "baseRatio": c.loadBaseRatio or None,
                            "noiseAmp": c.loadNoiseAmp or None,
                            "dutyOnMin": c.dutyOnMin, "dutyOffMin": c.dutyOffMin,
                            "windSpeedAvg": c.windSpeedAvg}}


# ---------------- 手动注册（先在「证书导入」页导入证书工具线下同步的证书） ----------------
@app.post("/api/devices/{name}/register")
def register_device_manual(name: str):
    """手动注册：使用已导入的设备证书在 TB 建档并绑定 X.509 凭据（devType 取设备配置）。

    完整流程：证书管理工具签发导出 → 线下同步 → 「证书导入」页导入 → 本接口注册 → 连接。
    """
    dev = manager.get(name) or _err404(name)
    cert_file = certapi.STORE_DIR / "devices" / name / "cert.pem"
    if not cert_file.is_file():
        raise HTTPException(400, "设备证书未导入：请先在「证书导入」页导入证书管理工具导出的 cert.pem/key.pem")
    cert_pem = cert_file.read_text()
    try:
        certapi._tb().register_device_with_cert(name, cert_pem, dev.cfg.devType)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"TB 注册失败: {e}") from e
    LOGS.add(device=name, direction="evt", result="tb-registered",
             error=f"devType={dev.cfg.devType}")
    return {"name": name, "register": "tb-registered", "devType": dev.cfg.devType}


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

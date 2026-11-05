"""证书管理工具后端（独立部署，默认端口 9001，对外 19001）。

与 ThingsBoard / 设备模拟器完全解耦，可单独主机运行：
  - 启动时幂等初始化 根CA + 中间CA（root 默认已生成，仅查看/导出，无生成入口）
  - 证书数据经 CERTADMIN_SECRETS 卷持久化（compose 挂载 ./data:/data/secrets）
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import api

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("certadmin")

app = FastAPI(title="VPP Cert Admin", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(api.router)


@app.on_event("startup")
def _init_ca():
    """幂等初始化根 CA + 中间 CA；已存在则跳过（FileExistsError）。"""
    try:
        res = api._pki().init_ca()
        log.info("CA 已初始化（root/intermediate 默认生成）: %s", res)
    except FileExistsError:
        log.info("CA 已存在，跳过初始化")


_frontend = Path(__file__).resolve().parents[2] / "frontend"
if (_frontend / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")


@app.exception_handler(Exception)
async def unhandled(_req, exc):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


def main():
    import uvicorn
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("CERTADMIN_PORT", "9001")), log_level="info")


if __name__ == "__main__":
    main()

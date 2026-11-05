"""证书导入 REST（精简版）：模拟器不签发证书。

证书由独立「证书管理工具」签发并导出（线下同步），模拟器只负责导入使用：
  - 平台根证书 ca.pem：TLS 校验 TB 服务端（信任根）
  - 设备证书 cert.pem + 私钥 key.pem：TLS 客户端证书 + TB X.509 注册

数据布局（SIM_CERT_STORE，默认 simulator/data/certs）：
  store/
  ├── ca.pem                    平台根证书（信任根，导入一次即可）
  └── devices/<name>/ cert.pem key.pem fullchain.pem（交付包内容）
"""
from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_WORKSPACE = Path(__file__).resolve().parents[3]          # .../thingsboard-dev
_CERTMGR_PARENT = _WORKSPACE / "certmgr"
if str(_CERTMGR_PARENT) not in sys.path:
    sys.path.insert(0, str(_CERTMGR_PARENT))

from certmgr import openssl_tool as ossl  # noqa: E402  （仅用 info() 解析证书）
from certmgr.tb_client import TBClient    # noqa: E402  （注册 TB 用）

STORE_DIR = Path(os.environ.get("SIM_CERT_STORE", _WORKSPACE / "simulator" / "data" / "certs"))
TB_URL = os.environ.get("TB_URL", "http://localhost:18080")
TB_USER = os.environ.get("TB_USER", "tenant@thingsboard.org")
TB_PASS = os.environ.get("TB_PASS", "tenant")

_lock = threading.Lock()


def _ca_file() -> Path:
    return STORE_DIR / "ca.pem"


def _dev_dir(name: str) -> Path:
    return STORE_DIR / "devices" / name


def _tb() -> TBClient:
    tb = TBClient(TB_URL, TB_USER, TB_PASS)
    tb.login()
    return tb


def _scan() -> list[dict]:
    out = []
    base = STORE_DIR / "devices"
    if not base.exists():
        return out
    for d in sorted(base.iterdir()):
        cert = d / "cert.pem"
        if not cert.is_file():
            continue
        try:
            info = ossl.info(cert)
        except Exception:  # noqa: BLE001
            continue
        out.append({"name": d.name,
                    "cn": info.get("cn", ""), "serial": info.get("serial", ""),
                    "notBefore": info.get("notBefore", ""), "notAfter": info.get("notAfter", ""),
                    "hasKey": (d / "key.pem").is_file()})
    return out


class ImportReq(BaseModel):
    name: str
    certPem: str                      # 交付包 cert.pem（或 fullchain.pem）
    keyPem: str | None = None         # 模拟器本机建立 TLS 需要私钥
    caPem: str | None = None          # 平台根证书；首次导入一次即可（覆盖式更新信任根）


router = APIRouter(prefix="/api/certs", tags=["certs"])


@router.get("")
def list_certs():
    """已导入的设备证书（本机文件扫描，非平台签发登记簿）。"""
    with _lock:
        return _scan()


@router.get("/ca")
def ca_status():
    """平台根证书信任状态（证书工具导出 root-ca.pem，线下同步后在此导入）。"""
    ca = _ca_file()
    if not ca.is_file():
        return {"present": False}
    try:
        info = ossl.info(ca)
    except Exception:  # noqa: BLE001
        return {"present": False}
    return {"present": True, "cn": info.get("cn", ""),
            "serial": info.get("serial", ""), "notAfter": info.get("notAfter", "")}


@router.post("/import")
def import_cert(req: ImportReq):
    """导入证书工具线下同步的证书（对应交付包 cert.pem/key.pem/root-ca.pem）。

    模拟器不签发证书：签发请使用独立证书管理工具（docs/03 证书管理操作手册）。
    """
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    cert_pem = (req.certPem or "").strip()
    if "-----BEGIN CERTIFICATE-----" not in cert_pem:
        raise HTTPException(400, "certPem 不是 PEM 证书（缺少 BEGIN CERTIFICATE）")
    with _lock:
        d = _dev_dir(req.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "cert.pem").write_text(cert_pem + "\n")
        try:
            info = ossl.info(d / "cert.pem")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"证书解析失败: {e}") from e
        if not info.get("cn"):
            raise HTTPException(400, "证书解析失败或缺少 CN")
        (d / "fullchain.pem").write_text(cert_pem + "\n")
        key_imported = False
        if req.keyPem and "PRIVATE KEY" in req.keyPem:
            (d / "key.pem").write_text(req.keyPem.strip() + "\n")
            key_imported = True
        ca_imported = False
        if req.caPem and "-----BEGIN CERTIFICATE-----" in req.caPem:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            _ca_file().write_text(req.caPem.strip() + "\n")
            ca_imported = True
    return {"name": req.name, "cn": info.get("cn", ""), "serial": info.get("serial", ""),
            "notAfter": info.get("notAfter", ""), "keyImported": key_imported,
            "caImported": ca_imported}


@router.get("/{name}")
def cert_detail(name: str):
    d = _dev_dir(name)
    cert = d / "cert.pem"
    if not cert.is_file():
        raise HTTPException(404, f"未导入 {name}")
    info = ossl.info(cert)
    return {"name": name, "cn": info.get("cn", ""), "serial": info.get("serial", ""),
            "notBefore": info.get("notBefore", ""), "notAfter": info.get("notAfter", ""),
            "hasKey": (d / "key.pem").is_file(), "certPem": cert.read_text()}


@router.delete("/{name}")
def delete_cert(name: str):
    d = _dev_dir(name)
    if not d.is_dir():
        raise HTTPException(404, f"未导入 {name}")
    with _lock:
        shutil.rmtree(d)
    return {"deleted": name}

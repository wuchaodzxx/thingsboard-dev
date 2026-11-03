"""证书管理 REST（封装 certmgr PKI + TB 绑定）。CA 私钥不通过 API 暴露。"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .logs import sanitize

router = APIRouter(prefix="/api/certs", tags=["certs"])

_WORKSPACE = Path(__file__).resolve().parents[3]          # .../thingsboard-dev
_CERTMGR_PARENT = _WORKSPACE / "certmgr"
if str(_CERTMGR_PARENT) not in sys.path:
    sys.path.insert(0, str(_CERTMGR_PARENT))

from certmgr.pki import PKI  # noqa: E402
from certmgr.tb_client import TBClient  # noqa: E402

SECRETS_DIR = Path(os.environ.get("SIM_SECRETS_DIR", _CERTMGR_PARENT / "secrets"))
TB_URL = os.environ.get("TB_URL", "http://localhost:18080")
TB_USER = os.environ.get("TB_USER", "tenant@thingsboard.org")
TB_PASS = os.environ.get("TB_PASS", "tenant")

_lock = threading.Lock()  # openssl 串行化


def _pki() -> PKI:
    return PKI(SECRETS_DIR)


def _tb() -> TBClient:
    tb = TBClient(TB_URL, TB_USER, TB_PASS)
    tb.login()
    return tb


class IssueReq(BaseModel):
    name: str
    days: int = 365


class CsrReq(BaseModel):
    name: str
    csrPem: str
    days: int = 365


class RevokeReq(BaseModel):
    reason: str = "unspecified"
    tbRevoke: bool = True


class RegisterReq(BaseModel):
    type: str = "default"


@router.get("")
def list_certs():
    with _lock:
        return sanitize(_pki().list_certs())


@router.get("/expiry")
def expiry(days: int = 30):
    with _lock:
        return _pki().check_expiry(days)


@router.post("/device")
def issue_device(req: IssueReq):
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    try:
        with _lock:
            return _pki().issue_device_cert(req.name, days=req.days)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e


@router.post("/csr")
def sign_csr(req: CsrReq):
    import tempfile
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(req.csrPem)
        csr_path = f.name
    try:
        with _lock:
            return _pki().sign_external_csr(Path(csr_path), req.name, days=req.days)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    finally:
        os.unlink(csr_path)


@router.get("/{name}")
def cert_detail(name: str):
    reg = {c["name"]: c for c in _pki().list_certs()}
    if name not in reg:
        raise HTTPException(404, f"不存在 {name}")
    info = sanitize(reg[name])
    cert_file = reg[name]["files"].get("cert")
    if cert_file and Path(cert_file).exists():
        info["certPem"] = Path(cert_file).read_text()
    return info


@router.post("/{name}/verify")
def verify_cert(name: str):
    try:
        with _lock:
            return {"name": name, "result": _pki().verify_device(name)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


@router.post("/{name}/revoke")
def revoke_cert(name: str, req: RevokeReq):
    try:
        with _lock:
            res = _pki().revoke(name, req.reason)
        if req.tbRevoke:
            dev = _tb().get_device(name)
            if dev is None:
                res["tbRevoke"] = "device-not-in-tb"
            else:
                _tb().reset_credentials_to_access_token(dev["id"]["id"])
                res["tbRevoke"] = "credentials-reset"
        return res
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e


@router.post("/{name}/tb-register")
def tb_register(name: str, req: RegisterReq):
    try:
        with _lock:
            cert_pem = _pki().cert_pem(name)
        dev = _tb().register_device_with_cert(name, cert_pem, req.type)
        return {"device": dev["name"], "deviceId": dev["id"]["id"],
                "credentialsType": "X509_CERTIFICATE"}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e

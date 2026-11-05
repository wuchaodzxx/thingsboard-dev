"""证书管理 REST：CA 查看/导出、证书新增（签发/CSR 代签/导入）、查看、导出交付包、交付台账。

范围约定（模拟真实线下交付场景，与 TB / 模拟器解耦）：
  - 根证书默认已生成（启动幂等初始化），不提供生成入口，仅查看与导出
  - 本工具只管证书签发与导出；TB 注册、设备连接由平台/设备侧线下同步后自行完成
  - CA 私钥不经 API 暴露；导出内容仅证书与（未销毁的）设备私钥
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

_ROOT = Path(__file__).resolve().parents[3]           # 容器 /workspace；仓库根目录（宿主机直跑）
_CERTMGR_PARENT = _ROOT / "certmgr"                   # certmgr 包父目录（certmgr/certmgr/pki.py）
if str(_CERTMGR_PARENT) not in sys.path:
    sys.path.insert(0, str(_CERTMGR_PARENT))

from certmgr.openssl_tool import info as ossl_info  # noqa: E402
from certmgr.pki import PKI  # noqa: E402

SECRETS_DIR = Path(os.environ.get("CERTADMIN_SECRETS",
                                  _ROOT / "certadmin" / "data" / "secrets"))

_lock = threading.Lock()   # openssl 子进程串行化


def _pki() -> PKI:
    return PKI(SECRETS_DIR)


def _registry_map() -> dict:
    return {c["name"]: c for c in _pki().list_certs()}


def _view(c: dict | None) -> dict | None:
    """登记簿记录 → API 视图：隐藏文件路径，仅保留 hasKey 布尔（私钥存在且未销毁）。"""
    if c is None:
        return None
    c = dict(c)
    files = c.pop("files", None) or {}
    c["hasKey"] = bool(files.get("key")) and Path(files["key"]).exists()
    return c


class IssueDeviceReq(BaseModel):
    name: str
    days: int = 7300          # 默认 20 年


class CsrReq(BaseModel):
    name: str
    csrPem: str
    days: int = 365


class IssueServerReq(BaseModel):
    name: str = "server"
    days: int = 7300
    dns: list[str] = ["localhost", "tb"]
    ips: list[str] = ["127.0.0.1"]


class ImportReq(BaseModel):
    name: str
    certPem: str
    keyPem: str | None = None   # 第三方/厂家已持有私钥时不传，仅登记证书


class DeliverReq(BaseModel):
    destroyKey: bool = False


router = APIRouter(prefix="/api", tags=["certadmin"])


# ---------- 健康与 CA ----------
@router.get("/health")
def health():
    return {"status": "ok", "caInitialized": (SECRETS_DIR / "root" / "ca.pem").exists()}


@router.get("/ca")
def ca_info():
    """根证书/中间证书查看（默认已生成；无私钥导出入口）。"""
    items = _pki().list_certs()
    root = next((c for c in items if c["kind"] == "root-ca"), None)
    inter = next((c for c in items if c["kind"] == "intermediate-ca"), None)
    return {"initialized": root is not None,
            "root": _view(root), "intermediate": _view(inter)}


@router.get("/ca/root-ca.pem")
def download_root_ca():
    p = SECRETS_DIR / "root" / "ca.pem"
    if not p.exists():
        raise HTTPException(404, "根证书不存在")
    return FileResponse(p, media_type="application/x-pem-file", filename="root-ca.pem")


@router.get("/ca/chain.pem")
def download_chain():
    """中间证书+根证书链（供需要完整链的网关/负载均衡使用）。"""
    p = SECRETS_DIR / "intermediate" / "ca-chain.pem"
    if not p.exists():
        raise HTTPException(404, "证书链不存在")
    return FileResponse(p, media_type="application/x-pem-file", filename="ca-chain.pem")


# ---------- 证书新增 ----------
@router.post("/certs/device")
def issue_device(req: IssueDeviceReq):
    """签发设备证书（CN=设备名，平台侧生成密钥对，随交付包导出）。"""
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    try:
        with _lock:
            _pki().issue_device_cert(req.name, days=req.days)
            rec = _registry_map().get(req.name)
        return _view(rec)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e


@router.post("/certs/csr")
def sign_csr(req: CsrReq):
    """代签外部 CSR（私钥不出厂家环境，只回签证书）。"""
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
        f.write(req.csrPem)
        csr_path = f.name
    try:
        with _lock:
            _pki().sign_external_csr(Path(csr_path), req.name, days=req.days)
            rec = _registry_map().get(req.name)
        return _view(rec)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e
    finally:
        os.unlink(csr_path)


@router.post("/certs/server")
def issue_server(req: IssueServerReq):
    """签发 MQTT 服务器证书（SAN 按 TB 主机名/IP 定制，导出后线下装配到 TB）。"""
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    try:
        with _lock:
            _pki().issue_server_cert(SECRETS_DIR / "server", req.name, days=req.days,
                                     dns=req.dns, ips=req.ips)
            rec = _registry_map().get(req.name)
        return _view(rec)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e


@router.post("/certs/import")
def import_cert(req: ImportReq):
    """登记外部已有设备证书（PEM），私钥可选一并入册。"""
    if not req.name or "/" in req.name:
        raise HTTPException(400, "非法名称")
    try:
        with _lock:
            _pki().import_device_cert(req.name, req.certPem, req.keyPem)
            rec = _registry_map().get(req.name)
        return _view(rec)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"{type(e).__name__}: {e}") from e


# ---------- 查看与校验 ----------
@router.get("/certs")
def list_certs():
    with _lock:
        items = _pki().list_certs()
    return [_view(c) for c in items]


@router.get("/certs/{name}")
def cert_detail(name: str):
    reg = _registry_map()
    if name not in reg:
        raise HTTPException(404, f"不存在 {name}")
    info = _view(reg[name])
    cert_file = (reg[name].get("files") or {}).get("cert")
    if cert_file and Path(cert_file).exists():
        if not info.get("issuer"):                     # 旧记录无 issuer，实时从证书读取
            info["issuer"] = ossl_info(Path(cert_file)).get("issuer", "")
        info["certPem"] = Path(cert_file).read_text()
    return info


@router.post("/certs/{name}/verify")
def verify_cert(name: str):
    reg = _registry_map()
    if name not in reg:
        raise HTTPException(404, f"不存在 {name}")
    cert_file = (reg[name].get("files") or {}).get("cert")
    if not cert_file or not Path(cert_file).exists():
        raise HTTPException(404, "证书文件缺失")
    try:
        with _lock:
            return {"name": name, "result": _pki().verify_cert_files(Path(cert_file))}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e)) from e


# ---------- 导出 ----------
@router.get("/certs/{name}/bundle")
def cert_bundle(name: str):
    """导出 zip。设备证书 = 交付包（cert/fullchain/key/root-ca/README）；
    服务器证书 = TB 装配包（server.pem / server-fullchain.pem / server_key.pem / root-ca/README）。"""
    reg = _registry_map()
    if name not in reg:
        raise HTTPException(404, f"不存在 {name}")
    rec = reg[name]
    files = rec.get("files") or {}
    is_server = str(rec.get("kind")) == "server"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        key_p = files.get("key")
        has_key = bool(key_p and Path(key_p).exists())
        if is_server:
            for arc, key in ((f"{name}.pem", "cert"), (f"{name}-fullchain.pem", "fullchain")):
                p = files.get(key)
                if p and Path(p).exists():
                    z.write(p, arc)
            if has_key:
                z.write(key_p, f"{name}_key.pem")
            readme = (
                f"MQTT 服务器证书装配包 - {name}\n"
                f"{'=' * 40}\n"
                f"CN: {rec.get('cn', '')}    序列号: {rec.get('serial', '')}\n"
                f"有效期: {rec.get('notBefore', '')} ~ {rec.get('notAfter', '')}\n"
                f"SAN: DNS={','.join(rec.get('dns', []) or [])} IP={','.join(rec.get('ips', []) or [])}\n"
                f"\n"
                f"文件清单：\n"
                f"  {name}.pem           服务器证书\n"
                f"  {name}-fullchain.pem 服务器证书+中间证书（TB 加载此文件）\n"
                f"  {name}_key.pem       服务器私钥（严禁外传）\n"
                f"  root-ca.pem          平台根证书（分发给设备/模拟器信任）\n"
                f"\n"
                f"TB 装配步骤（线下同步到 TB 主机后）：\n"
                f"  1. 覆盖 deploy/certs/server/ 下同名文件\n"
                f"  2. 确认 deploy/.env：MQTT_SSL_ENABLED=true、\n"
                f"     MQTT_SSL_PEM_CERT=/certs/server/{name}-fullchain.pem、\n"
                f"     MQTT_SSL_PEM_KEY=/certs/server/{name}_key.pem\n"
                f"  3. 重启 TB：docker compose -f deploy/docker-compose.yml up -d --force-recreate thingsboard\n"
            )
        else:
            for arc, key in (("cert.pem", "cert"), ("fullchain.pem", "fullchain")):
                p = files.get(key)
                if p and Path(p).exists():
                    z.write(p, arc)
            if has_key:
                z.write(key_p, "key.pem")
            readme = (
                f"设备证书交付包 - {name}\n"
                f"{'=' * 40}\n"
                f"CN: {rec.get('cn', '')}    序列号: {rec.get('serial', '')}\n"
                f"有效期: {rec.get('notBefore', '')} ~ {rec.get('notAfter', '')}\n"
                f"\n"
                f"文件清单：\n"
                f"  cert.pem      设备证书（含设备公钥）\n"
                f"  fullchain.pem 设备证书+中间证书（设备 TLS 握手时出示）\n"
                + (f"  key.pem       设备私钥（烧录后务必删除传输副本，严禁外传）\n" if has_key
                   else f"  （私钥已销毁/不随包交付，设备侧应已持有私钥）\n")
                + f"  root-ca.pem   平台根证书（设备须信任此证书）\n"
                f"\n"
                f"设备侧对接步骤（厂家）：\n"
                f"  1. 将 key.pem + fullchain.pem 烧录至设备安全存储\n"
                f"  2. 将 root-ca.pem 加入设备信任库\n"
                f"  3. 连接平台 mqtts 端口；平台按 CN 匹配设备名（须先完成 TB X.509 注册）\n"
                f"\n"
                f"模拟器侧对接步骤：\n"
                f"  将 cert.pem/key.pem/root-ca.pem 内容粘贴至「证书导入」页完成导入，"
                f"再在设备管理页注册并连接。\n"
            )
        root_ca = SECRETS_DIR / "root" / "ca.pem"
        if root_ca.exists():
            z.write(root_ca, "root-ca.pem")
        z.writestr("README.txt", readme)
    buf.seek(0)
    suffix = "server-bundle" if is_server else "cert-bundle"
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}-{suffix}.zip"'})


# ---------- 交付台账 ----------
@router.post("/certs/{name}/deliver")
def deliver_cert(name: str, req: DeliverReq):
    """线下交付台账：记录交付时间；destroyKey=true 销毁平台侧私钥（交付完成后推荐）。"""
    try:
        with _lock:
            return _pki().mark_delivered(name, destroy_key=req.destroyKey)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e

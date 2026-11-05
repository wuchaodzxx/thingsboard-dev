"""P7 v1.3 迭代测试公共设施：证书管理工具（certadmin）API、模拟器 v1.3 回归、全流程闭环。

运行方式（仓库根目录，certadmin :19001、模拟器 :19000、TB :18080/:18885 运行中）：
  .venv/bin/python -m pytest tests/p7_v13 -v

结果落盘：tests/results_p7_v13.json（机器可读，含硬件条件与逐用例六要素）。

与 P6（v1.2 存档）的差异：v1.3 证书签发已移至独立证书管理工具（certadmin，:19001），
模拟器仅负责「证书导入 → 注册 → 连接」。本组用例自含证书准备：
  - 通过 certadmin REST 签发/导出，宿主机侧读挂载目录 certadmin/data/ 取证书与私钥；
  - 全部测试数据统一 tc7- 前缀，每用例结束后自动四级清理：
    certadmin 登记簿与文件（registry.json + devices/<n>/ + server/tc7-*）→
    模拟器设备与证书 → TB 设备。用例中途失败也不残留。

所有判定基于真实交互（certadmin REST / 模拟器 REST / TB REST / 真实 TLS 握手）。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import pytest
import requests

WS = Path(__file__).resolve().parents[2]
CERTADMIN = os.environ.get("CERTADMIN_URL", "http://localhost:19001")
SIM = os.environ.get("SIM_URL", "http://localhost:19000")
CA_DATA = WS / "certadmin" / "data"               # 宿主机 certadmin/data ↔ 容器 /data/secrets
CA_REGISTRY = CA_DATA / "registry.json"
TMP = WS / "tests" / "p7_v13" / "_tmp"            # 用例运行期临时产物
TMP.mkdir(parents=True, exist_ok=True)
PREFIX = "tc7-"                                   # 全部测试数据统一前缀（自动清理依据）

sys.path.insert(0, str(WS / "certmgr"))           # 复用 certmgr PKI 库（TBClient/openssl_tool）

RECORDS: list[dict] = []          # 全量用例结果（含文档六要素）
STARTED = time.time()


# ---------------- 结果收集（同 p6 六要素机制） ----------------
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    if rep.when != "call":
        return
    meta = getattr(item, "_p7_meta", {}) or {}
    RECORDS.append({
        "id": meta.get("tcid", item.nodeid.split("::")[-1]),
        "name": meta.get("name", item.name),
        "precondition": meta.get("precondition", ""),
        "steps": meta.get("steps", ""),
        "expected": meta.get("expected", ""),
        "result": "PASS" if rep.passed else ("SKIP" if rep.skipped else "FAIL"),
        "detail": meta.get("detail", ""),
        "metrics": meta.get("metrics", {}),
        "error": "" if rep.passed else str(rep.longrepr)[-800:],
        "durationSec": round(rep.duration, 3),
    })


def pytest_sessionfinish(session, exitstatus):
    if not RECORDS:
        return
    out = WS / "tests" / "results_p7_v13.json"
    payload = {
        "startedAt": STARTED, "endedAt": time.time(),
        "durationSec": round(time.time() - STARTED, 1),
        "hardware": hardware_info(),
        "results": RECORDS,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n→ 结果已写入 {out}")


def hardware_info() -> dict:
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpuCount": os.cpu_count(),
        "python": platform.python_version(),
    }
    try:
        from certmgr.tb_client import TBClient
        tb = TBClient()
        tb.login()
        info["tbVersion"] = tb._req("GET", "/api/system/info").get("version")
    except Exception:
        info["tbVersion"] = "unknown"
    return info


@pytest.fixture
def rec(request):
    """用例内记录六要素与实际结果：rec(tcid=..., name=..., precondition=..., ...)。"""
    def _set(**kw):
        node_meta = getattr(request.node, "_p7_meta", {}) or {}
        node_meta.update(kw)
        request.node._p7_meta = node_meta
    return _set


# ---------------- REST 封装 ----------------
def ca(method: str, path: str, **kw) -> requests.Response:
    """certadmin 原始响应（负例用例需检查 status_code，不 raise）。"""
    return requests.request(method, CERTADMIN + path, timeout=kw.pop("timeout", 30), **kw)


def caj(method: str, path: str, **kw) -> dict:
    r = ca(method, path, **kw)
    if r.status_code >= 400:
        raise AssertionError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def sim(method: str, path: str, **kw) -> requests.Response:
    """模拟器原始响应（负例用例需检查 status_code，不 raise）。"""
    return requests.request(method, SIM + path, timeout=kw.pop("timeout", 30), **kw)


def simj(method: str, path: str, **kw) -> dict:
    r = sim(method, path, **kw)
    r.raise_for_status()
    return r.json()


def wait_for(fn, timeout: float, interval: float = 1.0, desc: str = ""):
    """轮询直至 fn() 返回真值（TB 遥测/状态异步落库，断言一律轮询）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            ok, last = fn()
            if ok:
                return last
        except Exception as e:  # noqa: BLE001
            last = f"poll error: {e}"
        time.sleep(interval)
    raise AssertionError(f"轮询超时({timeout}s) {desc}: last={last}")


# ---------------- TB 夹具 ----------------
@pytest.fixture(scope="session")
def tb():
    from certmgr.tb_client import TBClient
    c = TBClient()
    c.login()
    return c


def tb_dev_id(tb, name: str) -> str:
    d = tb.get_device(name)
    assert d, f"TB 中无设备 {name}"
    return d["id"]["id"]


def tb_active(tb, name: str) -> bool:
    """TB 在线标志（本版本仅 /api/device/info/{id} 含 active）。"""
    return bool(tb.get_device_info(tb_dev_id(tb, name)).get("active"))


# ---------------- certadmin 文件级操作与清理 ----------------
def read_registry() -> dict:
    if not CA_REGISTRY.exists():
        return {"issued": {}}
    return json.loads(CA_REGISTRY.read_text())


_CONTAINER_SECRETS = "/data/secrets/"    # 登记簿由容器写入，files 记录的是容器内路径


def cert_file(name: str, fname: str) -> Path | None:
    """登记簿 files 记录的证书产物文件（容器路径 /data/secrets/... 映射回宿主机 CA_DATA）。"""
    rec = read_registry()["issued"].get(name)
    if not rec:
        return None
    p = (rec.get("files") or {}).get(fname)
    if not p:
        return None
    p = str(p)
    host = CA_DATA / p[len(_CONTAINER_SECRETS):] \
        if p.startswith(_CONTAINER_SECRETS) else Path(p)
    return host if host.exists() else None


def cleanup(tb=None) -> None:
    """四级清理全部 tc7- 前缀测试数据（忽略不存在的项）。

    顺序：TB 设备 → 模拟器设备/证书 → certadmin 登记簿与文件。
    certadmin 进程每次请求都重读 registry.json，宿主机直接改写即时生效。
    """
    # 1) TB 设备（先于模拟器删除，名称来源模拟器设备列表）
    try:
        sim_names = [d["name"] for d in simj("GET", "/api/devices")
                     if str(d["name"]).startswith(PREFIX)]
    except Exception:
        sim_names = []
    if tb is None:
        try:
            from certmgr.tb_client import TBClient
            tb = TBClient()
            tb.login()
        except Exception:
            tb = None
    for n in sim_names:
        try:
            d = tb.get_device(n) if tb else None
            if d:
                tb.delete_device(d["id"]["id"])
        except Exception:
            pass
    # 2) 模拟器设备与证书
    for n in sim_names:
        try:
            simj("DELETE", f"/api/devices/{n}")
        except Exception:
            pass
    try:
        cert_names = [c["name"] for c in simj("GET", "/api/certs")
                      if str(c["name"]).startswith(PREFIX)]
        for n in cert_names:
            simj("DELETE", f"/api/certs/{n}")
    except Exception:
        pass
    # 3) certadmin 登记簿条目 + 设备目录 + 服务器证书文件
    try:
        reg = read_registry()
        gone = [k for k in reg["issued"] if k.startswith(PREFIX)]
        for k in gone:
            reg["issued"].pop(k, None)
            d = CA_DATA / "devices" / k
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        if gone:
            CA_REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=2))
        for p in (CA_DATA / "server").glob(f"{PREFIX}*"):
            p.unlink(missing_ok=True)
        # 孤儿目录兜底：签发失败残留的产物目录（登记簿中无条目，上面的 gone 扫不到）
        for d in (CA_DATA / "devices").glob(f"{PREFIX}*"):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _per_test_cleanup():
    """用例结束后清理本用例全部 tc7- 数据（失败也兜底）；用例前置由 ensure_* 自备。"""
    yield
    cleanup()


@pytest.fixture(scope="session", autouse=True)
def _session_cleanup():
    """整轮前后各兜底清理一次（清历史残留）。"""
    cleanup()
    yield
    cleanup()


# ---------------- 证书与 openssl 工具 ----------------
def root_ca_pem() -> str:
    return ca("GET", "/api/ca/root-ca.pem").text


def bundle_zip(name: str) -> ZipFile:
    """下载交付/装配包并返回内存 ZipFile（调用方负责 namelist/read 断言）。"""
    r = ca("GET", f"/api/certs/{name}/bundle")
    r.raise_for_status()
    import io
    return ZipFile(io.BytesIO(r.content))


def issue_device(name: str, days: int | None = None) -> dict:
    """签发设备证书（days=None 走默认 7300=20 年）。

    宿主机清理 rmtree 设备目录后，macOS Docker 挂载的目录缓存可能短暂滞后
    （容器 mkdir 命中陈旧缓存 → openssl 写文件 ENOENT → 500），隔 1.5s 重试一次。
    """
    body = {"name": name} if days is None else {"name": name, "days": days}
    try:
        return caj("POST", "/api/certs/device", json=body)
    except AssertionError as e:
        if "No such file" not in str(e):
            raise
        time.sleep(1.5)
        return caj("POST", "/api/certs/device", json=body)


def ensure_device_cert(name: str, days: int = 7300) -> dict:
    """取在册设备证书（无则签发）—— 用例自含前置，支持单独运行。"""
    if name not in read_registry()["issued"]:
        issue_device(name, days)
    return caj("GET", f"/api/certs/{name}")


def cert_days(pem_path: Path) -> int:
    """证书 notBefore→notAfter 自然日差（校验签发有效期，如 7300=20 年）。"""
    info = _pem_info(pem_path)
    fmt = "%b %d %H:%M:%S %Y %Z"
    nb = datetime.strptime(info["notBefore"], fmt)
    na = datetime.strptime(info["notAfter"], fmt)
    return (na - nb).days


def _pem_info(pem_path: Path) -> dict:
    from certmgr.openssl_tool import info as ossl_info
    return ossl_info(Path(pem_path))


def pem_to_der(pem_text: str) -> bytes:
    """PEM → DER（与 TB X.509 凭据 base64(DER) 逐字节比对用）。"""
    r = subprocess.run(["openssl", "x509", "-outform", "DER"],
                       input=pem_text.encode(), capture_output=True, check=True)
    return r.stdout


def x509_text(pem_path: Path) -> str:
    """openssl x509 -text 全文（断言 SAN 等）。"""
    return subprocess.check_output(
        ["openssl", "x509", "-in", str(pem_path), "-noout", "-text"]).decode()


def local_key_csr(dir_path: Path, cn: str) -> tuple[Path, Path]:
    """本地生成密钥对与 CSR（模拟厂家环境：私钥不出外部）。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    key, csr = dir_path / "key.pem", dir_path / "req.csr"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                    "-out", str(key)], check=True)
    subprocess.run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr),
                    "-subj", f"/CN={cn}/O=VPP-Lab"], check=True)
    return key, csr


def local_self_signed(dir_path: Path, cn: str) -> tuple[Path, Path]:
    """本地生成自签证书+私钥（模拟第三方 CA 签发的外部证书导入场景）。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    key, cert = dir_path / "key.pem", dir_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec",
                    "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "365",
                    "-subj", f"/CN={cn}/O=VPP-Lab"], check=True)
    return cert, key


def bring_up_device(tb, name: str, dev_type: str = "pv", cap_kw: float = 20) -> dict:
    """v1.3 完整接入流水线：签发→校验→导入→建档→注册→连接→TB 上线→首轮遥测。

    返回 {"certPem", "devId", "telemetry"}；任意环节失败即抛 AssertionError。
    """
    issue_device(name)
    r = caj("POST", f"/api/certs/{name}/verify")
    assert ": OK" in r["result"], f"链校验失败: {r}"
    cert_pem = cert_file(name, "cert").read_text()
    key_pem = cert_file(name, "key").read_text()
    imp = simj("POST", "/api/certs/import",
               json={"name": name, "certPem": cert_pem, "keyPem": key_pem,
                     "caPem": root_ca_pem()})
    assert imp["keyImported"] is True, f"导入异常: {imp}"
    simj("POST", "/api/devices", json={"name": name, "devType": dev_type,
                                       "capKw": cap_kw, "autoConnect": False})
    reg = simj("POST", f"/api/devices/{name}/register")
    assert reg["register"] == "tb-registered", f"注册异常: {reg}"
    simj("POST", f"/api/devices/{name}/connect")
    wait_for(lambda: (tb_active(tb, name), None), 60, desc=f"{name} TB 上线")
    dev_id = tb_dev_id(tb, name)

    def _ts_ready():
        t = tb.get_latest_timeseries(dev_id, "p")
        rows = t.get("p") or []
        return (bool(rows) and rows[0]["value"] is not None, t)
    telemetry = wait_for(_ts_ready, 150, interval=5, desc=f"{name} 首轮遥测入库")
    return {"certPem": cert_pem, "devId": dev_id, "telemetry": telemetry}

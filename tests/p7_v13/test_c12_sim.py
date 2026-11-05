"""C12 模拟器 v1.3 适配回归用例。

覆盖 v1.3 迭代对模拟器的行为变更：证书签发管理移出（旧端点下线）、设备创建不再自动注册、
证书仅支持导入（对应 certadmin 交付包）、注册依赖已导入证书。

前置：模拟器 :19000 与 TB :18080 运行中；certadmin :19001 运行中（TC-SIM-03 需取交付包）。
运行：.venv/bin/python -m pytest tests/p7_v13/test_c12_sim.py -v
"""
from __future__ import annotations

import time

from conftest import (PREFIX, bundle_zip, cert_file, caj, ensure_device_cert,
                      root_ca_pem, sim, simj)


def test_sim01_legacy_endpoints_retired(rec):
    rec(tcid="TC-SIM-01", name="旧证书签发端点下线：POST /api/certs/device|csr 均拒绝",
        precondition="模拟器 :19000 运行中（v1.3）",
        steps="1) POST /api/certs/device（v1.2 签发入口）；2) POST /api/certs/csr（v1.2 代签入口）",
        expected="两个旧签发端点均 405 Method Not Allowed（路径仍在但仅 GET/DELETE 语义）",
        )
    r1 = sim("POST", "/api/certs/device", json={"name": PREFIX + "sim01", "days": 365})
    assert r1.status_code == 405, f"旧签发端点应 405: {r1.status_code}"
    r2 = sim("POST", "/api/certs/csr", json={"name": PREFIX + "sim01", "csrPem": "junk"})
    assert r2.status_code == 405, f"旧代签端点应 405: {r2.status_code}"
    rec(detail=f"POST /api/certs/device→{r1.status_code}，POST /api/certs/csr→{r2.status_code}；"
               "签发已移交 certadmin")


def test_sim02_register_requires_imported_cert(rec, tb):
    rec(tcid="TC-SIM-02", name="未导入证书注册拒绝：register 返回 400 且不建 TB 档",
        precondition="模拟器与 TB 运行中",
        steps="1) 创建设备（不连接）；2) 直接 POST /api/devices/{name}/register；3) 查 TB 设备",
        expected="注册被拒（400，提示先导入证书）；TB 无同名设备",
        )
    name = "tc7-sim02"
    r = simj("POST", "/api/devices", json={"name": name, "devType": "pv",
                                           "capKw": 10, "autoConnect": False})
    assert r["created"] == name, f"创建失败: {r}"
    reg = sim("POST", f"/api/devices/{name}/register")
    assert reg.status_code == 400, f"未导入证书注册应 400: {reg.status_code}"
    assert "未导入" in reg.text, f"错误提示应指向导入证书: {reg.text}"
    assert tb.get_device(name) is None, "注册被拒后 TB 不应有同名设备"
    rec(detail=f"register→400（{reg.json().get('detail', '')[:40]}…），TB 无建档")


def test_sim03_import_validate_and_delete(rec):
    rec(tcid="TC-SIM-03", name="证书导入：非法 PEM 拒绝、合法导入回读、删除后 404",
        precondition="模拟器与 certadmin 运行中（导入素材取自 certadmin 交付包）",
        steps="1) POST /api/certs/import 垃圾 PEM（应 400）；2) certadmin 签发 tc7-sim03 并取 "
              "cert/key/root-ca；3) 合法导入；4) GET 列表与详情回读；5) GET /api/certs/ca 信任根状态；"
              "6) DELETE 后再 GET",
        expected="非法 PEM 400；导入 keyImported=true 且 cn 正确；回读一致；信任根 CN=VPP Root CA；"
                "删除成功且详情 404",
        )
    name = "tc7-sim03"
    bad = sim("POST", "/api/certs/import", json={"name": name, "certPem": "not-a-pem"})
    assert bad.status_code == 400, f"非法 PEM 应 400: {bad.status_code}"
    ensure_device_cert(name)                      # certadmin 侧在册（供取证书）
    cert_pem = cert_file(name, "cert").read_text()
    key_pem = cert_file(name, "key").read_text()
    imp = simj("POST", "/api/certs/import",
               json={"name": name, "certPem": cert_pem, "keyPem": key_pem,
                     "caPem": root_ca_pem()})
    assert imp["keyImported"] is True and imp["cn"] == name, f"导入异常: {imp}"
    assert imp["caImported"] is True, f"信任根导入失败: {imp}"
    d = simj("GET", f"/api/certs/{name}")
    assert d["hasKey"] is True and d["serial"] == imp["serial"], f"详情回读不符: {d}"
    lst = {c["name"] for c in simj("GET", "/api/certs")}
    assert name in lst, f"列表缺 {name}"
    ca_st = simj("GET", "/api/certs/ca")
    assert ca_st["present"] and ca_st["cn"] == "VPP Root CA", f"信任根状态异常: {ca_st}"
    dele = simj("DELETE", f"/api/certs/{name}")
    assert dele["deleted"] == name, f"删除异常: {dele}"
    assert sim("GET", f"/api/certs/{name}").status_code == 404, "删除后详情应 404"
    rec(detail=f"非法 PEM→400；导入 cn={imp['cn']} keyImported/caImported=true；"
               "回读一致；信任根 VPP Root CA；删除后 404")


def test_sim04_no_auto_register(rec, tb):
    rec(tcid="TC-SIM-04", name="创建设备不再自动注册：v1.2 载荷兼容但不触发签发/注册",
        precondition="模拟器与 TB 运行中",
        steps="1) POST /api/devices 携带 v1.2 风格 autoRegister=true 载荷（autoConnect=false）；"
              "2) 等待 3s；3) 查 TB 设备与模拟器设备状态",
        expected="创建成功（多余字段被忽略）；无自动 TB 注册；设备未连接",
        )
    name = "tc7-sim04"
    r = simj("POST", "/api/devices", json={"name": name, "devType": "pv", "capKw": 10,
                                           "autoRegister": True, "autoConnect": False})
    assert r["created"] == name, f"创建失败: {r}"
    time.sleep(3)                                 # 给潜在的自动流水线留出暴露窗口
    assert tb.get_device(name) is None, "v1.3 不应自动注册 TB 设备"
    devs = {d["name"]: d for d in simj("GET", "/api/devices")}
    assert name in devs and not devs[name]["connected"], f"设备状态异常: {devs.get(name)}"
    rec(detail="autoRegister 载荷被忽略（Pydantic 默认行为）；TB 无建档、设备未连接；"
               "注册须显式 POST /register（依赖已导入证书）")

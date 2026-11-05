"""C13 v1.3 全流程闭环用例：证书线下同步场景的端到端验证。

模拟真实离线交付：certadmin 签发/导出 → （线下同步）→ 模拟器导入 → 注册 → TLS 在线 → 遥测 →
交付销钥 → 销钥后设备凭存量证书持续运行。另验证 TB 侧服务器证书装配与信任链。

前置：certadmin :19001、模拟器 :19000、TB :18080 运行中；TB mqtts :18885 已启用
      certadmin 签发的服务器证书（docs/03 §4.2 装配流程已完成）。
运行：.venv/bin/python -m pytest tests/p7_v13/test_c13_flow.py -v
"""
from __future__ import annotations

import base64
import socket
import ssl
import subprocess

from conftest import (TMP, _pem_info, bundle_zip, bring_up_device, caj,
                      cert_file, root_ca_pem, simj, tb_active, tb_dev_id,
                      wait_for)


def test_flow01_tb_server_cert_trust(rec):
    rec(tcid="TC-FLOW-01", name="TB 服务器证书就绪：TLS 证书由 certadmin CA 签发且与登记一致",
        precondition="TB mqtts :18885 已装配 certadmin 导出的服务器证书（docs/03 §4.2）",
        steps="1) 真实 TLS 握手 localhost:18885 取服务器证书；2) 解析签发者与序列号；"
              "3) 与 certadmin 登记簿 server 记录比对；4) POST /api/certs/server/verify 校验链",
        expected="握手成功（TLS 1.2+）；签发者=VPP Intermediate CA 01；序列号与登记一致；verify OK",
        )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE          # 仅取证书元数据；信任链由步骤 3/4 校验
    with socket.create_connection(("localhost", 18885), timeout=15) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as s:
            der = s.getpeercert(binary_form=True)
            tls_ver = s.version()
    assert tls_ver and tls_ver >= "TLSv1.2", f"TLS 版本过低: {tls_ver}"
    der_f, pem_f = TMP / "peer.der", TMP / "peer.pem"
    der_f.write_bytes(der)
    subprocess.run(["openssl", "x509", "-inform", "DER", "-outform", "PEM",
                    "-in", str(der_f), "-out", str(pem_f)], check=True)
    info = _pem_info(pem_f)
    assert "VPP Intermediate CA 01" in info["issuer"], f"服务器证书签发者不符: {info['issuer']}"
    srv = caj("GET", "/api/certs/server")
    assert srv and srv["kind"] == "server", f"登记簿缺 server 记录: {srv}"
    assert info["serial"] == srv["serial"], \
        f"TB 在用证书与登记不一致: {info['serial']} vs {srv['serial']}"
    v = caj("POST", "/api/certs/server/verify")
    assert ": OK" in v["result"], f"服务器证书链校验失败: {v}"
    rec(detail=f"TLS {tls_ver}；issuer={info['issuer']}；serial 与登记一致（{srv['serial'][:16]}…）；"
               f"verify OK", metrics={"tlsVersion": tls_ver})


def test_flow02_offline_sync_full_chain(rec, tb):
    rec(tcid="TC-FLOW-02", name="设备证书线下同步闭环：签发→导出→导入→注册→TLS 上线→遥测→凭据绑定",
        precondition="certadmin/模拟器/TB 运行中；TB 信任 certadmin 根证书（TC-FLOW-01 环境）",
        steps="1) certadmin 签发 tc7-flow02 并链校验；2) 读交付包内容（cert/key/root-ca）；"
              "3) 模拟器导入；4) 创建设备并注册；5) 连接等 TB active；6) 读 TB X.509 凭据并与"
              "签发证书 DER 逐字节比对；7) 等 TB 首轮遥测 p 入库",
        expected="注册 tb-registered；TB active；凭据类型 X509_CERTIFICATE 且值与签发证书一致；"
                "遥测 p 有值",
        )
    name = "tc7-flow02"
    issue = caj("POST", "/api/certs/device", json={"name": name})    # 默认 20 年
    assert issue["hasKey"] is True, f"签发异常: {issue}"
    v = caj("POST", f"/api/certs/{name}/verify")
    assert ": OK" in v["result"], f"链校验失败: {v}"
    z = bundle_zip(name)                     # 模拟线下导出交付包
    assert set(z.namelist()) >= {"cert.pem", "key.pem", "root-ca.pem"}, f"交付包异常: {z.namelist()}"
    cert_pem = z.read("cert.pem").decode()   # 模拟厂家拿到交付包后向模拟器导入
    key_pem = z.read("key.pem").decode()
    ca_pem = z.read("root-ca.pem").decode()
    assert ca_pem.strip() == root_ca_pem().strip(), "交付包根证书与平台导出不一致"
    imp = simj("POST", "/api/certs/import",
               json={"name": name, "certPem": cert_pem, "keyPem": key_pem, "caPem": ca_pem})
    assert imp["keyImported"] is True and imp["cn"] == name, f"导入异常: {imp}"
    simj("POST", "/api/devices", json={"name": name, "devType": "pv",
                                       "capKw": 30, "autoConnect": False})
    reg = simj("POST", f"/api/devices/{name}/register")
    assert reg["register"] == "tb-registered", f"注册异常: {reg}"
    simj("POST", f"/api/devices/{name}/connect")
    wait_for(lambda: (tb_active(tb, name), None), 60, desc=f"{name} TB 上线")
    dev_id = tb_dev_id(tb, name)
    cred = tb.get_credentials(dev_id)
    assert cred["credentialsType"] == "X509_CERTIFICATE", f"凭据类型异常: {cred['credentialsType']}"
    want_der = subprocess.run(["openssl", "x509", "-outform", "DER"],
                              input=cert_pem.encode(), capture_output=True, check=True).stdout
    val = cred["credentialsValue"]
    matched = False
    try:                                     # TB 标准存法：base64(DER)
        matched = base64.b64decode(val, validate=True) == want_der
    except Exception:
        matched = False
    if not matched:                          # 兼容直接存 PEM 的存法
        matched = val.strip() == cert_pem.strip()
    assert matched, "TB X.509 凭据与签发证书不一致（绑定错误）"

    def _ts_ready():
        t = tb.get_latest_timeseries(dev_id, "p")
        rows = t.get("p") or []
        return (bool(rows) and rows[0]["value"] is not None, t)
    telemetry = wait_for(_ts_ready, 150, interval=5, desc=f"{name} 首轮遥测入库")
    p = float(telemetry["p"][0]["value"])
    rec(detail=f"签发→交付包导入→注册 {reg['register']}→active；凭据 base64(DER) 与签发证书一致；"
               f"遥测 p={p} kW", metrics={"p": p, "days": 7300})


def test_flow03_deliver_destroy_keep_online(rec, tb):
    rec(tcid="TC-FLOW-03", name="交付销钥后设备持续在线：仅凭存量证书运行",
        precondition="certadmin/模拟器/TB 运行中（用例自备 tc7-flow03 全链接入）",
        steps="1) 完整接入流水线建立 tc7-flow03 在线并记录遥测基线；"
              "2) POST /api/certs/tc7-flow03/deliver {destroyKey:true}；"
              "3) 重新下载 bundle 断言无私钥；4) 等待下一轮遥测入库且 TB active",
        expected="keyDestroyed=true 且交付包无 key.pem；设备不重连仍持续上报遥测、保持在线",
        )
    name = "tc7-flow03"
    state = bring_up_device(tb, name, dev_type="pv", cap_kw=20)
    dev_id = state["devId"]
    base_ts = int(state["telemetry"]["p"][0]["ts"])
    r = caj("POST", f"/api/certs/{name}/deliver", json={"destroyKey": True})
    assert r["keyDestroyed"] is True, f"销毁失败: {r}"
    assert not cert_file(name, "key"), "销毁后平台侧仍可读到私钥文件"
    z = bundle_zip(name)
    assert "key.pem" not in z.namelist(), f"销毁后交付包仍含私钥: {z.namelist()}"
    assert "已销毁" in z.read("README.txt").decode(), "README 应说明私钥已销毁"

    def _new_point():
        t = tb.get_latest_timeseries(dev_id, "p")
        rows = t.get("p") or []
        ok = bool(rows) and int(rows[0]["ts"]) > base_ts and rows[0]["value"] is not None
        return (ok, int(rows[0]["ts"]) if rows else None)
    ts = wait_for(_new_point, 150, interval=5, desc="销钥后新一轮遥测入库")
    wait_for(lambda: (tb_active(tb, name), None), 15, desc="销钥后保持在线")
    rec(detail=f"keyDestroyed=true，销毁后交付包={z.namelist()}；销钥后新遥测 ts={ts}（>基线 "
               f"{base_ts}）且 TB active——设备凭存量证书持续运行", metrics={"newTelemetryTs": ts})

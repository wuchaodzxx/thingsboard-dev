"""P6 C1 设备注册和认证（3 例）。

前置条件：TB(:18080/:18885) 与模拟器(:19000)运行中，root+中间 CA 证书体系已初始化。
"""
from __future__ import annotations

import base64
import time

import pytest

from conftest import SECRETS, TMP, make_device, purge_devices, sim_req, tb_dev_id
from certmgr.openssl_tool import run as openssl_run

TYPE = pytest.mark.functional


@TYPE
def test_c1_01_cert_issue_and_chain(rec):
    """注册即签发：创建设备自动签发 X.509 证书且链校验通过。"""
    name = f"p6-c1-{int(time.time())}"
    rec(tcid="TC-C1-01", name="设备注册：证书签发与证书链校验",
        precondition="TB/模拟器运行中，root+intermediate CA 已初始化",
        steps="POST /api/devices(autoRegister=true) → GET /api/certs/{name} → POST /api/certs/{name}/verify",
        expected="证书自动签发成功；openssl verify 返回 OK")
    try:
        t0 = time.time()
        d = make_device(name, "pv", 50, connect=False)
        cert = sim_req("GET", f"/api/certs/{name}")
        v = sim_req("POST", f"/api/certs/{name}/verify")
        assert cert.get("certPem", "").startswith("-----BEGIN CERTIFICATE"), "证书 PEM 缺失"
        assert "OK" in v.get("result", ""), f"链校验失败: {v}"
        rec(detail=f"签发+登记 {(time.time()-t0)*1000:.0f}ms，serial={cert.get('serial')}，"
                    f"verify={v['result'].strip().splitlines()[-1]}")
    finally:
        purge_devices(_tb_lazy(), [name])


@TYPE
def test_c1_02_tb_register_x509(rec, tb):
    """TB 注册：设备存在且凭据类型为 X509_CERTIFICATE（双向 TLS 唯一证据）。"""
    name = f"p6-c1-{int(time.time())}"
    rec(tcid="TC-C1-02", name="设备注册：TB 注册与 X.509 凭据绑定",
        precondition="同 TC-C1-01",
        steps="创建设备(autoRegister) → TB GET /api/tenant/devices?deviceName → GET credentials",
        expected="TB 中存在同名设备且 credentialsType=X509_CERTIFICATE，"
                 "凭据值与设备证书一致（TB 3.5.1 以 base64(DER) 存储）")
    try:
        make_device(name, "pv", 50, connect=False)
        d = tb.get_device(name)
        assert d, "TB 中未注册设备"
        cred = tb.get_credentials(d["id"]["id"])
        assert cred["credentialsType"] == "X509_CERTIFICATE", cred["credentialsType"]
        # TB 存储的凭据值为 base64 编码的证书 DER；与模拟器签发证书逐字节比对
        tb_der = base64.b64decode(cred["credentialsValue"])
        sim_pem = SECRETS / "devices" / name / "cert.pem"
        assert sim_pem.exists(), f"设备证书文件缺失 {sim_pem}"
        tmp_der = TMP / f"{name}.der"
        openssl_run(["x509", "-outform", "DER", "-in", str(sim_pem),
                     "-out", str(tmp_der)])
        assert tb_der == tmp_der.read_bytes(), "TB 凭据 DER 与设备证书不一致"
        assert tb_dev_id(tb, name) == d["id"]["id"]
        rec(detail=f"TB deviceId={d['id']['id']}, credentialsType=X509_CERTIFICATE, "
                   f"凭据 DER 与签发证书一致")
    finally:
        purge_devices(tb, [name])


@TYPE
def test_c1_03_reregister_idempotent(rec, tb):
    """重复注册幂等：同名设备重复创建复用证书与 TB 设备，不产生重复。"""
    name = f"p6-c1-{int(time.time())}"
    rec(tcid="TC-C1-03", name="设备注册：重复创建幂等性",
        precondition="同 TC-C1-01，且该设备已注册一次",
        steps="再次 POST /api/devices 同名设备 → 对比证书 serial 与 TB deviceId",
        expected="复用已有证书（serial 不变）与同一 TB 设备，请求成功无报错")
    try:
        make_device(name, "pv", 50, connect=False)
        s1 = sim_req("GET", f"/api/certs/{name}").get("serial")
        id1 = tb_dev_id(tb, name)
        make_device(name, "pv", 50, connect=False)          # 重复创建
        s2 = sim_req("GET", f"/api/certs/{name}").get("serial")
        id2 = tb_dev_id(tb, name)
        assert s1 == s2, f"证书 serial 变化 {s1}→{s2}"
        assert id1 == id2, f"TB deviceId 变化 {id1}→{id2}"
        rec(detail=f"serial={s1} 不变，deviceId={id1[:8]}… 不变（幂等复用）")
    finally:
        purge_devices(tb, [name])


def _tb_lazy():
    from certmgr.tb_client import TBClient
    c = TBClient()
    c.login()
    return c

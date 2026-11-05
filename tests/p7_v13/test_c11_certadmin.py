"""C11 证书管理工具 certadmin API 用例（v1.3）。

覆盖 v1.3 功能迭代核心：CA 首启自动生成（仅查看/导出）、证书新增（签发/CSR 代签/导入）、
查看（列表/详情/校验）、导出（设备交付包/服务器装配包）、交付台账与私钥销毁、参数与资源负例。

前置：certadmin :19001 运行中（数据目录 certadmin/data/ 宿主机可读，容器内挂载为 /data/secrets）。
运行：.venv/bin/python -m pytest tests/p7_v13/test_c11_certadmin.py -v
"""
from __future__ import annotations

from conftest import (CA_DATA, TMP, _pem_info, bundle_zip, caj, ca, cert_days,
                      cert_file, ensure_device_cert, issue_device,
                      local_key_csr, local_self_signed, x509_text)


# ---------------- CA 自动生成与查看 ----------------
def test_ca01_init_and_health(rec):
    rec(tcid="TC-CA-01", name="CA 首启自动生成：健康检查与根/中间证书查看",
        precondition="certadmin :19001 运行中，CA 已初始化（首启幂等生成）",
        steps="1) GET /api/health；2) GET /api/ca 查看根/中间证书",
        expected="caInitialized=true；root/intermediate 均在册；视图不含 files 文件路径（私钥路径不外露）",
        )
    h = caj("GET", "/api/health")
    assert h["status"] == "ok" and h["caInitialized"] is True, f"健康检查异常: {h}"
    info = caj("GET", "/api/ca")
    assert info["initialized"] is True, f"CA 未初始化: {info}"
    root, inter = info["root"], info["intermediate"]
    assert root and root["kind"] == "root-ca" and root["cn"] == "VPP Root CA", f"根证书异常: {root}"
    assert inter and inter["kind"] == "intermediate-ca" \
        and inter["cn"] == "VPP Intermediate CA 01", f"中间证书异常: {inter}"
    for c in (root, inter):
        assert c["serial"] and c["notAfter"], f"证书元数据缺失: {c}"
        assert "files" not in c, f"视图泄露文件路径: {c}"     # 私钥/文件路径不外露
    rec(detail=f"root serial={root['serial']} notAfter={root['notAfter']}；"
               f"intermediate cn={inter['cn']}；files 字段未外露",
        metrics={"rootSerial": root["serial"]})


def test_ca02_export_root_and_chain(rec):
    rec(tcid="TC-CA-02", name="根证书/证书链导出（仅导出，无私钥下载入口）",
        precondition="certadmin :19001 运行中",
        steps="1) GET /api/ca/root-ca.pem；2) GET /api/ca/chain.pem；3) openssl 解析两文件",
        expected="根证书为 PEM 证书且不含私钥；链文件含中间+根两段证书",
        )
    root_pem = ca("GET", "/api/ca/root-ca.pem")
    assert root_pem.status_code == 200, f"根证书导出失败: {root_pem.status_code}"
    assert "BEGIN CERTIFICATE" in root_pem.text, "非 PEM 证书"
    assert "PRIVATE KEY" not in root_pem.text, "根证书响应泄露私钥"
    (TMP / "root-ca.pem").write_text(root_pem.text)
    ri = _pem_info(TMP / "root-ca.pem")
    assert ri["cn"] == "VPP Root CA", f"CN 不符: {ri}"
    chain = ca("GET", "/api/ca/chain.pem")
    assert chain.status_code == 200, f"证书链导出失败: {chain.status_code}"
    n_blocks = chain.text.count("BEGIN CERTIFICATE")
    assert n_blocks == 2, f"链应含中间+根两段证书: {n_blocks}"
    (TMP / "chain.pem").write_text(chain.text)
    ci = _pem_info(TMP / "chain.pem")
    assert ci["cn"] == "VPP Intermediate CA 01", f"链首应为中间证书: {ci}"
    rec(detail=f"root-ca.pem CN={ri['cn']}；chain.pem 两段（首段 {ci['cn']}），无私钥泄露")


# ---------------- 证书新增：签发 / CSR / 导入 ----------------
def test_ca03_issue_device_default_20y(rec):
    rec(tcid="TC-CA-03", name="设备证书签发：默认有效期 20 年 + 同名重签覆盖",
        precondition="certadmin :19001 运行中，CA 链正常",
        steps="1) POST /api/certs/device 不带 days（走默认 7300）；2) 解析 notBefore→notAfter 日差；"
              "3) 同名再签发 days=365，比对序列号与有效期",
        expected="默认签发有效期 7300 天（20 年）；重签成功且序列号更新（覆盖语义）、有效期 365 天",
        )
    name = "tc7-ca03"
    d1 = issue_device(name)                       # 默认 days
    assert d1["name"] == name and d1["serial"], f"签发返回异常: {d1}"
    assert d1["hasKey"] is True, f"平台签发应持有私钥: {d1}"
    days1 = cert_days(CA_DATA / "devices" / name / "cert.pem")
    serial1 = d1["serial"]
    d2 = issue_device(name, days=365)             # 同名重签（覆盖）
    days2 = cert_days(CA_DATA / "devices" / name / "cert.pem")
    assert abs(days1 - 7300) <= 1, f"默认有效期应为 20 年(7300 天): {days1}"
    assert days2 == 365, f"显式 days=365 未生效: {days2}"
    assert d2["serial"] != serial1, f"重签序列号应更新: {serial1} -> {d2['serial']}"
    rec(detail=f"默认签发 {days1} 天（20 年）；重签 365 天 serial {serial1[:12]}…→{d2['serial'][:12]}…",
        metrics={"defaultDays": days1, "reissueDays": days2})


def test_ca04_detail_and_list_view(rec):
    rec(tcid="TC-CA-04", name="证书查看：详情（PEM+签发者）与列表（hasKey 布尔，不泄露路径）",
        precondition="certadmin :19001 运行中（用例自备在册设备证书）",
        steps="1) ensure 在册证书 tc7-ca03；2) GET /api/certs/tc7-ca03 详情；3) GET /api/certs 列表",
        expected="详情含 certPem 与 issuer=VPP Intermediate CA 01；列表项含 hasKey 且无 files 路径",
        )
    name = "tc7-ca03"
    ensure_device_cert(name)
    d = caj("GET", f"/api/certs/{name}")
    assert "BEGIN CERTIFICATE" in d.get("certPem", ""), "详情缺 certPem"
    assert "VPP Intermediate CA 01" in d.get("issuer", ""), f"签发者不符: {d.get('issuer')}"
    assert "PRIVATE KEY" not in d.get("certPem", ""), "详情不应包含私钥"
    items = {c["name"]: c for c in caj("GET", "/api/certs")}
    assert name in items, f"列表缺 {name}"
    it = items[name]
    assert isinstance(it["hasKey"], bool), f"hasKey 应为布尔: {it}"
    assert "files" not in it, f"列表泄露文件路径: {it}"
    assert {"VPP Root CA", "VPP Intermediate CA 01", "server"} <= set(items), \
        f"列表应含 CA 与服务器证书: {sorted(items)}"
    rec(detail=f"issuer={d['issuer']}；列表 {len(items)} 项含 CA/服务器/设备，无路径泄露")


def test_ca05_verify_chain(rec):
    rec(tcid="TC-CA-05", name="证书链校验：openssl verify 通过中间+根信任链",
        precondition="certadmin :19001 运行中（用例自备在册设备证书）",
        steps="1) ensure 在册证书 tc7-ca03；2) POST /api/certs/tc7-ca03/verify",
        expected="校验结果含 ': OK'（证书链可信）",
        )
    name = "tc7-ca03"
    ensure_device_cert(name)
    r = caj("POST", f"/api/certs/{name}/verify")
    assert ": OK" in r["result"], f"链校验失败: {r}"
    rec(detail=f"verify={r['result']}")


def test_ca06_sign_external_csr(rec):
    rec(tcid="TC-CA-06", name="CSR 代签：私钥不出厂家环境（平台只回签证书）",
        precondition="certadmin :19001 运行中",
        steps="1) openssl 本地生成密钥对+CSR；2) POST /api/certs/csr 代签；"
              "3) 检查 hasKey 与平台侧目录无 key.pem；4) 链校验；5) 交付包无 key.pem",
        expected="签发成功 hasKey=false；平台侧无私钥文件；verify OK；bundle 不含 key.pem",
        )
    name = "tc7-ca06"
    key, csr = local_key_csr(TMP / name, name)
    r = caj("POST", "/api/certs/csr", json={"name": name, "csrPem": csr.read_text(), "days": 365})
    assert r["name"] == name, f"代签返回异常: {r}"
    assert r["hasKey"] is False, f"CSR 签发不应有平台私钥: {r}"
    assert cert_file(name, "key") is None, "平台侧不应存在 key.pem"
    v = caj("POST", f"/api/certs/{name}/verify")
    assert ": OK" in v["result"], f"代签证书链校验失败: {v}"
    z = bundle_zip(name)
    assert "key.pem" not in z.namelist(), f"CSR 交付包不应含私钥: {z.namelist()}"
    rec(detail=f"代签 {name}（days=365）hasKey=false，verify OK，交付包={z.namelist()}")


def test_ca07_import_external_cert(rec):
    rec(tcid="TC-CA-07", name="外部证书导入登记：第三方证书+私钥入册",
        precondition="certadmin :19001 运行中",
        steps="1) openssl 本地生成自签证书+私钥（模拟第三方 CA 签发）；"
              "2) POST /api/certs/import 带 certPem+keyPem；3) 详情回读",
        expected="导入成功 hasKey=true；详情 certPem 与导入内容一致",
        )
    name = "tc7-ca07"
    cert, key = local_self_signed(TMP / name, name)
    cert_pem, key_pem = cert.read_text(), key.read_text()
    r = caj("POST", "/api/certs/import",
            json={"name": name, "certPem": cert_pem, "keyPem": key_pem})
    assert r["hasKey"] is True, f"导入后应持有私钥: {r}"
    d = caj("GET", f"/api/certs/{name}")
    assert d["certPem"].strip() == cert_pem.strip(), "详情 certPem 与导入不一致"
    assert d["hasKey"] is True, f"详情 hasKey 异常: {d}"
    rec(detail=f"导入 {name}（第三方自签）cn={d['cn']}，私钥入册，certPem 回读一致")


def test_ca08_issue_server_san(rec):
    rec(tcid="TC-CA-08", name="服务器证书签发：SAN 按 TB 主机定制",
        precondition="certadmin :19001 运行中",
        steps="1) POST /api/certs/server {name=tc7-srv, dns=[localhost,thingsboard], ips=[127.0.0.1]}；"
              "2) 详情回读 dns/ips；3) openssl x509 -text 解析 SAN",
        expected="kind=server；登记 dns/ips 与请求一致；证书 SAN 含 DNS:thingsboard 与 IP:127.0.0.1",
        )
    name = "tc7-srv"
    r = caj("POST", "/api/certs/server",
            json={"name": name, "days": 365, "dns": ["localhost", "thingsboard"],
                  "ips": ["127.0.0.1"]})
    assert r["kind"] == "server", f"kind 异常: {r}"
    assert sorted(r["dns"]) == ["localhost", "thingsboard"], f"dns 回读不符: {r}"
    assert r["ips"] == ["127.0.0.1"], f"ips 回读不符: {r}"
    pem = cert_file(name, "cert")
    text = x509_text(pem)
    assert "DNS:thingsboard" in text, f"SAN 缺 DNS:thingsboard:\n{text[-600:]}"
    assert "IP Address:127.0.0.1" in text, f"SAN 缺 IP Address:\n{text[-600:]}"
    rec(detail=f"服务器证书 {name} SAN=DNS:localhost,DNS:thingsboard,IP:127.0.0.1",
        metrics={"dns": r["dns"], "ips": r["ips"]})


# ---------------- 导出 ----------------
def test_ca09_device_bundle(rec):
    rec(tcid="TC-CA-09", name="导出设备交付包：cert/fullchain/key/root-ca/README",
        precondition="certadmin :19001 运行中（用例自备持有私钥的在册证书）",
        steps="1) ensure 在册证书 tc7-ca03；2) GET /api/certs/tc7-ca03/bundle 下载 zip；"
              "3) 断言包内清单与内容",
        expected="含 cert.pem/fullchain.pem/key.pem/root-ca.pem/README.txt；"
                "cert.pem 与详情一致；fullchain 含设备+中间两段；README 含烧录指引",
        )
    name = "tc7-ca03"
    ensure_device_cert(name)
    z = bundle_zip(name)
    names = set(z.namelist())
    assert {"cert.pem", "fullchain.pem", "key.pem", "root-ca.pem", "README.txt"} <= names, \
        f"交付包清单不符: {sorted(names)}"
    detail = caj("GET", f"/api/certs/{name}")
    assert z.read("cert.pem").decode().strip() == detail["certPem"].strip(), "包内证书与详情不一致"
    assert z.read("fullchain.pem").decode().count("BEGIN CERTIFICATE") == 2, \
        "fullchain 应含设备证书+中间证书"
    assert "PRIVATE KEY" in z.read("key.pem").decode(), "key.pem 应为私钥"
    readme = z.read("README.txt").decode()
    assert "烧录" in readme and "信任" in readme, "README 缺设备侧对接指引"
    rec(detail=f"交付包 {sorted(names)}；cert 与详情一致，fullchain 两段，README 含烧录/信任指引",
        metrics={"files": sorted(names)})


def test_ca10_server_bundle(rec):
    rec(tcid="TC-CA-10", name="导出服务器装配包：TB 装配布局与指引",
        precondition="certadmin :19001 运行中（用例自备签发 tc7-srv）",
        steps="1) 签发服务器证书 tc7-srv；2) GET /api/certs/tc7-srv/bundle 下载 zip；"
              "3) 断言装配包清单；4) 校验 README 装配指引",
        expected="含 tc7-srv.pem/tc7-srv-fullchain.pem/tc7-srv_key.pem/root-ca.pem/README.txt；"
                "README 含 MQTT_SSL_PEM_CERT 装配步骤",
        )
    name = "tc7-srv"
    caj("POST", "/api/certs/server",
        json={"name": name, "days": 365, "dns": ["localhost"], "ips": ["127.0.0.1"]})
    z = bundle_zip(name)
    names = set(z.namelist())
    assert {f"{name}.pem", f"{name}-fullchain.pem", f"{name}_key.pem",
            "root-ca.pem", "README.txt"} <= names, f"装配包清单不符: {sorted(names)}"
    readme = z.read("README.txt").decode()
    assert "MQTT_SSL_PEM_CERT" in readme and "fullchain" in readme, "README 缺 TB 装配步骤"
    assert z.read(f"{name}-fullchain.pem").decode().count("BEGIN CERTIFICATE") == 2, \
        "服务器 fullchain 应含服务器证书+中间证书"
    rec(detail=f"装配包 {sorted(names)}，README 含 MQTT_SSL_PEM_CERT 指引",
        metrics={"files": sorted(names)})


# ---------------- 交付台账与私钥销毁 ----------------
def test_ca11_deliver_destroy_key(rec):
    rec(tcid="TC-CA-11", name="交付台账登记与私钥销毁：销毁后 hasKey=false、包内无私钥",
        precondition="certadmin :19001 运行中（用例自备签发 tc7-ca11）",
        steps="1) 签发 tc7-ca11；2) POST /api/certs/tc7-ca11/deliver {destroyKey:true}；"
              "3) 查详情 hasKey/keyDestroyedAt；4) 平台侧 key.pem 文件核实；"
              "5) 重新下载 bundle 断言无 key.pem",
        expected="keyDestroyed=true；deliveredAt/keyDestroyedAt 登记；key.pem 物理删除；"
                "销毁后交付包无私钥且 README 说明已销毁",
        )
    name = "tc7-ca11"
    d = issue_device(name)
    assert d["hasKey"] is True, f"签发应持有私钥: {d}"
    r = caj("POST", f"/api/certs/{name}/deliver", json={"destroyKey": True})
    assert r["keyDestroyed"] is True, f"销毁失败: {r}"
    assert r["deliveredAt"], f"交付时间未登记: {r}"
    d = caj("GET", f"/api/certs/{name}")
    assert d["hasKey"] is False, f"销毁后 hasKey 应为 false: {d}"
    assert d.get("keyDestroyedAt"), f"销毁时间未登记: {d}"
    key_path = CA_DATA / "devices" / name / "key.pem"
    assert not key_path.exists(), f"私钥文件未物理删除: {key_path}"
    z = bundle_zip(name)
    assert "key.pem" not in z.namelist(), f"销毁后交付包仍含私钥: {z.namelist()}"
    readme = z.read("README.txt").decode()
    assert "已销毁" in readme, "README 应说明私钥已销毁"
    rec(detail=f"deliveredAt={r['deliveredAt']}，key.pem 已物理删除，销毁后交付包={z.namelist()}",
        metrics={"deliveredAt": r["deliveredAt"], "keyDestroyed": True})


# ---------------- 负例 ----------------
def test_ca12_validation_negative(rec):
    rec(tcid="TC-CA-12", name="参数校验负例：非法名称/非法 PEM 全部 400",
        precondition="certadmin :19001 运行中",
        steps="1) POST /api/certs/device name 含 /；2) POST /api/certs/csr name 含 /；"
              "3) POST /api/certs/import certPem 为垃圾文本",
        expected="三个请求均返回 400，且不产生登记记录",
        )
    r1 = ca("POST", "/api/certs/device", json={"name": "a/b"})
    assert r1.status_code == 400, f"非法名称应 400: {r1.status_code}"
    r2 = ca("POST", "/api/certs/csr", json={"name": "x/y", "csrPem": "junk"})
    assert r2.status_code == 400, f"CSR 非法名称应 400: {r2.status_code}"
    r3 = ca("POST", "/api/certs/import", json={"name": "tc7-bad", "certPem": "not-a-pem"})
    assert r3.status_code == 400, f"非法 PEM 应 400: {r3.status_code}"
    names = {c["name"] for c in caj("GET", "/api/certs")}
    assert "tc7-bad" not in names, "非法导入不应产生登记"
    rec(detail=f"device/csr 非法名称与非法 PEM 导入均 400，登记簿无残留（{len(names)} 项）")


def test_ca13_not_found(rec):
    rec(tcid="TC-CA-13", name="资源不存在负例：详情/校验/导出/交付均 404",
        precondition="certadmin :19001 运行中",
        steps="1) GET /api/certs/tc7-nope；2) POST 同名 /verify；3) GET 同名 /bundle；"
              "4) POST 同名 /deliver",
        expected="四个请求均返回 404",
        )
    n = "tc7-nope"
    assert ca("GET", f"/api/certs/{n}").status_code == 404, "详情应 404"
    assert ca("POST", f"/api/certs/{n}/verify").status_code == 404, "verify 应 404"
    assert ca("GET", f"/api/certs/{n}/bundle").status_code == 404, "bundle 应 404"
    assert ca("POST", f"/api/certs/{n}/deliver", json={"destroyKey": False}).status_code == 404, \
        "deliver 应 404"
    rec(detail="detail/verify/bundle/deliver 四类不存在资源均 404")


def test_ca14_invalid_csr_rejected(rec):
    rec(tcid="TC-CA-14", name="无效 CSR 拒绝：签发失败且不留半成品登记",
        precondition="certadmin :19001 运行中",
        steps="1) POST /api/certs/csr csrPem 为垃圾文本；2) 查列表确认无该名称登记",
        expected="返回 ≥400 错误；登记簿无该名称（无半成品证书残留）",
        )
    name = "tc7-badcsr"
    r = ca("POST", "/api/certs/csr", json={"name": name, "csrPem": "garbage-not-a-csr"})
    assert r.status_code >= 400, f"无效 CSR 应被拒绝: {r.status_code}"
    names = {c["name"] for c in caj("GET", "/api/certs")}
    assert name not in names, f"失败签发不应登记: {name}"
    assert not (CA_DATA / "devices" / name).exists(), "失败签发不应残留产物目录"
    rec(detail=f"无效 CSR 返回 {r.status_code}，登记簿与产物目录均无残留")

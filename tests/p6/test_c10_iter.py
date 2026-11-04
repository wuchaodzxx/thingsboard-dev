"""C10 迭代验证用例（v1.2）：load/wind 设备类型、负荷特征配置、手动证书+手动注册、
日志数据方向与原始报文。

前置：模拟器 :19000 与 TB :18080/:18885 运行中。
运行：.venv/bin/python -m pytest tests/p6/test_c10_iter.py --scale 1

所有判定基于真实交互（模拟器 REST / TB REST / 真实 TLS 连接）。
"""
from __future__ import annotations

import json
import time

import requests

from conftest import (SIM, SECRETS, make_device, purge_devices, sim_req,
                      tb_active, tb_dev_id, wait_for)


# ---------------- 本地工具 ----------------
def _ts(tb, name: str, keys: str) -> dict:
    """TB 最新遥测（真实落库值，非模拟器自述）。"""
    dev_id = tb_dev_id(tb, name)
    return tb._req("GET", f"/api/plugins/telemetry/DEVICE/{dev_id}/values/timeseries?keys={keys}")


def _ts_ready(tb, name: str, keys: str) -> dict | None:
    """TB 最新遥测（真实落库值，非模拟器自述）。TB 持久化异步：键先到值后到，
    需等待 value 非 None 才算就绪。"""
    t = _ts(tb, name, keys)
    if not all(t.get(k) and t[k][0]["value"] is not None for k in keys.split(",")):
        return None
    return t


def _client_attr(tb, name: str, keys: str) -> dict:
    """查 TB 客户端属性（CLIENT_SCOPE）。注意：TB 3.5.1 的 /clientKeys?keys= 路径
    实测返回空，须取全量后本地过滤。"""
    dev_id = tb_dev_id(tb, name)
    rows = tb._req("GET", f"/api/plugins/telemetry/DEVICE/{dev_id}/values/attributes/CLIENT_SCOPE")
    want = set(keys.split(","))
    return {r["key"]: r["value"] for r in rows if r["key"] in want}


def _make(name: str, dev_type: str, cap: float, load_type: int = 1,
          auto_register: bool = True, connect: bool = True) -> dict:
    return sim_req("POST", "/api/devices", json={
        "name": name, "devType": dev_type, "capKw": cap, "loadType": load_type,
        "autoRegister": auto_register, "autoConnect": connect})


# ---------------- IT-01 load 设备类型（充电桩负荷） ----------------
def test_it01_load_device(tb, rec):
    rec(tcid="TC-IT-01", name="devType=load 设备创建/注册/遥测（loadType=2 充电桩）",
        precondition="模拟器与 TB 运行中，CA 链正常",
        steps="1) 创建 load 设备 loadType=2 自动注册；2) 等 TB 建档与上线；"
              "3) 读 TB 遥测 p/loadType 与客户端属性 devType",
        expected="注册成功；TB active；p≤0（受电为负）；遥测含 loadType=2；devType 属性为 load")
    purge_devices(tb, ["load-it-01"])
    r = _make("load-it-01", "load", 50, load_type=2)
    assert "tb-registered" in r["register"], f"注册流水线异常: {r['register']}"
    wait_for(lambda: (tb_dev_id(tb, "load-it-01"), None), 30, desc="TB 建档")
    wait_for(lambda: (tb_active(tb, "load-it-01"), None), 60, desc="load-it-01 上线")
    t = wait_for(lambda: ((x := _ts_ready(tb, "load-it-01", "p,loadType")) is not None, x),
                 90, desc="遥测 p/loadType 落库")
    assert str(t["loadType"][0]["value"]) == "2", f"loadType 上报错误: {t}"
    p = float(t["p"][0]["value"])
    assert p <= 0, f"load 受电应为负值: {p}"
    attr = wait_for(lambda: ((a := _client_attr(tb, "load-it-01", "devType")).get("devType") == "load", a),
                    60, desc="devType 客户端属性上报")
    rec(detail=f"p={p} kW, loadType={t['loadType'][0]['value']}, register={r['register']}",
        metrics={"capKw": 50, "p": p})


# ---------------- IT-02 wind 设备类型 ----------------
def test_it02_wind_device(tb, rec):
    rec(tcid="TC-IT-02", name="devType=wind 风电设备创建/遥测（风速+三次方出力）",
        precondition="模拟器与 TB 运行中",
        steps="1) 创建 wind 设备；2) 等 TB 建档与上线；3) 读 TB 遥测 p/windSpeed",
        expected="注册成功；TB active；p≥0（发电）；遥测含 windSpeed ∈[0,60] m/s")
    purge_devices(tb, ["wind-it-01"])
    _make("wind-it-01", "wind", 80)
    wait_for(lambda: (tb_active(tb, "wind-it-01"), None), 60, desc="wind-it-01 上线")
    t = wait_for(lambda: ((x := _ts_ready(tb, "wind-it-01", "p,windSpeed")) is not None, x),
                 90, desc="遥测 p/windSpeed 落库")
    p = float(t["p"][0]["value"])
    v = float(t["windSpeed"][0]["value"])
    assert p >= 0, f"wind 发电应为非负值: {p}"
    assert 0 <= v <= 60, f"windSpeed 越界: {v}"
    rec(detail=f"p={p} kW, windSpeed={v} m/s", metrics={"capKw": 80, "p": p, "windSpeed": v})


# ---------------- IT-03 负荷特征模拟配置 ----------------
def test_it03_load_profile(tb, rec):
    rec(tcid="TC-IT-03", name="负荷特征配置：基线负荷率/占空启停生效",
        precondition="load-it-01 已创建（TC-IT-01）",
        steps="1) POST load-profile baseRatio=0.9；2) 轮询模拟器 status.curP 验证功率抬升；"
              "3) POST dutyOnMin=1 dutyOffMin=1 启用分钟级占空启停；4) 轮询 curP 出现 0（停运窗口）；"
              "5) 关闭占空恢复",
        expected="配置回读一致；基线生效后 curP≈-0.9×cap；占空启用后 2 分钟内出现 curP=0")
    purge_devices(tb, ["load-it-03"])
    _make("load-it-03", "load", 50, load_type=1, connect=False)
    r = sim_req("POST", "/api/devices/load-it-03/load-profile",
                json={"baseRatio": 0.9, "noiseAmp": 0.02})
    assert r["loadProfile"]["baseRatio"] == 0.9, f"配置回读异常: {r}"
    sim_req("POST", "/api/devices/load-it-03/connect")
    p_base = wait_for(lambda: ((d := sim_req("GET", "/api/devices/load-it-03")["status"]["curP"]) is not None
                               and -52 <= d <= -38, d), 150, desc="基线负荷率生效 curP≈-45（含爬坡）")
    sim_req("POST", "/api/devices/load-it-03/load-profile",
            json={"dutyOnMin": 1, "dutyOffMin": 3})   # off 窗口需 ≥爬坡回零时长(≈110s)+余量
    def _saw_zero():
        p = sim_req("GET", "/api/devices/load-it-03")["status"]["curP"]
        return (p >= -5, p)                           # 停运窗口内功率爬坡回零
    p_off = wait_for(_saw_zero, 240, interval=5, desc="占空停运窗口 curP 爬回 0")
    sim_req("POST", "/api/devices/load-it-03/load-profile", json={"dutyOnMin": 0, "dutyOffMin": 0})
    rec(detail=f"基线 curP={p_base} kW（预期≈-45±噪声）；占空停运观测到 curP=0",
        metrics={"baseP": p_base, "dutyZeroAt": p_off})


# ---------------- IT-04 手动证书配置 + 手动注册 ----------------
def test_it04_manual_cert_register(tb, rec):
    rec(tcid="TC-IT-04", name="手动流程：创建不注册 → CSR 签发 → 导入证书 → 手动注册 → 上线",
        precondition="模拟器与 TB 运行中；certmgr secrets 可写",
        steps="1) 创建 dev-auto=false 设备（无证书）；2) openssl 生成外部密钥对+CSR；"
              "3) POST /api/certs/csr 签发（私钥不出外部）；4) POST /api/certs/import 导入证书+私钥；"
              "5) POST /api/devices/{name}/register 手动注册；6) 连接并等 TB active",
        expected="创建返回 manual-pending 且不自动注册；导入校验通过；注册后 TB active")
    import subprocess
    name = "load-man-it"
    purge_devices(tb, [name])
    r = sim_req("POST", "/api/devices", json={"name": name, "devType": "load", "capKw": 30,
                                              "loadType": 4, "autoRegister": False,
                                              "autoConnect": False})
    assert r["register"] == "manual-pending", f"应待手动注册: {r['register']}"
    tmp = SECRETS / "devices" / f"{name}-ext"
    tmp.mkdir(parents=True, exist_ok=True)
    key, csr = tmp / "key.pem", tmp / "req.csr"
    subprocess.run(["openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout",
                    "-out", str(key)], check=True)
    subprocess.run(["openssl", "req", "-new", "-key", str(key), "-out", str(csr),
                    "-subj", f"/CN={name}/O=VPP-Lab"], check=True)
    signed = sim_req("POST", "/api/certs/csr", json={
        "name": name, "csrPem": csr.read_text(), "days": 365})
    # 模拟外部厂家持有 cert+key：读取签发产物，走手动导入通道
    cert_pem = (SECRETS / "devices" / name / "cert.pem").read_text()
    imp = sim_req("POST", "/api/certs/import", json={
        "name": name, "certPem": cert_pem, "keyPem": key.read_text()})
    assert imp["keyImported"] is True and "load-man-it" in imp["cn"], f"导入异常: {imp}"
    reg = sim_req("POST", f"/api/devices/{name}/register")
    assert reg["register"] == "tb-registered" and reg["devType"] == "load", f"注册异常: {reg}"
    sim_req("POST", f"/api/devices/{name}/connect")
    wait_for(lambda: (tb_active(tb, name), None), 60, desc="手动流程设备上线")
    purge_devices(tb, [name])
    rec(detail=f"导入 cn={imp['cn']} serial={imp.get('serial', '')}；注册 {reg['register']}")


# ---------------- IT-05 日志方向与原始报文 ----------------
def test_it05_log_direction_raw(tb, rec):
    rec(tcid="TC-IT-05", name="日志数据方向（上行/下行）与原始报文记录",
        precondition="load-it-01 在线且持续遥测（TC-IT-01 已执行）",
        steps="1) 查询 direction=out 日志；2) 查询 direction=in 日志；3) 逐条校验 directionCN 与 raw；"
              "4) 解析 raw 为 JSON",
        expected="上行记录 directionCN=上行且 raw 为合法 JSON；下行记录 raw 非空（M08 报文）")
    logs_out = sim_req("GET", "/api/logs", params={"device": "load-it-01", "direction": "out", "limit": 20})
    assert logs_out, "无上行日志"
    for r in logs_out:
        assert r["direction"] == "out" and str(r["directionCN"]).startswith("上行"), f"方向标识错误: {r['seq']}"
        assert r["raw"], f"上行记录缺 raw: seq={r['seq']}"
        json.loads(r["raw"])          # 原始报文应为合法 JSON
    logs_in = sim_req("GET", "/api/logs", params={"device": "load-it-01", "direction": "in", "limit": 20})
    if not logs_in:                        # 主动触发真实下行：TB 下发共享属性（M08）
        tb.save_shared_attributes(tb_dev_id(tb, "load-it-01"),
                                  {"hbPeriodSec": 30, "tele15mEnable": True})
        logs_in = wait_for(lambda: (bool(x := sim_req("GET", "/api/logs", params={
            "device": "load-it-01", "direction": "in", "limit": 20})), x), 30,
            desc="M08 下行日志")
    assert logs_in, "无下行日志（M08 下发后应记录）"
    for r in logs_in:
        assert r["direction"] == "in" and str(r["directionCN"]).startswith("下行"), f"方向标识错误: {r['seq']}"
        assert r["raw"], f"下行记录缺 raw: seq={r['seq']}"
    rec(detail=f"上行 {len(logs_out)} 条/下行 {len(logs_in)} 条，raw 与 directionCN 全部符合",
        metrics={"outCount": len(logs_out), "inCount": len(logs_in)})


# ---------------- IT-06 参数校验负例 ----------------
def test_it06_validation_negative(rec):
    rec(tcid="TC-IT-06", name="devType/loadType 校验负例",
        precondition="模拟器运行中",
        steps="1) POST devType=flex（已废弃）；2) POST devType=load 且 loadType=0；"
              "3) load-profile 越界 baseRatio=1.5",
        expected="全部 400，错误信息明确")
    r1 = requests.post(f"{SIM}/api/devices", json={"name": "bad-flex", "devType": "flex"})
    assert r1.status_code == 400, f"flex 应拒绝: {r1.status_code}"
    r2 = requests.post(f"{SIM}/api/devices", json={"name": "bad-lt", "devType": "load", "loadType": 0})
    assert r2.status_code == 400, f"loadType=0 应拒绝: {r2.status_code}"
    _make("bad-lp", "load", 10, connect=False)
    r3 = requests.post(f"{SIM}/api/devices/bad-lp/load-profile", json={"baseRatio": 1.5})
    assert r3.status_code == 400, f"baseRatio=1.5 应拒绝: {r3.status_code}"
    sim_req("DELETE", "/api/devices/bad-lp")
    rec(detail=f"flex→{r1.json()}，loadType→{r2.json()}，baseRatio→{r3.json()}")

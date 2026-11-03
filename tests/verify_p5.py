"""P5 平台侧验证实测脚本：通过模拟器验证控制台（/api/verify/*）完成需求第四章 7 项。

所有判定均基于 ThingsBoard 实际交互与存储结果（REST 回读），不以模拟器发送日志为判据。
前置：TB 容器 18080/18885 运行中；模拟器 uvicorn 19000 运行中（已注册 verifyapi）。
用法：python tests/verify_p5.py
输出：控制台逐项 PASS/FAIL + tests/results_p5.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

WS = Path(__file__).resolve().parents[1]
SIM = "http://localhost:19000"
RESULTS: list[dict] = []
PV, PCS = "pv-1001", "pcs-1001"


def step(sid: str, name: str, fn, critical=True):
    t0 = time.time()
    try:
        detail = fn()
        RESULTS.append({"id": sid, "name": name, "result": "PASS",
                        "detail": detail, "elapsedMs": int((time.time() - t0) * 1000)})
        print(f"[PASS] {sid} {name} | {detail}")
    except Exception as e:  # noqa: BLE001
        RESULTS.append({"id": sid, "name": name, "result": "FAIL", "error": str(e),
                        "elapsedMs": int((time.time() - t0) * 1000)})
        print(f"[FAIL] {sid} {name} | {e}")
        if critical:
            print("（关键用例失败，终止后续步骤）")
            _dump()
            sys.exit(1)


def _dump():
    out = WS / "tests" / "results_p5.json"
    out.write_text(json.dumps({"ts": int(time.time() * 1000), "results": RESULTS},
                              ensure_ascii=False, indent=1))
    print(f"→ {out}")


def poll(fn, timeout: float, interval: float = 1.0, desc: str = ""):
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


def vapi(method: str, path: str, **kw):
    """验证控制台调用（/api/verify/*）。"""
    r = requests.request(method, SIM + path, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


# ---------- 7 项验证 ----------
def v1_status():
    """需求 4.1：设备注册、连接及认证状态（TB active + X509 凭据）。"""
    devs = {d["name"]: d for d in vapi("GET", "/api/verify/devices")["devices"]}
    for n in (PV, PCS):
        d = devs.get(n)
        assert d, f"TB 中无 {n}"
        assert d["active"] is True, f"{n} TB active={d['active']}"
        assert d["credentialsType"] == "X509_CERTIFICATE", f"{n} 凭据 {d['credentialsType']}"
        assert d["devType"] in ("pv", "pcs"), d
    return f"{PV}/{PCS}: active=true, credentialsType=X509_CERTIFICATE, type={devs[PV]['devType']}/{devs[PCS]['devType']}"


def v2_telemetry():
    """需求 4.2：最新遥测与历史数据（TB 实际存储回读）。"""
    latest = vapi("GET", "/api/verify/telemetry/latest", params={"device": PV, "keys": "p,s,dqf"})["latest"]
    for k in ("p", "s", "dqf"):
        assert k in latest and latest[k], f"PV latest 缺 {k}: {latest.keys()}"
    end = int(time.time() * 1000)
    h = vapi("GET", "/api/verify/telemetry/history",
             params={"device": PV, "keys": "p,dqf", "startTs": end - 5 * 60000, "endTs": end})
    pts = h["data"].get("p", [])
    assert len(pts) >= 2, f"5min 窗口 p 仅 {len(pts)} 条"
    assert pts[-1]["ts"] % 60000 == 0, f"ts 未整分对齐 {pts[-1]['ts']}"
    latest_pcs = vapi("GET", "/api/verify/telemetry/latest",
                      params={"device": PCS, "keys": "soc,soh,p"})["latest"]
    assert "soc" in latest_pcs, f"PCS latest 缺 soc: {latest_pcs.keys()}"
    return (f"PV latest p/s/dqf 齐全, 5min 历史 p={len(pts)} 条(整分对齐), "
            f"PCS soc={latest_pcs['soc'][0]['value']}")


def v3_attributes():
    """需求 4.3：下发共享属性并验证设备应用（CLIENT_SCOPE 回执为 TB 存储证据）。"""
    now = int(time.time() * 1000)
    r = vapi("POST", "/api/verify/attributes",
             json={"device": PV, "payload": {"cfgVer": 2, "hbPeriodSec": 30, "tele15mEnable": True}})
    assert r["saved"]["cfgVer"] == 2, r

    def _applied():
        m = {a["key"]: a["value"] for a in vapi("GET", "/api/verify/attributes",
                                                params={"device": PV})["clientScope"]}
        ok = m.get("cfgVerApplied") == 2 and m.get("cfgResult") == 0 \
            and m.get("cfgApplyTs", 0) >= now
        return ok, m
    m = poll(_applied, 20, desc="cfgVerApplied=2 回执(TB clientScope)")
    return f"cfgVer=2 已应用: cfgVerApplied={m['cfgVerApplied']}, cfgResult={m['cfgResult']}, hbPeriod→30s"


def v4_callup():
    """需求 4.4：发起设备数据召测（readRealtimeData），以 TB 侧 p 点更新为入库证据。"""
    before = vapi("GET", "/api/verify/telemetry/latest",
                  params={"device": PV, "keys": "p"})["latest"]["p"][0]["ts"]
    now = int(time.time() * 1000)
    r = vapi("POST", "/api/verify/rpc",
             json={"device": PV, "method": "readRealtimeData",
                   "env": {"cmdId": f"v5-rd-{now}", "issuedAt": now,
                           "expireAt": now + 5000, "params": {}}})["response"]
    assert r["state"] == "SUCCESS" and r["errCode"] == 0, r
    assert isinstance(r["data"]["p"], (int, float)), r

    def _m05():
        cur = vapi("GET", "/api/verify/telemetry/latest",
                   params={"device": PV, "keys": "p"})["latest"]["p"][0]["ts"]
        return cur > before, (before, cur)
    poll(_m05, 10, desc="M05 召测数据入库(TB p ts 前移)")
    return f"SUCCESS p={r['data']['p']}, TB p ts {before}→更新"


def v5_control():
    """需求 4.5：发送远程控制指令（setActivePowerTarget），期望 ACK。"""
    now = int(time.time() * 1000)
    r = vapi("POST", "/api/verify/rpc",
             json={"device": PV, "method": "setActivePowerTarget",
                   "env": {"cmdId": f"v5-pt-{now}", "issuedAt": now,
                           "expireAt": now + 180000, "params": {"targetP": 60}}})["response"]
    assert r["state"] == "ACK", r
    return f"cmdId=v5-pt-{now} state=ACK (受理)"


def v6_exec_data():
    """需求 4.6：指令响应及实际执行数据（平台 cmdState 终态 + actualP + 功率到位）。"""
    send_ms = int(time.time() * 1000)
    # v5 已发出 ACK 指令；此处轮询平台侧终态（若 v5 与 v6 间隔过短则重发一条）
    led = vapi("GET", "/api/verify/ledger", params={"device": PV})
    plat = led["platformLatest"]
    if plat.get("cmdState", {}).get("value") != "SUCCESS" or plat.get("cmdState", {}).get("ts", 0) < send_ms:
        now = int(time.time() * 1000)
        vapi("POST", "/api/verify/rpc",
             json={"device": PV, "method": "setActivePowerTarget",
                   "env": {"cmdId": f"v5-pt2-{now}", "issuedAt": now,
                           "expireAt": now + 180000, "params": {"targetP": 60}}})

    def _terminal():
        led = vapi("GET", "/api/verify/ledger", params={"device": PV})
        plat = led["platformLatest"]
        dev_ok = any(e["cmdId"].startswith("v5-pt") and e["state"] == "SUCCESS"
                     for e in led["deviceLedger"])
        ok = (plat.get("cmdState", {}).get("value") == "SUCCESS"
              and plat.get("cmdState", {}).get("ts", 0) > send_ms
              and "actualP" in plat and dev_ok)
        return ok, plat
    plat = poll(_terminal, 60, desc="平台 cmdState=SUCCESS(本轮)+actualP")
    # 实际执行数据：TB p 遥测到位 60±1.5（曲线与调节平滑）
    def _p():
        cur = vapi("GET", "/api/verify/telemetry/latest",
                   params={"device": PV, "keys": "p"})["latest"]["p"][0]
        return cur["ts"] > send_ms and abs(float(cur["value"]) - 60) <= 1.5, cur["value"]
    v = poll(_p, 150, desc="TB p→60")
    return f"cmdState=SUCCESS(ts>{send_ms}), actualP={plat['actualP']['value']}, TB p={v}"


def v7_compare():
    """需求 4.7：发送 vs 平台接收完整性比对（M03 逐点核对，verdict=PASS）。"""
    def _cmp():
        r = vapi("POST", "/api/verify/compare",
                 json={"device": PV, "windowSec": 300, "msgType": "M03"})
        return r["verdict"] == "PASS" and r["sentCount"] >= 2, r
    r = poll(_cmp, 60, interval=5, desc="M03 完整性比对")
    return (f"M03 窗口300s: sent={r['sentCount']} matched={r['receivedCount']} "
            f"missing={len(r['missingTs'])} mismatch={len(r['valueMismatch'])} → PASS")


if __name__ == "__main__":
    print("== P5 平台侧验证（需求第四章 7 项，全部基于 TB 实际存储判定）==")
    step("P5-01", "设备注册/连接/认证状态", v1_status)
    step("P5-02", "最新遥测与历史数据查询", v2_telemetry)
    step("P5-03", "共享属性下发与应用验证", v3_attributes)
    step("P5-04", "数据召测(readRealtimeData)", v4_callup)
    step("P5-05", "远程控制指令下发(ACK)", v5_control)
    step("P5-06", "指令响应与执行数据核验", v6_exec_data)
    step("P5-07", "发送/接收数据完整性比对", v7_compare)
    _dump()
    n_fail = sum(1 for r in RESULTS if r["result"] == "FAIL")
    print(f"== 完成：{len(RESULTS) - n_fail}/{len(RESULTS)} PASS ==")
    sys.exit(1 if n_fail else 0)

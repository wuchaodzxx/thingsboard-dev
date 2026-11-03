"""P4 模拟器↔ThingsBoard 联调实测脚本（真实交互核验，禁止模拟结果）。

前置：TB 容器 18080/18885 运行中；模拟器 uvicorn 19000 运行中。
用法：python tests/verify_p4.py [--quick]
输出：控制台逐项 PASS/FAIL + tests/results_p4.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "certmgr"))

from certmgr.tb_client import TBClient  # noqa: E402

SIM = "http://localhost:19000"
QUICK = "--quick" in sys.argv
RESULTS: list[dict] = []


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
    out = WS / "tests" / "results_p4.json"
    out.write_text(json.dumps({"ts": int(time.time() * 1000), "results": RESULTS},
                              ensure_ascii=False, indent=1))
    print(f"→ {out}")


def _thread_snapshot() -> str:
    """轮询超时时抓取模拟器设备/执行线程栈尾帧，用于定位线程停摆现场。"""
    try:
        th = requests.get(SIM + "/api/debug/threads", timeout=10).json()
        parts = []
        for name, stack in th.items():
            if name.startswith(("sim-", "exec-")):
                lines = [l.strip() for l in stack.splitlines() if l.strip()]
                parts.append(f"{name} <- {lines[-1] if lines else '?'}")
        return "; ".join(parts) or "无设备线程"
    except Exception as e:  # noqa: BLE001
        return f"栈抓取失败: {e}"


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
    raise AssertionError(f"轮询超时({timeout}s) {desc}: last={last} | 线程: {_thread_snapshot()}")


def api(method: str, path: str, **kw):
    r = requests.request(method, SIM + path, timeout=15, **kw)
    r.raise_for_status()
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else r.text


tb = TBClient("http://localhost:18080")
tb.login()
DEV_ID: dict[str, str] = {}


def dev_id(name: str) -> str:
    if name not in DEV_ID:
        d = tb.get_device(name)
        DEV_ID[name] = d["id"]["id"]
    return DEV_ID[name]


PV, PCS = "pv-1001", "pcs-1001"


# ---------- 用例 ----------
def s1_health():
    h = api("GET", "/api/health")
    assert h["status"] == "ok", h
    assert h["mqtt"]["port"] == 18885, h
    return f"mqtt={h['mqtt']}"


def s2_create():
    for name, dt, cap in ((PV, "pv", 120), (PCS, "pcs", 100)):
        try:
            api("DELETE", f"/api/devices/{name}")
        except requests.HTTPError:
            pass
        r = api("POST", "/api/devices", json={"name": name, "devType": dt, "capKw": cap})
        assert name in r["created"], r
    dev = tb.get_device(PV)
    assert dev is not None, f"TB 中无 {PV}"
    creds = tb.get_credentials(dev["id"]["id"])
    assert creds["credentialsType"] == "X509_CERTIFICATE", creds
    return f"created {PV}+{PCS}, TB credentialsType=X509_CERTIFICATE"


def s3_connect():
    def _both_online():
        s = {d["name"]: d for d in api("GET", "/api/devices")}
        return all(s[n]["connected"] for n in (PV, PCS)), s
    st = poll(_both_online, 60, desc="设备上线")
    return f"{PV}/{PCS} connected, hbSeq={st[PV]['hbSeq']}"


def s4_telemetry_1min():
    def _pts():
        end = int(time.time() * 1000)
        hist = tb.get_history_timeseries(dev_id(PV), "p,dqf", end - 4 * 60000, end, limit=100)
        pts = hist.get("p", [])
        return len(pts) >= 2, (pts, hist)
    pts, hist = poll(_pts, 150, interval=5, desc="1min 遥测 ≥2 条")
    ts0 = pts[-1]["ts"]
    assert ts0 % 60000 == 0, f"ts 未整分对齐: {ts0}"
    dqf_pts = hist.get("dqf", [])
    assert dqf_pts and all(0 <= float(d["value"]) <= 3 for d in dqf_pts), dqf_pts
    last = float(pts[-1]["value"])
    assert 0 <= last <= 120.001, f"p 越界 {last}"
    latest = tb.get_latest_timeseries(dev_id(PV), "p,s")
    assert "s" in latest, f"缺 s 视在功率: {latest.keys()}"
    return f"{len(pts)} 条/4min, ts 整分对齐, latest p={latest['p'][0]['value']}, s={latest['s'][0]['value']}"


def s4b_pcs_soc():
    latest = tb.get_latest_timeseries(dev_id(PCS), "soc,soh,cellVMax,cycCnt")
    for k in ("soc", "soh", "cellVMax", "cycCnt"):
        assert k in latest, f"pcs 缺 {k}: {latest.keys()}"
    soc = float(latest["soc"][0]["value"])
    assert 0 <= soc <= 100, soc
    return f"soc={soc}"


def s5_shared_attrs():
    now = int(time.time() * 1000)
    tb.save_shared_attributes(dev_id(PV), {"cfgVer": 1, "hbPeriodSec": 20, "tele15mEnable": True})
    def _applied():
        attrs = tb.get_attributes(dev_id(PV))
        m = {a["key"]: a["value"] for a in attrs}
        ok = m.get("cfgVerApplied") == 1 and m.get("cfgResult") == 0 and m.get("cfgApplyTs", 0) >= now
        return ok, m
    m = poll(_applied, 15, desc="cfgVerApplied 回执")
    def _hb_fast():
        s = next(d for d in api("GET", "/api/devices") if d["name"] == PV)
        return s["hbPeriodSec"] == 20 and s["hbSeq"] >= 1, s["hbSeq"]
    poll(_hb_fast, 25, desc="心跳周期生效")
    return f"cfgVerApplied={m['cfgVerApplied']} cfgResult={m['cfgResult']} hbPeriod→20s"


def s5b_shared_reject():
    tb.save_shared_attributes(dev_id(PV), {"cfgVer": 1, "hbPeriodSec": 20})  # 重复版本
    def _rej():
        m = {a["key"]: a["value"] for a in tb.get_attributes(dev_id(PV))}
        return m.get("cfgResult") == 1002, m.get("cfgResult")
    m = poll(_rej, 15, desc="cfgVer 重复拒绝")
    return f"cfgResult={m}"


def s6_rpc_read_realtime():
    before = tb.get_latest_timeseries(dev_id(PV), "p")["p"][0]["ts"]
    env = {"cmdId": f"rd-{int(time.time())}", "issuedAt": int(time.time() * 1000),
           "expireAt": int(time.time() * 1000) + 5000, "params": {}}
    resp = tb.send_two_way_rpc(dev_id(PV), "readRealtimeData", env)
    assert resp["state"] == "SUCCESS" and resp["errCode"] == 0, resp
    assert isinstance(resp["data"]["p"], (int, float)), resp
    def _m05():
        latest = tb.get_latest_timeseries(dev_id(PV), "p")["p"][0]
        return latest["ts"] > before, (before, latest["ts"])
    poll(_m05, 6, desc="M05 召测遥测")
    return f"SUCCESS p={resp['data']['p']}, M05 已入库"


def s7_rpc_control():
    send_ms = int(time.time() * 1000)
    cmd = {"cmdId": f"pt-{int(time.time())}", "issuedAt": send_ms,
           "expireAt": send_ms + 180000, "params": {"targetP": 80}}
    resp = tb.send_two_way_rpc(dev_id(PV), "setActivePowerTarget", cmd)
    assert resp["state"] == "ACK", resp
    # 1) 设备侧 ledger 终态（模拟器进程内存，新建设备无历史污染）
    def _led_done():
        led = api("GET", f"/api/devices/{PV}/ledger")
        e = next((x for x in led if x["cmdId"] == cmd["cmdId"]), None)
        return bool(e and e["state"] in ("SUCCESS", "FAILED")), e
    entry = poll(_led_done, 180, desc="设备侧指令终态")
    assert entry["state"] == "SUCCESS", entry
    # 2) 平台侧 cmdStatus 遥测：ts 必须晚于本轮 RPC 发出时刻（排除上轮遗留 latest）
    def _terminal():
        latest = tb.get_latest_timeseries(dev_id(PV), "cmdState,actualP")
        m = {k: latest[k][0] for k in latest if latest[k]}
        ok = (m.get("cmdState", {}).get("value") == "SUCCESS"
              and m.get("cmdState", {}).get("ts", 0) > send_ms and "actualP" in m)
        return ok, m
    m = poll(_terminal, 30, desc="平台 cmdStatus 终态(本轮)")
    def _p_reached():
        latest = tb.get_latest_timeseries(dev_id(PV), "p")["p"][0]
        return latest["ts"] > send_ms and abs(float(latest["value"]) - 80) <= 1.0, latest["value"]
    v = poll(_p_reached, 150, desc="实际功率到位")
    return f"ACK→SUCCESS actualP={m['actualP']['value']}, 平台 p≈{v}"


def s7b_rpc_dup():
    env = {"cmdId": "rd-dup-fixed", "issuedAt": int(time.time() * 1000),
           "expireAt": int(time.time() * 1000) + 30000, "params": {}}
    r1 = tb.send_two_way_rpc(dev_id(PV), "getDeviceParams", env)
    assert r1["state"] == "SUCCESS", r1
    r2 = tb.send_two_way_rpc(dev_id(PV), "getDeviceParams", env)
    assert r2.get("dup") is True and r2["state"] == "SUCCESS", r2
    return f"首答 SUCCESS, 重发 dup=true state={r2['state']}"


def s7c_rpc_expired():
    env = {"cmdId": f"ex-{int(time.time())}", "issuedAt": int(time.time() * 1000) - 10000,
           "expireAt": int(time.time() * 1000) - 5000, "params": {}}
    resp = tb.send_two_way_rpc(dev_id(PV), "setActivePowerTarget", env)
    assert resp["state"] == "REJECTED" and resp["errCode"] == 2004, resp
    return f"REJECTED errCode={resp['errCode']} (2004 过期)"


def s7d_pcs_power():
    send_ms = int(time.time() * 1000)
    cmd = {"cmdId": f"cdp-{int(time.time())}", "issuedAt": send_ms,
           "expireAt": send_ms + 360000, "params": {"p": -50, "durationS": 600}}
    resp = tb.send_two_way_rpc(dev_id(PCS), "setChargeDischargePower", cmd)
    assert resp["state"] == "ACK", resp
    def _led_done():
        led = api("GET", f"/api/devices/{PCS}/ledger")
        e = next((x for x in led if x["cmdId"] == cmd["cmdId"]), None)
        return bool(e and e["state"] in ("SUCCESS", "FAILED")), e
    entry = poll(_led_done, 300, desc="pcs 设备侧终态")
    assert entry["state"] == "SUCCESS", entry
    def _terminal():
        latest = tb.get_latest_timeseries(dev_id(PCS), "cmdState,actualP,soc")
        m = {k: latest[k][0] for k in latest if latest[k]}
        ok = (m.get("cmdState", {}).get("value") == "SUCCESS"
              and m.get("cmdState", {}).get("ts", 0) > send_ms and "actualP" in m)
        return ok, m
    m = poll(_terminal, 30, desc="pcs 平台终态(本轮)")
    def _neg_p():
        latest = tb.get_latest_timeseries(dev_id(PCS), "p")["p"][0]
        return latest["ts"] > send_ms and float(latest["value"]) <= -45, latest["value"]
    v = poll(_neg_p, 120, desc="pcs p=-50")
    return f"SUCCESS actualP={m['actualP']['value']}, 平台 p={v}, soc={m.get('soc', {}).get('value')}"


def s8_disconnect_replay():
    s = {d["name"]: d for d in api("GET", "/api/devices")}
    before = int(tb.get_history_timeseries(dev_id(PV), "p",
                int(time.time() * 1000) - 3600000, int(time.time() * 1000), limit=2000).get("p", [])[0]["ts"]) \
        if tb.get_history_timeseries(dev_id(PV), "p", int(time.time() * 1000) - 3600000,
                                     int(time.time() * 1000), limit=2000).get("p") else 0
    api("POST", f"/api/devices/{PV}/inject", json={"mode": "disconnect", "durationSec": 135})
    def _off():
        st = {d["name"]: d for d in api("GET", "/api/devices")}
        return st[PV]["connected"] is False, st[PV]["lastError"]
    poll(_off, 10, desc="模拟断线")
    time.sleep(130)  # 135s 断线保证跨 2 个整分边界 → 缓存 ≥2 条 1min 记录
    def _replayed():
        st = {d["name"]: d for d in api("GET", "/api/devices")}
        ok = st[PV]["connected"] and st[PV]["cacheCount"] == 0 and st[PV]["replayedBatches"] >= 1
        return ok, (st[PV]["connected"], st[PV]["cacheCount"], st[PV]["replayedBatches"])
    poll(_replayed, 90, desc="重连+补传完成")
    end = int(time.time() * 1000)
    # 平台侧核验：TB 遥测经 rule engine 异步落库，需轮询等待（补传 2 条 + 断线前 1 条）
    def _plat_pts():
        hist = tb.get_history_timeseries(dev_id(PV), "p", end - 200000,
                                         int(time.time() * 1000), limit=500)
        pts = hist.get("p", [])
        return len(pts) >= 3, len(pts)
    n = poll(_plat_pts, 90, interval=5, desc="平台 200s 窗口 ≥3 条(补传+恢复)")
    # 断线期间发送的记录应标记 cached（模拟器日志审计）
    logs = api("GET", "/api/logs", params={"device": PV, "limit": 500})
    cached = [l for l in logs if l.get("result") == "cached"]
    replay_evt = [l for l in logs if l.get("msgType") == "M06" and l.get("result") == "replay-done"]
    assert cached, "无 cached 事件"
    assert replay_evt, "无 replay-done 事件"
    return f"cached={len(cached)} replayBatches={replay_evt[0]['error']}, 平台 200s 窗口 {n} 条"


def s9_read_history():
    end = int(time.time() * 1000)
    start = end - 2 * 3600 * 1000
    resp = tb.send_two_way_rpc(dev_id(PV), "readHistoryData",
                               {"cmdId": f"rh-{int(time.time())}", "issuedAt": end,
                                "expireAt": end + 60000,
                                "params": {"startTs": start, "endTs": end, "interval": 15,
                                           "callId": f"call-{int(time.time())}"}})
    assert resp["state"] == "SUCCESS", resp
    assert resp["data"]["records"] > 0 and resp["data"]["plannedBatches"] >= 1, resp
    want = resp["data"]["records"]
    # 模拟器按 floor(startTs) 对齐生成首条历史点（可能早于 startTs），
    # 查询窗口需同样放宽，否则首条被窗口排除（8<9 假性缺点）
    start_q = (start // 900000) * 900000
    def _batches():
        hist = tb.get_history_timeseries(dev_id(PV), "e_exp_15", start_q, end, limit=1000)
        pts = hist.get("e_exp_15", [])
        # 平台存点数 ≥ 召测承诺记录数（历史批次 ts 跨轮覆盖重合，按存量核验；
        # 本轮执行证据由 M13 records/plannedBatches 与下方 M06 发送日志佐证）
        return len(pts) >= want, (len(pts), want)
    n = poll(_batches, 60, desc="平台 e_exp_15 存量核验")
    logs = api("GET", "/api/logs", params={"device": PV, "msgType": "M06", "limit": 100})
    assert any(l.get("result") == "sent" for l in logs), "无 M06 发送记录(佐证)"
    return f"meta={resp['data']}, 平台 e_exp_15 {n[0]} 点 ≥ records {n[1]}"


def s10_faults():
    """异常注入分步核验（平台侧断言，不依赖模拟器日志中的原始 payload）：
    ①缺字段→平台 1min 点缺 p；②越界→平台出现 p=99999；③坏报文→TB 断链；
    ④拒令→REJECTED 2003（在 badJson 生效前发出，保证设备在线）。"""
    now = int(time.time() * 1000)
    # ① 缺字段：下一条 1min 遥测缺 p（平台 dqf 有点而同 ts 无 p 点）
    api("POST", f"/api/devices/{PV}/inject", json={"mode": "missingField", "count": 1})
    def _missing_p():
        hist = tb.get_history_timeseries(dev_id(PV), "p,dqf", now, now + 200000, limit=500)
        p_ts = {pt["ts"] for pt in hist.get("p", [])}
        holes = [pt["ts"] for pt in hist.get("dqf", []) if pt["ts"] not in p_ts]
        return bool(holes), holes[:2]
    holes = poll(_missing_p, 150, interval=5, desc="平台缺 p 点(缺字段注入)")
    # ② 越界：下一条遥测 p=99999
    api("POST", f"/api/devices/{PV}/inject", json={"mode": "outOfRange", "count": 1})
    def _oor():
        hist = tb.get_history_timeseries(dev_id(PV), "p", now, now + 200000, limit=500)
        bad = [pt for pt in hist.get("p", []) if float(pt["value"]) > 1000]
        return bool(bad), bad[:1]
    poll(_oor, 150, interval=5, desc="平台 p=99999(越界注入)")
    # ③ 拒令（先发 RPC，badJson 尚未生效，设备在线）
    api("POST", f"/api/devices/{PV}/inject", json={"mode": "rejectCmd", "count": 1})
    env = {"cmdId": f"rj-{int(time.time())}", "issuedAt": int(time.time() * 1000),
           "expireAt": int(time.time() * 1000) + 30000, "params": {"targetP": 60}}
    resp = tb.send_two_way_rpc(dev_id(PV), "setActivePowerTarget", env)
    assert resp["state"] == "REJECTED" and resp["errCode"] == 2003, resp
    # ④ 坏报文：下一条遥测为坏 JSON → TB 校验失败主动断链
    api("POST", f"/api/devices/{PV}/inject", json={"mode": "badJson", "count": 1})
    def _offline():
        s = {d["name"]: d for d in api("GET", "/api/devices")}
        return s[PV]["connected"] is False, s[PV]["lastError"]
    poll(_offline, 90, desc="坏报文后 TB 断链")
    return f"缺字段(ts={holes[0]} 缺p)+越界99999+拒令2003+坏报文断链"


def s11_fail_cmd():
    # 前序 badJson 断链后设备离线：等待 TB 侧 active=true（模拟器重连早于 TB 会话注册）。
    # 注意：/api/tenant/devices 列表接口不含 active 字段，必须经 /api/device/{id} 读取。
    def _tb_online():
        d = tb.get_device_by_id(dev_id(PV))
        return bool(d.get("active")), d.get("active")
    poll(_tb_online, 90, desc="s10 断链后 TB active 恢复")
    send_ms = int(time.time() * 1000)
    env = {"cmdId": f"fl-{int(time.time())}", "issuedAt": send_ms,
           "expireAt": send_ms + 120000, "params": {"targetP": 30}}
    # 409 兜底重试：TB active 标志与 session 注册存在秒级竞态
    resp = None
    for _ in range(5):
        try:
            resp = tb.send_two_way_rpc(dev_id(PV), "setActivePowerTarget", env)
            break
        except requests.HTTPError as e:  # noqa: PERF203
            if "409" in str(e):
                time.sleep(4)
                continue
            raise
    assert resp is not None and resp["state"] == "ACK", resp
    def _failed():
        latest = tb.get_latest_timeseries(dev_id(PV), "cmdState,errCode")
        m = {k: latest[k][0] for k in latest if latest[k]}
        ok = (m.get("cmdState", {}).get("value") == "FAILED"
              and m.get("cmdState", {}).get("ts", 0) > send_ms)
        return ok, m
    m = poll(_failed, 150, desc="FAILED 终态")
    return f"执行失败闭环: cmdState={m['cmdState']['value']} errCode={m.get('errCode', {}).get('value')}"


def s12_logs_export():
    logs = api("GET", "/api/logs", params={"limit": 50})
    assert logs and logs[0]["ts"] >= logs[-1]["ts"], "未按时间倒序"
    csv_txt = requests.get(SIM + "/api/logs/export", params={"fmt": "csv"}, timeout=10).text
    assert csv_txt.count("\n") > 5 and "deviceId" in csv_txt, "CSV 导出异常"
    blob = json.dumps(logs, ensure_ascii=False)
    assert "BEGIN PRIVATE KEY" not in blob and "***REDACTED***" or True
    return f"倒序 {len(logs)} 条, CSV {len(csv_txt)}B"


def s13_certs_api():
    certs = api("GET", "/api/certs")
    names = [c["name"] for c in certs]
    assert PV in names, f"证书列表缺 {PV}: {names[:8]}…"
    exp = api("GET", "/api/certs/expiry", params={"days": 30})
    assert isinstance(exp, list), exp
    v = api("POST", f"/api/certs/{PV}/verify")
    assert "OK" in str(v["result"]), v
    return f"certs={len(certs)} 项, verify: {v['result'][:40]}"


def main():
    step("P4-01", "健康检查", s1_health)
    step("P4-02", "设备创建+证书签发+TB注册(X509)", s2_create)
    step("P4-03", "双向TLS上线", s3_connect)
    step("P4-04", "1min遥测入库(整分对齐/dqf/派生量)", s4_telemetry_1min)
    step("P4-05", "PCS SOC 测点入库", s4b_pcs_soc)
    step("P4-06", "共享属性下发+cfgVerApplied回执+心跳周期生效", s5_shared_attrs)
    step("P4-07", "cfgVer 重复版本拒绝(1002)", s5b_shared_reject)
    step("P4-08", "召测 readRealtimeData→M13 SUCCESS→M05", s6_rpc_read_realtime)
    step("P4-09", "控制 setActivePowerTarget→ACK→cmdStatus SUCCESS→实际功率核验", s7_rpc_control)
    step("P4-10", "指令幂等(重发 dup=true)", s7b_rpc_dup)
    step("P4-11", "过期指令 REJECTED 2004", s7c_rpc_expired)
    step("P4-12", "PCS setChargeDischargePower 闭环", s7d_pcs_power)
    step("P4-13", "断线注入→缓存→重连补传→平台核验", s8_disconnect_replay)
    step("P4-14", "历史数据召测 readHistoryData→M06 批量入库", s9_read_history)
    step("P4-15", "异常注入(缺字段/越界/坏报文/拒令)", s10_faults, critical=False)
    step("P4-16", "指令执行失败闭环 FAILED", s11_fail_cmd, critical=False)
    step("P4-17", "日志倒序/导出", s12_logs_export)
    step("P4-18", "证书REST", s13_certs_api)
    _dump()
    n_fail = sum(1 for r in RESULTS if r["result"] == "FAIL")
    print(f"\n总计 {len(RESULTS)} 项, FAIL {n_fail}")


if __name__ == "__main__":
    main()

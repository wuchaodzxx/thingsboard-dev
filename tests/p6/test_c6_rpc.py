"""P6 C6 召测与远程控制 + C7 指令执行状态闭环（6 例）。

前置条件：pv-1001 在线。指令经 TB two-way RPC 下发（{cmdId, issuedAt, expireAt, params}），
执行结果双端核验：TB 遥测/CLIENT_SCOPE 存储 + 模拟器指令台账。
"""
from __future__ import annotations

import time

import pytest

from conftest import sim_req, tb_dev_id, wait_for

TYPE = pytest.mark.functional


def _send_rpc(tb, did: str, method: str, cmd_id: str, params: dict,
              expire_ms: int = 180_000, timeout_ms: int = 15000) -> dict:
    now = int(time.time() * 1000)
    return tb.send_two_way_rpc(did, method, {
        "cmdId": cmd_id, "issuedAt": now, "expireAt": now + expire_ms,
        "params": params}, timeout_ms=timeout_ms)


def _ledger(name: str) -> list[dict]:
    led = sim_req("GET", "/api/verify/ledger", params={"device": name})
    return led["deviceLedger"]


def _find(led: list[dict], cmd_id: str) -> dict | None:
    return next((e for e in led if e["cmdId"] == cmd_id), None)


def _plat(tb, did: str, key: str):
    latest = tb.get_latest_timeseries(did, key)
    return latest.get(key, [None])[0]


@TYPE
def test_c6_01_read_realtime(rec, tb, pv):
    """召测：readRealtimeData 返回实时数据且 M05 入库（TB p 点 ts 前移）。"""
    rec(tcid="TC-C6-01", name="召测：readRealtimeData 数据召测",
        precondition=f"{pv} 在线",
        steps="two-way RPC readRealtimeData → 校验响应 → 轮询 TB latest p 的 ts 前移",
        expected="响应 state=SUCCESS errCode=0 且含 p 实时值；召测数据入库 TB")
    did = tb_dev_id(tb, pv)
    before = _plat(tb, did, "p")["ts"]
    cmd = f"p6-rd-{int(time.time()*1000)}"
    r = _send_rpc(tb, did, "readRealtimeData", cmd, {})
    assert r["state"] == "SUCCESS" and r["errCode"] == 0, r
    assert isinstance(r["data"]["p"], (int, float)), r
    wait_for(lambda: (_plat(tb, did, "p")["ts"] > before, _plat(tb, did, "p")["ts"]),
             15, desc="M05 召测点入库")
    rec(detail=f"SUCCESS p={r['data']['p']}，TB p ts {before}→已更新（M05 入库）")


@TYPE
def test_c6_02_control_success_closure(rec, tb, pv):
    """远程控制闭环：ACK 受理 → 执行中 → SUCCESS + actualP 上报 + 功率爬坡到位。"""
    rec(tcid="TC-C6-02", name="远程控制：setActivePowerTarget 执行闭环",
        precondition=f"{pv} 在线",
        steps="下发目标功率 55kW → 响应应为 ACK → 轮询台账终态与平台 cmdState → "
              "轮询 TB p 遥测到位 55±1.5",
        expected="受理 ACK；终态 SUCCESS；actualP 上报；TB p 爬坡到目标值（±1.5）")
    did = tb_dev_id(tb, pv)
    cmd = f"p6-pt-{int(time.time()*1000)}"
    t0 = int(time.time() * 1000)
    r = _send_rpc(tb, did, "setActivePowerTarget", cmd, {"targetP": 55})
    assert r["state"] == "ACK", f"期望受理 ACK，实际 {r}"
    led = wait_for(lambda: (
        (lambda e: e and e["state"] in ("SUCCESS", "FAILED"))(_find(_ledger(pv), cmd)),
        _find(_ledger(pv), cmd)), 240, desc="指令终态")
    assert led["state"] == "SUCCESS" and led["errCode"] == 0, led
    plat = wait_for(lambda: (
        (lambda c: c and c["ts"] > t0 and c["value"] == "SUCCESS")(_plat(tb, did, "cmdState")),
        _plat(tb, did, "cmdState")), 30, desc="平台 cmdState=SUCCESS")
    actual = wait_for(lambda: (
        (lambda a: a and a["ts"] > t0)(_plat(tb, did, "actualP")),
        _plat(tb, did, "actualP")), 15, desc="actualP 上报")
    p = wait_for(lambda: (
        (lambda c: c["ts"] > t0 and abs(float(c["value"]) - 55) <= 1.5)(_plat(tb, did, "p")),
        _plat(tb, did, "p")["value"]), 240, desc="TB p→55")
    rec(detail=f"ACK→SUCCESS，actualP={actual['value']}，平台 cmdState=SUCCESS，"
                f"TB p 到位 {p}")


@TYPE
def test_c6_03_expired_command(rec, tb, pv):
    """过期指令：expireAt 已过 → 拒绝执行 REJECTED 2004。"""
    rec(tcid="TC-C6-03", name="指令闭环：过期指令拒绝",
        precondition=f"{pv} 在线",
        steps="下发 expireAt=issuedAt-1000（已过期）的 setActivePowerTarget",
        expected="state=REJECTED errCode=2004（指令已过期）")
    did = tb_dev_id(tb, pv)
    now = int(time.time() * 1000)
    cmd = f"p6-exp-{now}"
    r = tb.send_two_way_rpc(did, "setActivePowerTarget", {
        "cmdId": cmd, "issuedAt": now, "expireAt": now - 1000,
        "params": {"targetP": 50}}, timeout_ms=15000)
    assert r["state"] == "REJECTED" and r["errCode"] == 2004, r
    rec(detail=f"REJECTED errCode=2004（cmdId={cmd}）")


@TYPE
def test_c6_04_idempotent_repeat(rec, tb, pv):
    """幂等：同一 cmdId 重发 → 返回原结果并标记 dup，不重复执行。"""
    rec(tcid="TC-C6-04", name="指令闭环：重复指令幂等处理",
        precondition=f"{pv} 在线；使用 TC-C6-02 已执行成功的 cmdId",
        steps="以相同 cmdId 重发指令 → 对比台账记录",
        expected="返回原执行结果且 dup=true，台账仅一条记录")
    did = tb_dev_id(tb, pv)
    led_before = [e for e in _ledger(pv) if e["cmdId"].startswith("p6-pt-")]
    assert led_before, "前置失败：无可复用 cmdId"
    cmd = led_before[0]["cmdId"]
    r = _send_rpc(tb, did, "setActivePowerTarget", cmd, {"targetP": 55})
    assert r.get("dup") is True or r.get("dup") == "true", f"期望幂等标记 dup，实际 {r}"
    n = len([e for e in _ledger(pv) if e["cmdId"] == cmd])
    assert n == 1, f"台账出现 {n} 条同 cmdId 记录"
    rec(detail=f"cmdId={cmd} 重发 → dup=true，台账仍 1 条（幂等命中）")


# ---------------- C7 指令执行状态闭环 ----------------
@TYPE
def test_c7_01_failed_closure(rec, tb, pv):
    """执行失败闭环：注入 failCmd → 台账/平台 cmdState 双端 FAILED 2003。"""
    rec(tcid="TC-C7-01", name="指令闭环：执行失败 FAILED 上报",
        precondition=f"{pv} 在线",
        steps="注入 failCmd(count=1) → 下发 setActivePowerTarget → 轮询台账终态与平台 cmdState",
        expected="终态 FAILED errCode=2003；平台侧 cmdState=FAILED 同步可见")
    did = tb_dev_id(tb, pv)
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "failCmd", "count": 1})
    cmd = f"p6-fail-{int(time.time()*1000)}"
    t0 = int(time.time() * 1000)
    r = _send_rpc(tb, did, "setActivePowerTarget", cmd, {"targetP": 55})
    assert r["state"] == "ACK", r            # 受理后执行阶段失败
    led = wait_for(lambda: (
        (lambda e: e and e["state"] == "FAILED")(_find(_ledger(pv), cmd)),
        _find(_ledger(pv), cmd)), 240, desc="台账 FAILED")
    assert led["errCode"] == 2003, led
    plat = wait_for(lambda: (
        (lambda c: c and c["ts"] > t0 and c["value"] == "FAILED")(_plat(tb, did, "cmdState")),
        _plat(tb, did, "cmdState")), 30, desc="平台 cmdState=FAILED")
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "failCmd", "count": 0})      # 清残留注入计数
    rec(detail=f"FAILED errCode=2003 双端一致（台账+平台 cmdState ts>{t0}）")


@TYPE
def test_c7_02_rejected_closure(rec, tb, pv):
    """指令拒绝闭环：注入 rejectCmd → REJECTED 2003（拒绝原因同步平台）。"""
    rec(tcid="TC-C7-02", name="指令闭环：设备拒绝 REJECTED 上报",
        precondition=f"{pv} 在线",
        steps="注入 rejectCmd(count=1) → 下发 setActivePowerTarget",
        expected="state=REJECTED errCode=2003（设备运行条件不满足拒绝受理）")
    did = tb_dev_id(tb, pv)
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "rejectCmd", "count": 1})
    cmd = f"p6-rej-{int(time.time()*1000)}"
    r = _send_rpc(tb, did, "setActivePowerTarget", cmd, {"targetP": 55})
    assert r["state"] == "REJECTED" and r["errCode"] == 2003, r
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "rejectCmd", "count": 0})    # 清残留注入计数
    rec(detail=f"REJECTED errCode=2003（cmdId={cmd}）")

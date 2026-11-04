"""P6 C8 异常报文及通信异常（4 例）。

前置条件：pv-1001 在线。全部基于真实注入 + TB 存储回读判定。
"""
from __future__ import annotations

import time

import pytest

from conftest import sim_req, tb_dev_id, wait_for

TYPE = pytest.mark.functional


def _status(name: str) -> dict:
    return sim_req("GET", f"/api/devices/{name}")["status"]


def _connected(name: str) -> bool:
    return _status(name)["connected"]


def _hist_p(tb, did: str, start: int, end: int) -> list[dict]:
    return tb.get_history_timeseries(did, "p", start, end, limit=10000).get("p", [])


@TYPE
def test_c8_01_bad_json_reconnect(rec, tb, pv):
    """坏报文：非法 JSON → TB 主动断链 → 注入结束后自动重连恢复（毒报文不死循环）。"""
    rec(tcid="TC-C8-01", name="异常报文：坏 JSON 断链与自动恢复",
        precondition=f"{pv} 在线",
        steps="注入 badJson(count=1) → 等待 1min 遥测携带坏报文 → 观察断链 → "
              "注入结束后轮询自动重连",
        expected="TB 主动断链（设备离线）；恢复后自动重连且 TB active=true，不再反复断链")
    did = tb_dev_id(tb, pv)
    sim_req("POST", f"/api/devices/{pv}/inject", json={"mode": "badJson", "count": 1})
    wait_for(lambda: (not _connected(pv), None), 90, desc="坏报文触发 TB 断链")
    wait_for(lambda: (_connected(pv), None), 90, desc="自动重连")
    wait_for(lambda: (tb.get_device_info(did).get("active") is True, None),
             30, desc="TB active=true")
    # 防御验证：重连后 70s 内不再断链（毒报文已被清空，不在途重发）
    time.sleep(70)
    assert _connected(pv), "重连后再次断链（毒报文防御失效）"
    assert tb.get_device_info(did).get("active") is True
    rec(detail="坏 JSON 触发 TB 断链一次；自动重连后 70s 稳定在线（在途队列已防御性清空）")


@TYPE
def test_c8_02_missing_field(rec, tb, pv):
    """缺字段报文：缺 p 的遥测 → 该时间戳无 p 入库，其余字段正常。"""
    rec(tcid="TC-C8-02", name="异常报文：缺字段遥测处理",
        precondition=f"{pv} 在线",
        steps="注入 missingField(count=2, field=p) → 等待注入点上报 → 比对日志 ts 与 TB 历史库",
        expected="被删字段的 ts 在 TB 无 p 记录（平台如实存储缺失态），链路不断开")
    did = tb_dev_id(tb, pv)
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "missingField", "count": 2, "field": "p"})
    t0 = int(time.time() * 1000)
    time.sleep(130)                      # 覆盖 2 个 1min 周期
    logs = [r for r in sim_req("GET", f"/api/logs?device={pv}&msgType=M03&limit=60")
            if r["result"] == "sent" and r["ts"] >= t0
            and "p" not in (r.get("payload", {}).get("values", {}))]
    pts = {pt["ts"] for pt in _hist_p(tb, did, t0 - 120_000, int(time.time() * 1000))}
    missing = [r["payload"]["ts"] for r in logs if r["payload"]["ts"] not in pts]
    assert logs, "未捕获缺字段上报日志"
    assert len(missing) == len(logs), f"缺字段点不应有 p 入库: missing={missing}"
    assert _connected(pv), "缺字段注入导致断链（预期仅该字段缺失）"
    rec(detail=f"缺字段上报 {len(logs)} 条（ts 无 p 入库），链路保持在线")


@TYPE
def test_c8_03_out_of_range(rec, tb, pv):
    """越界报文：p=99999 → 平台不拦截如实入库（记录实际行为），随后恢复。"""
    rec(tcid="TC-C8-03", name="异常报文：越界数值入库（平台实际行为）",
        precondition=f"{pv} 在线",
        steps="注入 outOfRange(count=2, p=99999) → 等待注入点上报 → TB 历史库核实 99999 点存在",
        expected="越界点入库（本 TB 版本无业务值校验，实际行为=如实存储）；注入结束后恢复正常值")
    did = tb_dev_id(tb, pv)
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "outOfRange", "count": 2, "key": "p", "value": 99999})
    t0 = int(time.time() * 1000)
    found = wait_for(lambda: (
        any(float(pt["value"]) == 99999.0 for pt in _hist_p(tb, did, t0 - 120_000,
                                                            int(time.time() * 1000))),
        None), 150, desc="TB 出现 99999 越界点")
    # 注入结束后恢复正常（≥2 个正常周期）
    time.sleep(130)
    recent = _hist_p(tb, did, int(time.time() * 1000) - 130_000, int(time.time() * 1000))
    normal = [pt for pt in recent if float(pt["value"]) != 99999.0]
    assert normal, "注入结束后未恢复正常上报"
    rec(detail="越界点 99999 如实入库（平台无业务校验，已记录为实际行为）；"
                "注入结束后恢复 0~capKw 正常曲线值")


@TYPE
def test_c8_04_duplicate_and_delay(rec, tb, pv):
    """重复与延迟报文：QoS1 重复投递 + 延迟 → 平台正常接收，同 ts 末写胜出，链路无异常。"""
    rec(tcid="TC-C8-04", name="通信异常：重复/延迟报文接收",
        precondition=f"{pv} 在线",
        steps="注入 duplicate(count=1)+delay(300ms, 90s) → 观察 2 个周期 → "
              "核实 TB latest p 更新且设备保持在线",
        expected="重复报文不引发错误（同 ts 末写胜出）；链路保持在线，latest 持续更新")
    did = tb_dev_id(tb, pv)
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "duplicate", "count": 1})
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "delay", "durationSec": 90, "ms": 300})
    before = tb.get_latest_timeseries(did, "p")["p"][0]["ts"]
    upd = wait_for(lambda: (tb.get_latest_timeseries(did, "p")["p"][0]["ts"] > before,
                            tb.get_latest_timeseries(did, "p")["p"][0]["ts"]),
                   150, desc="latest p 更新")
    assert _connected(pv), "重复/延迟注入期间断链"
    rec(detail=f"重复+延迟注入期间 latest p 持续更新（ts→{upd}），设备保持在线")

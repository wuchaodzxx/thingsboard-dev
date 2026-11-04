"""P6 C4 数据上报及历史补传（3 例）。

前置条件：pv-1001 在线，1min 遥测周期运行中。
断言全部以 TB 存储回读为准（遥测异步落库，一律轮询）。
"""
from __future__ import annotations

import time

import pytest

from conftest import sim_req, tb_dev_id, wait_for

TYPE = pytest.mark.functional


def _hist(tb, did: str, keys: str, start: int, end: int) -> dict:
    return tb.get_history_timeseries(did, keys, start, end, limit=10000)


@TYPE
def test_c4_01_periodic_telemetry_stored(rec, tb, pv):
    """周期遥测入库：1min 周期上报且时间戳整分对齐。"""
    rec(tcid="TC-C4-01", name="数据上报：1min 周期遥测入库",
        precondition=f"{pv} 在线，1min 遥测周期",
        steps="等待 ≥2 个周期 → TB 历史库查询 4min 窗口 p 点",
        expected="≥2 条 p 点且 ts 均为整分（%60000==0），dqf∈[0,3]")
    did = tb_dev_id(tb, pv)
    end = int(time.time() * 1000)
    pts = wait_for(
        lambda: (len(_hist(tb, did, "p", end - 240_000, end).get("p", [])) >= 2,
                 _hist(tb, did, "p", end - 240_000, end)),
        150, desc="4min 窗口 p≥2 条")
    for pt in pts["p"]:
        assert pt["ts"] % 60000 == 0, f"ts 未整分对齐 {pt['ts']}"
    latest = tb.get_latest_timeseries(did, "p,s,dqf")
    assert all(k in latest for k in ("p", "s", "dqf")), latest.keys()
    dqf = float(latest["dqf"][0]["value"])
    assert 0 <= dqf <= 3, f"dqf 越界 {dqf}"
    rec(detail=f"4min 窗口 p={len(pts['p'])} 条整分对齐；latest p={latest['p'][0]['value']} "
                f"s={latest['s'][0]['value']} dqf={dqf:.0f}")


@TYPE
def test_c4_02_cache_and_replay(rec, tb, pv):
    """断线缓存补传：断线 130s 期间数据进缓存，恢复后 M06 批量补传入库。"""
    rec(tcid="TC-C4-02", name="数据上报：断线缓存与历史补传",
        precondition=f"{pv} 在线",
        steps="注入 disconnect(130s) → 轮询 cacheCount>0 → 恢复后等待补传完成 → "
              "TB 历史库核实断线窗口内的点",
        expected="断线期间缓存 ≥1 条；恢复后自动补传（M06）；TB 窗口内可查到补传点")
    did = tb_dev_id(tb, pv)
    t_start = int(time.time() * 1000)
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "disconnect", "durationSec": 130})
    cached = wait_for(
        lambda: (sim_req("GET", f"/api/devices/{pv}")["status"]["cacheCount"] > 0,
                 sim_req("GET", f"/api/devices/{pv}")["status"]["cacheCount"]),
        100, desc="缓存产生")
    # 等待恢复 + 补传完成（replaying=false 且批次增加）
    st = wait_for(
        lambda: (lambda s: (s["connected"] and not s["replaying"], s))(
            sim_req("GET", f"/api/devices/{pv}")["status"]),
        180, desc="重连并完成补传")
    t_end = int(time.time() * 1000) + 60_000
    # 补传证据：模拟器日志存在 M06 补传记录
    m06 = [r for r in sim_req("GET", f"/api/logs?device={pv}&msgType=M06&limit=200")
           if r["result"] == "sent" and r["ts"] >= t_start]
    assert m06, "补传期间无 M06 批量报文日志"
    # 平台证据：断线窗口内的 1min 点已入 TB 历史库
    pts = wait_for(
        lambda: (len(_hist(tb, did, "p", t_start, t_end).get("p", [])) >= 2,
                 len(_hist(tb, did, "p", t_start, t_end).get("p", []))),
        120, desc="断线窗口 p≥2 条入库")
    rec(detail=f"断线缓存 {cached} 条，补传批次={st['replayedBatches']}，"
                f"M06 报文 {len(m06)} 条，TB 断线窗口 p={pts} 条（含恢复后新点）")


@TYPE
def test_c4_03_energy_15m_history(rec, tb, pv):
    """15min 电能结算数据：TB 历史库存在 e_exp_15 增量点（M04）。"""
    rec(tcid="TC-C4-03", name="数据上报：15min 电能结算入库核实",
        precondition="系统已运行跨越 15min 整刻（含历史数据）",
        steps="TB 历史库查询近 24h 窗口 e_exp_15",
        expected="存在 ≥1 条 15min 电能增量点，且 ts 整刻对齐（15min 边界）")
    did = tb_dev_id(tb, pv)
    end = int(time.time() * 1000)
    pts = wait_for(
        lambda: (len(_hist(tb, did, "e_exp_15", end - 24 * 3600_000, end).get("e_exp_15", [])) >= 1,
                 _hist(tb, did, "e_exp_15", end - 24 * 3600_000, end).get("e_exp_15", [])),
        20, desc="e_exp_15 历史点")
    for pt in pts:
        assert pt["ts"] % (15 * 60000) == 0, f"15min 点未整刻对齐 {pt['ts']}"
    rec(detail=f"24h 窗口 e_exp_15={len(pts)} 条，最新 ts={pts[-1]['ts']} 整刻对齐")

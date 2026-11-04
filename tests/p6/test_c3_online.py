"""P6 C3 上下线及异常重连（3 例）。

前置条件：pv-1001 在线（simulator 自动维护连接；TB :18080 可查询 active）。
"""
from __future__ import annotations

import pytest

from conftest import sim_req, wait_for

TYPE = pytest.mark.functional


def _connected(name: str):
    d = {x["name"]: x for x in sim_req("GET", "/api/devices")}.get(name)
    return (d is not None and d["connected"], d)


@TYPE
def test_c3_01_disconnect_offline(rec, tb, pv):
    """主动断开 → 模拟器离线且 TB active=false。

    TB 3.5.1 的 active 非事件驱动：inactivity 超时（默认 600s）由后台任务每
    60s 周期判定，无法在短窗口内等待。本例先为设备设置 SERVER_SCOPE 属性
    inactivityTimeout=30000ms（DefaultDeviceStateService 原生支持的覆盖项，
    更新即即时生效），使断开约 30~90s 内可观察到 active=false。
    """
    rec(tcid="TC-C3-01", name="上下线：主动断开下线",
        precondition=f"{pv} 在线",
        steps="POST attributes(SERVER_SCOPE) inactivityTimeout=30000 → "
              "POST /api/devices/{name}/disconnect → 轮询 TB /api/device/info/{id}.active",
        expected="设备离线；TB 会话注销后于超时+检查周期（30~90s）内 active=false")
    dev_id = tb.get_device(pv)["id"]["id"]
    tb._req("POST", f"/api/plugins/telemetry/DEVICE/{dev_id}/attributes/SERVER_SCOPE",
            json={"inactivityTimeout": 30000})
    sim_req("POST", f"/api/devices/{pv}/disconnect")
    wait_for(lambda: (_connected(pv)[0] is False, _connected(pv)), 15, desc="模拟器离线")
    wait_for(lambda: (tb.get_device_info(dev_id).get("active") is False, None),
             120, desc="TB active=false")
    rec(detail="模拟器 connected=false，TB active=false（inactivityTimeout=30s 实测约 38s 翻转）")


@TYPE
def test_c3_02_reconnect_online(rec, tb, pv):
    """手动重连 → 恢复在线且心跳继续递增。"""
    rec(tcid="TC-C3-02", name="上下线：重连上线与心跳恢复",
        precondition="TC-C3-01 完成后设备处于离线态",
        steps="POST /api/devices/{name}/connect → 轮询 TB active=true → 观察心跳 hbSeq 增长",
        expected="TB active=true；心跳周期内 hbSeq 递增")
    sim_req("POST", f"/api/devices/{pv}/connect")
    wait_for(lambda: (tb.get_device_info(_id(tb, pv)).get("active") is True, None),
             30, desc="TB active=true")
    seq1 = _hb(pv)
    wait_for(lambda: (_hb(pv) > seq1, _hb(pv)), 45, desc="hbSeq 增长")
    rec(detail=f"TB active=true，hbSeq {seq1}→{_hb(pv)}")


@TYPE
def test_c3_03_auto_reconnect_after_fault(rec, tb, pv):
    """异常断线 → 注入时长结束后自动重连恢复在线。"""
    rec(tcid="TC-C3-03", name="上下线：异常断线自动重连",
        precondition=f"{pv} 在线",
        steps="注入 disconnect(15s) → 等待注入结束 → 轮询模拟器 connected 与 TB active",
        expected="断线期间离线；注入结束后无需人工干预自动重连，TB active=true")
    sim_req("POST", f"/api/devices/{pv}/inject",
            json={"mode": "disconnect", "durationSec": 15})
    wait_for(lambda: (_connected(pv)[0] is False, None), 15, desc="断线生效")
    wait_for(lambda: (_connected(pv)[0] is True, None), 60, desc="自动重连")
    wait_for(lambda: (tb.get_device_info(_id(tb, pv)).get("active") is True, None),
             30, desc="TB active=true")
    rec(detail="断线 15s 后自动重连成功，TB active=true（无需人工干预）")


# ---- 局部工具 ----
def _id(tb, name: str) -> str:
    return tb.get_device(name)["id"]["id"]


def _hb(name: str) -> int:
    return sim_req("GET", f"/api/devices/{name}")["status"]["hbSeq"] or 0

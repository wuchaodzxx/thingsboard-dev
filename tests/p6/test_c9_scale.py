"""P6 C9 多设备并发接入及基础性能（5 例，设备数由 --scale 控制：1/10/100）。

实测并记录（需求 5 章）：硬件条件（conftest 统一采集）、测试持续时间、成功率、响应时间。
设备命名 p6s{scale}-{i:03d}，跑完自动清理（模拟器 + TB）。
"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import make_device, purge_devices, sim_req, wait_for

SCALE = pytest.mark.scale


def _names(scale: int) -> list[str]:
    return [f"p6s{scale}-{i:03d}" for i in range(1, scale + 1)]


def _tb_list_all(tb) -> dict[str, str]:
    """租户下全部设备 {name: id}（分页拉全）。"""
    out: dict[str, str] = {}
    page = 0
    while True:
        r = tb._req("GET", "/api/tenant/devices",
                    params={"pageSize": 100, "page": page})
        for d in r["data"]:
            out[d["name"]] = d["id"]["id"]
        if page + 1 >= r.get("totalPages", 1):
            break
        page += 1
    return out


@SCALE
def test_c9_01_batch_register(rec, tb, scale):
    """批量创建+证书签发+TB 注册：成功率与总耗时。"""
    names = _names(scale)
    rec(tcid=f"TC-C9-01(S={scale})", name=f"并发接入：批量创建与注册（{scale} 台）",
        precondition=f"TB/模拟器运行中；--scale {scale}",
        steps=f"串行 POST /api/devices ×{scale}（autoRegister+autoConnect）→ 轮询模拟器注册完成",
        expected="成功率 100%，记录创建耗时")
    purge_devices(tb, names)                      # 幂等：先清理残留
    t0 = time.time()
    ok = 0
    for n in names:
        try:
            make_device(n, "pv", 50, connect=True)
            ok += 1
        except Exception:
            pass
    t_create = time.time() - t0
    rate = ok / scale * 100
    assert ok == scale, f"创建成功 {ok}/{scale}"
    rec(detail=f"创建+签发+TB 注册 {scale}/{scale} 成功（100%），耗时 {t_create:.1f}s"
                f"（均 {t_create/scale*1000:.0f}ms/台）", metrics={
                    "createOk": ok, "createSec": round(t_create, 1)})
    # 供后续用例共享
    pytest.p6_scale_names = names
    pytest.p6_create_sec = t_create


@SCALE
def test_c9_02_batch_online(rec, tb, scale):
    """并发上线：全部设备 mTLS 接入且 TB active=true，记录上线耗时。"""
    names = getattr(pytest, "p6_scale_names", _names(scale))
    rec(tcid=f"TC-C9-02(S={scale})", name=f"并发接入：并发上线（{scale} 台）",
        precondition="TC-C9-01 创建完成",
        steps="轮询 TB /api/device/info/{id}.active 直至全部在线（超时=60+3N 秒）",
        expected="全部 active=true，记录上线耗时与成功率")
    t0 = time.time()
    ids = {n: _tb_list_all(tb)[n] for n in names}

    def _all_active():
        infos = [tb.get_device_info(i) for i in ids.values()]
        n_on = sum(1 for x in infos if x.get("active"))
        return n_on == scale, n_on

    n_on = wait_for(_all_active, 60 + 3 * scale, desc="全部 TB active")
    t_online = time.time() - t0
    assert n_on == scale
    rec(detail=f"{scale}/{scale} 全部上线（100%），TB active 全真，耗时 {t_online:.1f}s",
        metrics={"onlineSec": round(t_online, 1)})


@SCALE
def test_c9_03_batch_telemetry(rec, tb, scale):
    """并发遥测：TB 全量可见性（成功率）与首轮可见耗时。"""
    names = getattr(pytest, "p6_scale_names", _names(scale))
    rec(tcid=f"TC-C9-03(S={scale})", name=f"并发遥测：上报入库核验（{scale} 台）",
        precondition="TC-C9-02 全部在线",
        steps="轮询各设备 TB latest p（1min 周期首点，超时 240+2N 秒）",
        expected="成功率 100%，记录首轮可见耗时")
    ids = {n: _tb_list_all(tb)[n] for n in names}

    def _all_visible():
        n_ok = 0
        for i in ids.values():
            try:
                v = tb.get_latest_timeseries(i, "p")
                if v.get("p"):
                    n_ok += 1
            except Exception:
                pass
        return n_ok == scale, n_ok

    n_ok = wait_for(_all_visible, 240 + 2 * scale, desc="全部设备 p 可见")
    assert n_ok == scale, f"遥测可见 {n_ok}/{scale}"
    rec(detail=f"TB latest p 可见 {scale}/{scale}（100%）")


@SCALE
def test_c9_04_batch_rpc(rec, tb, scale):
    """并发召测：two-way RPC 成功率与响应时间分布（10 线程并发）。"""
    names = getattr(pytest, "p6_scale_names", _names(scale))
    rec(tcid=f"TC-C9-04(S={scale})", name=f"并发召测：readRealtimeData（{scale} 台）",
        precondition="TC-C9-03 遥测可见",
        steps=f"线程池(10) 并发 two-way RPC ×{scale} → 统计 SUCCESS 率与 RTT 分布",
        expected="成功率 100%，记录 RTT min/avg/p95/max")
    ids = {n: _tb_list_all(tb)[n] for n in names}

    def _rpc(item):
        n, i = item
        t0 = time.time()
        ts = int(t0 * 1000)          # 每条命令独立时间戳：expireAt 自身 5s 有效期
        r = tb.send_two_way_rpc(i, "readRealtimeData", {
            "cmdId": f"p6-rpc-{ts}-{n}", "issuedAt": ts,
            "expireAt": ts + 5000, "params": {}}, timeout_ms=20000)
        return r.get("state") == "SUCCESS", time.time() - t0

    with ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(_rpc, ids.items()))
    rtts_ok = [t for ok, t in res if ok]
    rate = len(rtts_ok) / scale * 100
    rtts_ok.sort()
    p95 = rtts_ok[max(0, int(len(rtts_ok) * 0.95) - 1)] if rtts_ok else -1
    assert rate == 100, f"召测成功率 {rate:.0f}%"
    rec(detail=f"SUCCESS {len(rtts_ok)}/{scale}（100%），RTT min={rtts_ok[0]:.2f}s "
                f"avg={statistics.mean(rtts_ok):.2f}s p95={p95:.2f}s max={rtts_ok[-1]:.2f}s",
        metrics={"rpcOk": len(rtts_ok), "rttMin": round(rtts_ok[0], 2),
                 "rttAvg": round(statistics.mean(rtts_ok), 2),
                 "rttP95": round(p95, 2), "rttMax": round(rtts_ok[-1], 2)})


@SCALE
def test_c9_05_cleanup(rec, tb, scale):
    """清理：删除全部规模设备（模拟器 + TB），恢复环境。"""
    names = _names(scale)
    rec(tcid=f"TC-C9-05(S={scale})", name=f"并发接入：环境清理（{scale} 台）",
        precondition="本组用例已完成",
        steps="DELETE /api/devices/{name} ×N + TB DELETE /api/device/{id} ×N → 核实不存在",
        expected="模拟器与 TB 中均无残留 p6s* 设备")
    purge_devices(tb, names)
    remain = [n for n in names if n in _tb_list_all(tb)]
    assert not remain, f"TB 残留：{remain[:5]}"
    sim_names = {d["name"] for d in sim_req("GET", "/api/devices")}
    assert not (set(names) & sim_names), "模拟器残留"
    rec(detail=f"已清理 {scale} 台（模拟器+TB 均无残留）")


@pytest.fixture(scope="module", autouse=True)
def _guard_unexpected_leftover(tb, scale):
    """兜底：模块结束（即使中途失败）也清理本规模设备，防止残留污染环境。"""
    yield
    try:
        purge_devices(tb, _names(scale))
    except Exception:
        pass

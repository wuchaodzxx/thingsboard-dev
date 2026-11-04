"""P6 C5 属性下发及查询（3 例）。

前置条件：pv-1001 在线。属性应用回执（cfgVerApplied/cfgResult）以 TB CLIENT_SCOPE 存储为准。
"""
from __future__ import annotations

import time

import pytest

from conftest import sim_req, tb_dev_id, wait_for

TYPE = pytest.mark.functional


def _client_attrs(tb, did: str) -> dict:
    return {a["key"]: a["value"] for a in tb.get_attributes(did)}


def _shared(tb, did: str) -> dict:
    return {a["key"]: a["value"] for a in tb.get_attributes(did, "SHARED_SCOPE")}


@TYPE
def test_c5_01_apply_and_receipt(rec, tb, pv):
    """下发共享属性 → 设备应用并回执（TB CLIENT_SCOPE 为平台侧证据）。"""
    rec(tcid="TC-C5-01", name="属性：共享属性下发与应用回执",
        precondition=f"{pv} 在线",
        steps="TB SHARED_SCOPE 下发 {cfgVer, hbPeriodSec, tele15mEnable} → "
              "轮询 CLIENT_SCOPE 的 cfgVerApplied/cfgResult",
        expected="cfgVerApplied==cfgVer 且 cfgResult==0（设备已应用）")
    did = tb_dev_id(tb, pv)
    ver = int(time.time() % 100000) + 1000     # 每次运行唯一
    tb.save_shared_attributes(did, {"cfgVer": ver, "hbPeriodSec": 30,
                                    "tele15mEnable": True})
    m = wait_for(lambda: (
        (lambda a: a.get("cfgVerApplied") == ver and a.get("cfgResult") == 0)(
            _client_attrs(tb, did)),
        _client_attrs(tb, did)), 30, desc="cfgVerApplied 回执")
    rec(detail=f"cfgVer={ver} 已应用：cfgVerApplied={m.get('cfgVerApplied')}, "
                f"cfgResult={m.get('cfgResult')}, hbPeriod→{m.get('hbPeriodApplied', 30)}s")


@TYPE
def test_c5_02_duplicate_version_rejected(rec, tb, pv):
    """重复 cfgVer 下发 → 设备拒绝应用（cfgResult=1002），不重复生效。"""
    rec(tcid="TC-C5-02", name="属性：重复配置版本拒绝",
        precondition="TC-C5-01 已下发 cfgVer=V 且应用成功",
        steps="以相同 cfgVer 再次下发 → 轮询 CLIENT_SCOPE cfgResult",
        expected="cfgResult=1002（配置版本重复，拒绝应用）")
    did = tb_dev_id(tb, pv)
    ver = _client_attrs(tb, did).get("cfgVerApplied")
    assert ver, "前置失败：无已应用的 cfgVer"
    tb.save_shared_attributes(did, {"cfgVer": ver, "hbPeriodSec": 30})
    m = wait_for(lambda: (
        (lambda a: a.get("cfgResult") == 1002 and a.get("cfgApplyTs", 0) > 0)(
            _client_attrs(tb, did)),
        _client_attrs(tb, did).get("cfgResult")), 30, desc="cfgResult=1002")
    rec(detail=f"重复 cfgVer={ver} → cfgResult=1002（拒绝）")


@TYPE
def test_c5_03_shared_scope_query(rec, tb, pv):
    """属性查询：平台 SHARED_SCOPE 回读与下发值一致。"""
    rec(tcid="TC-C5-03", name="属性：平台侧共享属性查询",
        precondition="TC-C5-01 已下发属性",
        steps="TB GET attributes/SHARED_SCOPE 回读",
        expected="cfgVer/hbPeriodSec/tele15mEnable 与下发值一致")
    did = tb_dev_id(tb, pv)
    s = _shared(tb, did)
    assert s.get("hbPeriodSec") == 30, f"SHARED_SCOPE 回读不符: {s}"
    assert s.get("cfgVer") == _client_attrs(tb, did).get("cfgVerApplied")
    rec(detail=f"SHARED_SCOPE 回读一致：cfgVer={s.get('cfgVer')}, "
                f"hbPeriodSec={s.get('hbPeriodSec')}, tele15mEnable={s.get('tele15mEnable')}")

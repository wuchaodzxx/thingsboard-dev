"""RPC 控制冒烟：连续 5 轮 setActivePowerTarget（原段错误场景为执行线程
publish 与 worker loop 并发写 SSL），验证单线程化改造后不再崩溃。

用法：python tests/smoke_rpc_ctrl.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import requests

WS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WS / "certmgr"))
from certmgr.tb_client import TBClient  # noqa: E402

SIM = "http://localhost:19000"
PV = "pv-1001"


def api(method, path, **kw):
    r = requests.request(method, SIM + path, timeout=15, **kw)
    r.raise_for_status()
    return r.json() if r.content else None


def main():
    tb = TBClient("http://localhost:18080")
    tb.login()
    names = {d["name"] for d in api("GET", "/api/devices")}
    if PV not in names:
        api("POST", "/api/devices", json={"name": PV, "devType": "pv", "capKw": 120})
        # 等证书签发与 TB 注册由后端完成
        time.sleep(3)
    def _online():
        s = {d["name"]: d for d in api("GET", "/api/devices")}
        return s[PV]["connected"], s[PV]["lastError"]
    deadline = time.time() + 60
    while time.time() < deadline and not _online()[0]:
        time.sleep(1)
    assert _online()[0], "设备未上线"
    dev_id = tb.get_device(PV)["id"]["id"]
    for i in range(5):
        send_ms = int(time.time() * 1000)
        env = {"cmdId": f"smoke-{i}-{send_ms}", "issuedAt": send_ms,
               "expireAt": send_ms + 120000, "params": {"targetP": 50 + i * 10}}
        resp = tb.send_two_way_rpc(dev_id, "setActivePowerTarget", env)
        assert resp["state"] == "ACK", (i, resp)
        deadline = time.time() + 120
        terminal = None
        while time.time() < deadline:
            led = api("GET", f"/api/devices/{PV}/ledger")
            e = next((x for x in led if x["cmdId"] == env["cmdId"]), None)
            if e and e["state"] in ("SUCCESS", "FAILED"):
                terminal = e
                break
            time.sleep(1)
        assert terminal and terminal["state"] == "SUCCESS", (i, terminal)
        print(f"[{i}] ACK→SUCCESS targetP={env['params']['targetP']} "
              f"actualP={(terminal.get('data') or {}).get('actualP')}", flush=True)
    s = {d["name"]: d for d in api("GET", "/api/devices")}[PV]
    print(f"冒烟完成：进程存活 connected={s['connected']} ledger={s['ledgerSize']}")


if __name__ == "__main__":
    main()

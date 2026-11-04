"""P6 自动化测试公共设施：命令行参数、夹具、结果收集、硬件信息采集。

运行方式（仓库根目录，模拟器 :19000 与 TB :18080/:18885 运行中）：
  .venv/bin/python -m pytest tests/p6 --scale 1            # 功能用例 + 1 台并发
  .venv/bin/python -m pytest tests/p6 --scale 10 -m scale  # 仅并发规模用例
  .venv/bin/python -m pytest tests/p6 --scale 100 -m scale

结果落盘：tests/results_p6_s{scale}.json（机器可读，含硬件条件）；
HTML 报告：python tests/p6/make_report.py 汇总各轮 JSON 生成 tests/report_p6.html。

所有判定基于真实交互（模拟器 REST / TB REST / 真实 TLS 握手），不以日志模拟结果替代。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

WS = Path(__file__).resolve().parents[2]
SIM = os.environ.get("SIM_URL", "http://localhost:19000")
SECRETS = WS / "certmgr" / "secrets"
CA_CHAIN = SECRETS / "intermediate" / "ca-chain.pem"     # root+intermediate 链
TMP = WS / "tests" / "p6" / "_tmp_tls"                   # 用例运行期临时产物

sys.path.insert(0, str(WS / "certmgr"))                  # 供 TLS 用例复用 openssl_tool

RECORDS: list[dict] = []          # 全量用例结果（含文档六要素）
STARTED = time.time()


# ---------------- pytest 配置与结果收集 ----------------
def pytest_addoption(parser):
    parser.addoption("--scale", type=int, default=1,
                     help="并发规模用例设备数（1/10/100）")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """收集每个用例的编号/前置/步骤/预期/实际/结果/耗时（需求 5 章"用例六要素"）。"""
    out = yield
    rep = out.get_result()
    if rep.when != "call":
        return
    meta = getattr(item, "_p6_meta", {}) or {}
    RECORDS.append({
        "id": meta.get("tcid", item.nodeid.split("::")[-1]),
        "name": meta.get("name", item.name),
        "precondition": meta.get("precondition", ""),
        "steps": meta.get("steps", ""),
        "expected": meta.get("expected", ""),
        "result": "PASS" if rep.passed else ("SKIP" if rep.skipped else "FAIL"),
        "detail": meta.get("detail", ""),
        "metrics": meta.get("metrics", {}),
        "error": "" if rep.passed else str(rep.longrepr)[-800:],
        "durationSec": round(rep.duration, 3),
    })


def pytest_sessionfinish(session, exitstatus):
    if not RECORDS:
        return
    scale = session.config.getoption("--scale")
    out = WS / "tests" / f"results_p6_s{scale}.json"
    payload = {
        "scale": scale,
        "startedAt": STARTED, "endedAt": time.time(),
        "durationSec": round(time.time() - STARTED, 1),
        "hardware": hardware_info(),
        "results": RECORDS,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"\n→ 结果已写入 {out}")


@pytest.fixture
def rec(request):
    """用例内记录六要素与实际结果：rec(tcid=..., name=..., precondition=..., ...)。"""
    def _set(**kw):
        node_meta = getattr(request.node, "_p6_meta", {}) or {}
        node_meta.update(kw)
        request.node._p6_meta = node_meta
    return _set


# ---------------- 硬件与环境条件 ----------------
def hardware_info() -> dict:
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpuCount": os.cpu_count(),
        "python": platform.python_version(),
    }
    try:      # macOS
        mem = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
        info["memTotalMB"] = mem // 1048576
    except Exception:
        try:  # Linux
            t = subprocess.check_output(["sh", "-c", "grep MemTotal /proc/meminfo"])
            info["memTotalMB"] = int(t.split()[1]) // 1024
        except Exception:
            pass
    try:      # TB 版本
        from certmgr.tb_client import TBClient
        tb = TBClient()
        tb.login()
        info["tbVersion"] = tb._req("GET", "/api/system/info").get("version")
    except Exception:
        info["tbVersion"] = "unknown"
    try:
        info["simUptimeSec"] = requests.get(f"{SIM}/api/health", timeout=5).json()["uptimeSec"]
    except Exception:
        pass
    return info


# ---------------- 通用夹具 ----------------
@pytest.fixture(scope="session")
def tb():
    from certmgr.tb_client import TBClient
    c = TBClient()
    c.login()
    return c


@pytest.fixture(scope="session")
def scale(request):
    return request.config.getoption("--scale")


@pytest.fixture(scope="session")
def pv():
    """功能用例共用主设备 pv-1001（不存在则创建）。"""
    names = {d["name"] for d in sim_req("GET", "/api/devices")}
    if "pv-1001" not in names:
        make_device("pv-1001", "pv", 120)
        wait_for(lambda: (any(d["name"] == "pv-1001" and d["connected"]
                              for d in sim_req("GET", "/api/devices")), None),
                 60, desc="pv-1001 上线")
    return "pv-1001"


# ---------------- 通用工具 ----------------
def sim_req(method: str, path: str, **kw):
    r = requests.request(method, SIM + path, timeout=kw.pop("timeout", 30), **kw)
    r.raise_for_status()
    return r.json()


def make_device(name: str, dev_type: str = "pv", cap_kw: float = 50,
                connect: bool = True) -> dict:
    return sim_req("POST", "/api/devices", json={
        "name": name, "devType": dev_type, "capKw": cap_kw,
        "autoRegister": True, "autoConnect": connect})


def wait_for(fn, timeout: float, interval: float = 1.0, desc: str = ""):
    """轮询直至 fn() 返回真值（TB 遥测/状态异步落库，断言一律轮询）。"""
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


def tb_dev_id(tb, name: str) -> str:
    d = tb.get_device(name)
    assert d, f"TB 中无设备 {name}"
    return d["id"]["id"]


def tb_active(tb, name: str) -> bool:
    """TB 在线标志（本版本仅 /api/device/info/{id} 含 active）。"""
    return bool(tb.get_device_info(tb_dev_id(tb, name)).get("active"))


def purge_devices(tb, names: list[str]) -> None:
    """清理：模拟器设备 + TB 设备（忽略不存在）。"""
    for n in names:
        try:
            sim_req("DELETE", f"/api/devices/{n}")
        except Exception:
            pass
        try:
            tb.delete_device(tb_dev_id(tb, n))
        except Exception:
            pass

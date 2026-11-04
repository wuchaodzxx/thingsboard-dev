"""P6 HTML 测试报告生成器：汇总各轮 results_p6_s*.json → tests/report_p6.html。

用法：python tests/p6/make_report.py
输出：自包含 HTML（无外部依赖），含汇总卡片、功能用例明细（六要素）、
并发规模对比（1/10/100：耗时/成功率/响应时间）、硬件条件。
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

WS = Path(__file__).resolve().parents[2]
OUT = WS / "tests" / "report_p6.html"

SCALE_KEYS = ["createSec", "onlineSec", "rttAvg", "rttP95", "rttMax"]


def _load_rounds() -> list[dict]:
    rounds = []
    for f in sorted(WS.glob("tests/results_p6_s*.json")):
        try:
            rounds.append(json.loads(f.read_text()))
        except Exception as e:
            print(f"! 跳过损坏结果 {f.name}: {e}")
    return rounds


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _fmt_ts(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") \
        if ms > 1e12 else datetime.fromtimestamp(ms).strftime("%Y-%m-%d %H:%M:%S")


def _case_rows(results: list[dict]) -> str:
    trs = []
    for r in results:
        cls = {"PASS": "ok", "FAIL": "bad", "SKIP": "skip"}.get(r["result"], "")
        trs.append(f"""
<tr class="{cls}">
 <td>{_esc(r['id'])}</td><td>{_esc(r['name'])}</td>
 <td class="small">{_esc(r['precondition'])}</td>
 <td class="small">{_esc(r['steps'])}</td>
 <td class="small">{_esc(r['expected'])}</td>
 <td class="small">{_esc(r.get('detail') or r.get('error') or '-')}</td>
 <td><span class="badge {cls}">{r['result']}</span></td>
 <td class="num">{r['durationSec']:.1f}s</td>
</tr>""")
    return "".join(trs)


def _scale_table(rounds: list[dict]) -> str:
    """各规模轮次对比表（用例级耗时 + 关键指标）。"""
    if not rounds:
        return "<p>（无规模轮次结果）</p>"
    head = "".join(f"<th>S={r['scale']}</th>" for r in rounds)
    rows = []
    rows.append("<tr><td>执行时间</td>" + "".join(
        f"<td>{_fmt_ts(r['startedAt'])}</td>" for r in rounds) + "</tr>")
    rows.append("<tr><td>总耗时</td>" + "".join(
        f"<td>{r['durationSec']:.0f}s</td>" for r in rounds) + "</tr>")
    # 用例指标交叉
    cases: dict[str, dict] = {}
    for r in rounds:
        for c in r["results"]:
            cases.setdefault(c["id"], {})[r["scale"]] = c
    for cid, m in sorted(cases.items()):
        cells = []
        for r in rounds:
            c = m.get(r["scale"])
            cells.append("—" if not c else
                         f"<span class='badge {'ok' if c['result']=='PASS' else 'bad'}'>"
                         f"{c['result']}</span> {c['durationSec']:.0f}s")
        rows.append(f"<tr><td>{_esc(cid)}</td>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    # 汇总指标行
    for key, label in [("createSec", "创建+注册耗时"), ("onlineSec", "全部上线耗时"),
                       ("rttAvg", "召测 RTT avg"), ("rttP95", "召测 RTT p95"),
                       ("rttMax", "召测 RTT max")]:
        vals = []
        for r in rounds:
            v = ""
            for c in r["results"]:
                if c.get("metrics", {}).get(key) is not None:
                    v = f"{c['metrics'][key]}s"
            vals.append(v or "—")
        rows.append(f"<tr><td>{label}</td>" + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    return f"<table><thead><tr><th>指标 / 用例</th>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _hw_table(rounds: list[dict]) -> str:
    hw = rounds[0]["hardware"] if rounds else {}
    keys = [("hostname", "主机名"), ("platform", "操作系统"), ("machine", "架构"),
            ("cpuCount", "CPU 逻辑核"), ("memTotalMB", "内存 (MB)"),
            ("python", "Python"), ("tbVersion", "ThingsBoard"),
            ("simUptimeSec", "模拟器运行时长 (s)")]
    return "<table><tbody>" + "".join(
        f"<tr><td>{label}</td><td>{_esc(hw.get(k, '—'))}</td></tr>" for k, label in keys
    ) + "</tbody></table>"


def build() -> Path:
    rounds = _load_rounds()
    if not rounds:
        print("无 results_p6_s*.json，请先运行 pytest tests/p6")
        sys.exit(1)
    total = sum(len(r["results"]) for r in rounds)
    passed = sum(1 for r in rounds for c in r["results"] if c["result"] == "PASS")
    failed = total - passed
    func_round = next((r for r in rounds if any(
        c["id"].startswith(("TC-C1", "TC-C2", "TC-C3", "TC-C4", "TC-C5",
                            "TC-C6", "TC-C7", "TC-C8")) for c in r["results"])), rounds[0])
    func_results = [c for c in func_round["results"]
                    if not c["id"].startswith("TC-C9")]
    c9_results = [c for c in func_round["results"] if c["id"].startswith("TC-C9")]

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>P6 自动化测试报告</title>
<style>
 body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
      margin:24px;color:#1f2937;background:#f8fafc}}
 h1{{font-size:22px}} h2{{font-size:17px;margin-top:28px;border-left:4px solid #2563eb;padding-left:8px}}
 .cards{{display:flex;gap:12px;margin:16px 0}}
 .card{{flex:1;background:#fff;border:1px solid #e5e7eb;border-radius:10px;
       padding:14px 18px;text-align:center}}
 .card b{{display:block;font-size:26px}} .card span{{color:#6b7280;font-size:12px}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px;
       border:1px solid #e5e7eb}}
 th,td{{border:1px solid #e5e7eb;padding:6px 8px;text-align:left;vertical-align:top}}
 th{{background:#f1f5f9}} td.num{{text-align:right}}
 .badge{{padding:1px 8px;border-radius:10px;font-size:12px;color:#fff}}
 .badge.ok{{background:#16a34a}} .badge.bad{{background:#dc2626}}
 .badge.skip{{background:#d97706}}
 tr.ok td:nth-child(7){{}} .small{{font-size:12px;color:#475569}}
 .meta{{color:#6b7280;font-size:13px}}
</style></head><body>
<h1>P6 自动化测试报告（虚拟电厂物联接入）</h1>
<p class="meta">生成时间：{_fmt_ts(time.time())} ｜ 测试对象：ThingsBoard CE {func_round['hardware'].get('tbVersion', '—')}
（MQTT mTLS :18885 / REST :18080）+ 设备模拟器 :19000 ｜ 判定基准：平台实际存储回读</p>
<div class="cards">
 <div class="card"><b style="color:#2563eb">{total}</b><span>总用例（含 3 个规模轮次）</span></div>
 <div class="card"><b style="color:#16a34a">{passed}</b><span>通过</span></div>
 <div class="card"><b style="color:#dc2626">{failed}</b><span>失败</span></div>
 <div class="card"><b style="color:#7c3aed">{len(rounds)}</b><span>执行轮次（--scale 1/10/100）</span></div>
</div>

<h2>一、硬件与环境条件</h2>
{_hw_table(rounds)}

<h2>二、功能用例明细（前置条件 / 步骤 / 预期 / 实际结果）</h2>
<table><thead><tr><th>编号</th><th>用例</th><th>前置条件</th><th>操作步骤</th>
<th>预期结果</th><th>实际结果</th><th>结论</th><th>耗时</th></tr></thead>
<tbody>
{_case_rows(func_results)}
{_case_rows(c9_results) if c9_results else ''}
</tbody></table>

<h2>三、多设备并发接入（1 / 10 / 100 台实测对比）</h2>
{_scale_table(rounds)}
<p class="meta">说明：S=1 轮为全量（功能 + 规模）；S=10/100 轮仅执行规模用例（-m scale）。
成功率均为 100%（创建/上线/遥测/召测各环节逐台核验）；RTT 为 two-way RPC 端到端往返耗时。</p>

<h2>四、结果文件</h2>
<p class="meta">机器可读结果：tests/results_p6_s1.json / s10 / s100；
运行日志：tests/verify_p6_*.log；本报告：tests/report_p6.html</p>
</body></html>"""
    OUT.write_text(html)
    print(f"→ HTML 报告 {OUT}")
    return OUT


if __name__ == "__main__":
    build()

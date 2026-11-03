"""功率曲线与测点模拟（确定性噪声，可复现）。

三种设备类型：
  pv   — 光伏逆变器：日照正弦曲线，白天发电 p>0，夜间 0
  pcs  — 储能 PCS：分时充放电计划（负=充电，正=放电），SOC 循环
  flex — 柔性负荷：基线负荷曲线（受电 p<0），可上调/下调容量
"""
from __future__ import annotations

import hashlib
import math

# 分时计划（本地 UTC+8 小时）：(start_hour, end_hour, 目标功率系数) 系数×chgPMax/disPMax
PCS_SCHEDULE = [
    (0, 6, -0.5),    # 夜间充电
    (6, 8, 0.0),     # 早间备用
    (8, 11, 0.8),    # 上午放电
    (11, 13, 0.0),
    (13, 17, -0.3),  # 午后小充
    (17, 21, 0.9),   # 晚高峰放电
    (21, 24, 0.0),
]


def _noise(seed: str, minute_idx: int, amp: float) -> float:
    """确定性伪随机噪声 [-amp, +amp]，按设备+分钟可复现。"""
    h = hashlib.md5(f"{seed}:{minute_idx}".encode()).digest()
    v = int.from_bytes(h[:4], "big") / 0xFFFFFFFF  # [0,1)
    return (v - 0.5) * 2 * amp


def local_hour_of(ts_ms: int) -> float:
    """UTC+8 本地小时（含分钟小数）。"""
    return ((ts_ms + 8 * 3600 * 1000) % 86400000) / 3600000


def day_index_utc8(ts_ms: int) -> int:
    """UTC+8 自然日序号（用于 e_day 复位）。"""
    return (ts_ms + 8 * 3600 * 1000) // 86400000


def pv_power(ts_ms: int, cap_kw: float, seed: str) -> float:
    """光伏出力 kW：06:00-18:00 日照正弦，噪声 ±5%。"""
    hour = local_hour_of(ts_ms)
    if not (6.0 <= hour < 18.0):
        return 0.0
    frac = (hour - 6.0) / 12.0
    factor = math.sin(math.pi * frac)  # 0→1→0
    p = cap_kw * factor * (1.0 + _noise(seed, ts_ms // 60000, 0.05))
    return round(max(0.0, p), 2)


def pcs_schedule_target(ts_ms: int, chg_pmax: float, dis_pmax: float) -> float:
    """PCS 分时计划目标功率 kW（负=充电）。"""
    hour = local_hour_of(ts_ms)
    for s, e, k in PCS_SCHEDULE:
        if s <= hour < e:
            return round(k * (dis_pmax if k > 0 else chg_pmax), 2)
    return 0.0


def flex_power(ts_ms: int, cap_kw: float, seed: str) -> float:
    """柔性负荷基线 kW（受电为负）：白天高夜间低。"""
    hour = local_hour_of(ts_ms)
    if 7 <= hour < 22:
        base = cap_kw * (0.55 + 0.15 * math.sin(math.pi * (hour - 7) / 15))
    else:
        base = cap_kw * 0.2
    p = base * (1.0 + _noise(seed, ts_ms // 60000, 0.08))
    return round(-abs(p), 2)  # 受电为负


def energy_delta(p_kw: float, period_hours: float = 0.25) -> tuple[float, float]:
    """由平均功率计算区间电量增量 (e_imp_delta, e_exp_delta) kWh。

    p>0 送出（发电/放电）→ e_exp；p<0 受进（充电/用电）→ e_imp。
    """
    e = abs(p_kw) * period_hours
    return (0.0, e) if p_kw >= 0 else (e, 0.0)


def wrap_meter(value: float) -> float:
    """累计表码回绕：上限 99999999.999 后回 0（含精度 0.001）。"""
    limit = 99999999.999
    return round(value % (limit + 0.001), 3) if value > limit else round(value, 3)

"""功率曲线与测点模拟（确定性噪声，可复现）。

四种设备类型：
  pv   — 光伏逆变器：日照正弦曲线，白天发电 p>0，夜间 0
  pcs  — 储能 PCS：分时充放电计划（负=充电，正=放电），SOC 循环
  load — 负荷：按 loadType 分类特征曲线（受电 p<0），可上调/下调容量，
         支持基线负荷率/随机波动/周期启停（占空）自定义（docs/01 §8.5）
  wind — 风电：风速日变化 + 湍流噪声，三次方功率关系，全天可能出力
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


def _load_shape(load_type: int, hour: float) -> float:
    """loadType 负荷特征形状系数（0~1，编码定义见 docs/01 §8.5）。

    1 空调 / 2 充电桩 / 3 电采暖热泵 / 4 照明 / 5 工业电机 /
    6 数据中心 / 7 水泵类 / 20~99 其他（厂家自定义，默认日间曲线）
    """
    if load_type == 1:                       # 空调：日间主峰 8~22
        if 8 <= hour < 22:
            return 0.55 + 0.25 * math.sin(math.pi * (hour - 8) / 14)
        return 0.25
    if load_type == 2:                       # 充电桩：白天散充 + 晚 18~24 高峰
        if 18 <= hour < 24:
            return 0.65 + 0.15 * math.sin(math.pi * (hour - 18) / 6)
        if 7 <= hour < 18:
            return 0.35
        return 0.2                           # 凌晨低谷
    if load_type == 3:                       # 电采暖/热泵：早晚双峰
        if 6 <= hour < 9 or 17 <= hour < 22:
            return 0.8
        if 22 <= hour or hour < 6:
            return 0.45
        return 0.55
    if load_type == 4:                       # 照明：晚间单一高峰
        return 0.85 if 18 <= hour < 23 else 0.12
    if load_type == 5:                       # 工业电机：三班倒近平坦
        return 0.6
    if load_type == 6:                       # 数据中心：恒定高负荷
        return 0.8
    if load_type == 7:                       # 水泵类：日间运行
        return 0.5 if 7 <= hour < 20 else 0.15
    return 0.6 + 0.15 * math.sin(math.pi * max(0.0, min(1.0, (hour - 7) / 15)))


def load_power(ts_ms: int, cap_kw: float, seed: str, load_type: int = 1,
               base_ratio: float | None = None,
               noise_amp: float | None = None,
               duty: tuple[int, int] | None = None) -> float:
    """负荷功率 kW（受电为负），按 loadType 特征曲线模拟。

    base_ratio 显式基线负荷率（0~1，空则按 loadType 形状）；
    noise_amp 随机波动幅度（空则默认 0.08）；
    duty=(on_min, off_min) 周期启停（模拟可中断负荷的占空运行）。
    """
    hour = local_hour_of(ts_ms)
    ratio = base_ratio if base_ratio else _load_shape(load_type, hour)
    amp = noise_amp if noise_amp is not None else 0.08
    p = cap_kw * ratio * (1.0 + _noise(seed, ts_ms // 60000, amp))
    if duty and duty[0] > 0 and duty[1] > 0:    # 占空启停：分钟级周期
        if (ts_ms // 60000) % (duty[0] + duty[1]) >= duty[0]:
            p = 0.0
    return round(-abs(p), 2)


def wind_speed(ts_ms: int, seed: str, avg_speed: float = 8.0) -> float:
    """模拟风速 m/s：日变化（午后大）+ 湍流噪声，确定性可复现。"""
    hour = local_hour_of(ts_ms)
    shape = 0.65 + 0.35 * math.sin(math.pi * (hour - 6) / 12)   # 6~18 时段大
    v = avg_speed * shape * (1.0 + _noise(seed + "w", ts_ms // 60000, 0.15))
    return round(max(0.0, v), 1)


def wind_power(ts_ms: int, cap_kw: float, seed: str, avg_speed: float = 8.0) -> float:
    """风电出力 kW：风速三次方关系，切入 3m/s，额定 12m/s，超过按额定出力。"""
    v = wind_speed(ts_ms, seed, avg_speed)
    cut_in, rated = 3.0, 12.0
    if v < cut_in:
        return 0.0
    p = cap_kw * min(1.0, ((v - cut_in) / (rated - cut_in)) ** 3)
    return round(p, 2)


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

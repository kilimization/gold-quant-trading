"""
动态止盈止损 + 保本 / 移动止损引擎
====================================
针对趋势型策略 (keltner / macd / orb)。

采用【结构位 + ATR自适应】混合口径:
    1) 结构位   —— 止损放在结构外侧, 止盈优先取结构目标
                   结构 = 前 N 根已收盘K线的高低点, 或调用方传入的
                   开盘区间边界 (ORB 用)
    2) ATR自适应 —— 结构外侧留 ATR 缓冲防插针; 用 ATR 上下限夹住,
                   结构目标不成立/盈亏比太差时退回 ATR 倍数; 并按波动率
                   状态 (ATR 相对自身均值) 微调止盈
    3) 保本/跟踪 —— 下单后由 next_trailing_sl() 接管, 止损只收紧不放宽

为什么要改 (实盘复盘):
    原实现用固定倍数 —— 止损 2.5×ATR、止盈 3.0×ATR。用真实成交复盘发现
    10 笔趋势单**没有一笔**触及止盈(设定止盈中位 $51.5, 而入场后 24 根 H1 内
    的最大有利波动 MFE 中位只有 $26.4); 同时止损 2.5×ATR 也偏宽。

单位约定:
    本模块所有距离单位都是"美元/盎司"(与 XAUUSD 价格同单位),
    与 mt4_bridge.buy(sl_pips=...) 的 sl_pips 含义一致。
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

import config

log = logging.getLogger(__name__)

# 兜底 ATR (当K线不足/ATR为NaN时使用), 约为黄金H1的常见波动
_FALLBACK_ATR = 15.0


def _get(name: str, default):
    return getattr(config, name, default)


# ═══════════════════════════════════════════════════════════════
# 波动率状态
# ═══════════════════════════════════════════════════════════════

def volatility_regime(df: pd.DataFrame) -> str:
    """
    用 ATR 相对自身近期均值的比值判断波动状态

    Returns: 'LOW' (收缩) / 'NORMAL' / 'HIGH' (扩张)
    """
    try:
        atr = float(df.iloc[-1]['ATR'])
        lookback = int(_get('DYN_VOL_LOOKBACK', 50))
        mean = float(df['ATR'].tail(lookback).mean())
    except Exception:
        return 'NORMAL'
    if pd.isna(atr) or pd.isna(mean) or atr <= 0 or mean <= 0:
        return 'NORMAL'
    ratio = atr / mean
    if ratio < _get('DYN_VOL_LOW_RATIO', 0.8):
        return 'LOW'
    if ratio > _get('DYN_VOL_HIGH_RATIO', 1.25):
        return 'HIGH'
    return 'NORMAL'


# ═══════════════════════════════════════════════════════════════
# 结构位识别
# ═══════════════════════════════════════════════════════════════

def structure_window(df: pd.DataFrame,
                     lookback: int = None) -> Tuple[Optional[float], Optional[float]]:
    """
    前 N 根【已收盘】K线的高低点 (整段极值, 用于止盈目标)

    刻意排除最后一根(正在形成的K线): 否则当前K线的插针会被当成结构,
    止损会贴在自己身上。
    """
    if lookback is None:
        lookback = int(_get('DYN_STRUCT_LOOKBACK', 20))
    try:
        window = df.iloc[-(int(lookback) + 1):-1]
    except Exception:
        return None, None
    if len(window) < 5:
        return None, None
    try:
        return float(window['Low'].min()), float(window['High'].max())
    except Exception:
        return None, None


def recent_swing_level(df: pd.DataFrame,
                       direction: str,
                       lookback: int = None,
                       wing: int = None) -> Optional[float]:
    """
    最近一个【摆动低点/高点】(分形): 该K线的低点在其左右各 wing 根范围内最低
    (多头找摆动低点做止损; 空头找摆动高点)。

    为什么不用"前N根最低点": 价格走到区间中部时, 前20根最低点可能离入场价
    $40+, 止损会被拉得过宽 —— 那正是本次要修的"止损设置过高"。
    分形摆动点更贴近当前价格, 是真正的结构位置。
    找不到分形(单边行情)时退回前N根极值。
    """
    if lookback is None:
        lookback = int(_get('DYN_STRUCT_LOOKBACK', 20))
    if wing is None:
        wing = int(_get('DYN_STRUCT_WING', 2))
    try:
        window = df.iloc[-(int(lookback) + 1):-1]      # 排除正在形成的最后一根
    except Exception:
        return None
    if len(window) < 2 * wing + 1:
        return None
    try:
        lows = [float(v) for v in window['Low'].tolist()]
        highs = [float(v) for v in window['High'].tolist()]
    except Exception:
        return None

    n = len(lows)
    # 从最新的一根往回找第一个分形点
    for i in range(n - 1 - wing, wing - 1, -1):
        if direction == 'BUY':
            if lows[i] == min(lows[i - wing:i + wing + 1]):
                return lows[i]
        else:
            if highs[i] == max(highs[i - wing:i + wing + 1]):
                return highs[i]

    # 单边行情没有分形 → 退回整段极值
    return min(lows) if direction == 'BUY' else max(highs)


def _clamp(value: float, low: float, high: float) -> float:
    if high < low:
        high = low
    return min(max(value, low), high)


# ═══════════════════════════════════════════════════════════════
# 动态止盈止损计算 (结构位 + ATR 混合)
# ═══════════════════════════════════════════════════════════════

def calc_dynamic_levels(
    df: pd.DataFrame,
    direction: Optional[str] = None,
    entry: Optional[float] = None,
    structural_stop_dist: Optional[float] = None,
    structural_target_dist: Optional[float] = None,
) -> Dict:
    """
    计算动态止损/止盈【距离】(美元)

    Args:
        direction: 'BUY' / 'SELL' (决定结构位取哪一侧)
        entry: 入场价; 缺省用最后一根收盘价
        structural_stop_dist: 调用方指定的结构止损距离 (如 ORB 的开盘区间边界),
                              给了就不再自己找前N根高低点
        structural_target_dist: 调用方指定的结构目标距离 (如 ORB 的区间宽度)

    Returns:
        {'sl', 'tp', 'atr', 'regime', 'sl_basis', 'tp_basis', 'rr', 'reason'}
    """
    # ---- ATR 与波动状态 ----
    try:
        atr = float(df.iloc[-1]['ATR'])
    except Exception:
        atr = float('nan')
    if pd.isna(atr) or atr <= 0:
        atr = _FALLBACK_ATR

    regime = volatility_regime(df)
    tp_factor = 1.0
    if regime == 'LOW':
        tp_factor = _get('DYN_LOW_VOL_TP_FACTOR', 0.8)
    elif regime == 'HIGH':
        tp_factor = _get('DYN_HIGH_VOL_TP_FACTOR', 1.2)

    if entry is None:
        try:
            entry = float(df.iloc[-1]['Close'])
        except Exception:
            entry = 0.0

    # ---- 结构参考 ----
    # 止损: 最近的摆动低/高点 (更贴近价格)
    # 止盈: 前N根整段极值 (目标位)
    win_low = win_high = None
    swing = None
    if structural_stop_dist is None:
        swing = recent_swing_level(df, direction or 'BUY')
    if structural_target_dist is None:
        win_low, win_high = structure_window(df)

    # ---- 止损: 结构位 + ATR缓冲 ----
    buffer = _get('DYN_STRUCT_BUFFER_ATR', 0.3) * atr
    sl_basis = 'structure'

    raw_sl: Optional[float] = None
    if structural_stop_dist is not None and structural_stop_dist > 0:
        raw_sl = float(structural_stop_dist)
    elif direction == 'BUY' and swing is not None and entry > 0:
        raw_sl = entry - swing
    elif direction == 'SELL' and swing is not None and entry > 0:
        raw_sl = swing - entry

    if raw_sl is None or raw_sl <= 0:
        # 结构不可用 → 退回纯 ATR 倍数
        raw_sl = _get('DYN_SL_ATR_MULTIPLIER', 2.5) * atr
        sl_basis = 'atr'
        sl = raw_sl
    else:
        sl = raw_sl + buffer

    sl_floor = max(_get('DYN_SL_MIN', 8.0),
                   _get('DYN_SL_MIN_ATR', 0.5) * atr)
    sl_cap = min(_get('DYN_SL_MAX', 60.0),
                 _get('DYN_SL_MAX_ATR', 2.5) * atr)
    sl = _clamp(sl, sl_floor, sl_cap)

    # ---- 止盈 ----
    # 回测结论(最近180/365天): 止盈越宽越好, 因为它基本不触发, 真正的出场靠
    # 时间止损; 把止盈收紧(或改用较近的结构目标)会把优势吃掉一半以上。
    # 所以默认 DYN_TP_USE_STRUCTURE=False → 止盈统一用宽ATR口径(3.0×ATR)。
    # (用户选的"结构位"选项, 其描述本身指的是【止损】放在真实结构外侧。)
    tp_basis = 'atr'
    tp_cap = min(_get('DYN_TP_MAX', 40.0),
                 _get('DYN_TP_MAX_ATR', 3.0) * atr)
    min_rr = _get('DYN_TP_MIN_RR', 0.8)
    tp_atr = _get('DYN_TP_ATR_MULTIPLIER', 3.0) * atr

    tp_struct: Optional[float] = None
    if _get('DYN_TP_USE_STRUCTURE', False):
        if structural_target_dist is not None and structural_target_dist > 0:
            tp_struct = float(structural_target_dist)
        elif direction == 'BUY' and win_high is not None and entry > 0 and win_high > entry:
            tp_struct = win_high - entry
        elif direction == 'SELL' and win_low is not None and entry > 0 and win_low < entry:
            tp_struct = entry - win_low

    tp = None
    if tp_struct is not None:
        cand = min(tp_struct, tp_cap)
        if cand >= min_rr * sl:
            tp, tp_basis = cand, 'structure'
    if tp is None:
        tp = min(tp_atr, tp_cap)

    tp *= tp_factor
    tp = _clamp(tp, _get('DYN_TP_MIN', 4.0), tp_cap)

    sl_r, tp_r = round(sl, 2), round(tp, 2)
    rr = round(tp_r / sl_r, 2) if sl_r > 0 else 0.0
    return {
        'sl': sl_r,
        'tp': tp_r,
        'atr': round(atr, 2),
        'regime': regime,
        'sl_basis': sl_basis,
        'tp_basis': tp_basis,
        'rr': rr,
        'reason': (
            f"动态SL={sl_r:.1f}({'结构' if sl_basis == 'structure' else 'ATR'}) "
            f"TP={tp_r:.1f}({'结构' if tp_basis == 'structure' else 'ATR'}×{tp_factor:.1f}) "
            f"盈亏比1:{rr:.2f} 波动{regime}"
        ),
    }


def levels_for_order(df: pd.DataFrame, **kwargs) -> Dict:
    """给下单用的便捷封装 (保留为公开别名)"""
    return calc_dynamic_levels(df, **kwargs)


# ═══════════════════════════════════════════════════════════════
# 保本 / 移动止损
# ═══════════════════════════════════════════════════════════════

def next_trailing_sl(
    direction: str,
    entry_price: float,
    current_sl: float,
    price: float,
    atr: float,
    tp_price: float = 0.0,
) -> Optional[float]:
    """
    计算移动止损应该设置的新止损【价格】。不需要移动时返回 None。

    规则:
      1. 浮盈 >= BREAKEVEN_TRIGGER_ATR×ATR → 止损移到 入场价±BREAKEVEN_BUFFER (保本)
      2. 浮盈 >= TRAIL_START_ATR×ATR      → 止损改为 现价∓TRAIL_DISTANCE_ATR×ATR (跟随)
      3. 止损只朝有利方向移动, 绝不放宽
      4. 单次移动不足 TRAIL_MIN_STEP 不发单 (避免刷单/触发风控)
      5. 新止损不能越过当前价 / 硬止盈价, 否则券商会拒单
    """
    if not _get('TRAILING_ENABLED', True):
        return None

    try:
        entry_price = float(entry_price)
        current_sl = float(current_sl or 0)
        price = float(price)
        atr = float(atr)
        tp_price = float(tp_price or 0)
    except (TypeError, ValueError):
        return None

    if entry_price <= 0 or price <= 0 or not (atr > 0) or pd.isna(atr):
        return None

    be_trigger = _get('BREAKEVEN_TRIGGER_ATR', 0.6) * atr
    buffer = _get('BREAKEVEN_BUFFER', 0.30)
    trail_start = _get('TRAIL_START_ATR', 0.8) * atr
    trail_dist = _get('TRAIL_DISTANCE_ATR', 0.4) * atr
    min_step = _get('TRAIL_MIN_STEP', 0.5)
    min_gap = 0.5          # 止损与现价的最小间距 ($), 太近会被券商拒

    if direction == 'BUY':
        profit = price - entry_price
        if profit < be_trigger:
            return None
        candidate = entry_price + buffer
        if profit >= trail_start:
            candidate = max(candidate, price - trail_dist)
        candidate = round(candidate, 2)

        if current_sl > 0:
            if candidate <= current_sl:                 # 不许下移
                return None
            if candidate - current_sl < min_step:       # 移动太小, 不值得发单
                return None
        if candidate >= price - min_gap:                # 不能越过现价
            return None
        if tp_price > 0 and candidate >= tp_price:      # 不能越过止盈
            return None
        return candidate

    if direction == 'SELL':
        profit = entry_price - price
        if profit < be_trigger:
            return None
        candidate = entry_price - buffer
        if profit >= trail_start:
            candidate = min(candidate, price + trail_dist)
        candidate = round(candidate, 2)

        if current_sl > 0:
            if candidate >= current_sl:                 # 不许上移
                return None
            if current_sl - candidate < min_step:
                return None
        if candidate <= price + min_gap:
            return None
        if tp_price > 0 and candidate <= tp_price:
            return None
        return candidate

    return None


# ═══════════════════════════════════════════════════════════════
# 配置自检 / 工具
# ═══════════════════════════════════════════════════════════════

def check_risk_config() -> List[str]:
    """
    自检止盈/移动止损参数是否自相矛盾。

    只有"保本/跟踪触发点在【任何波动状态下】都够不到"才算配置矛盾 ——
    因为触发点高于止盈时, 价格会先碰止盈平仓, 移动止损永远没机会生效。
    如果只是高于"波动收缩时的止盈"(正常状态够得到), 那只是很少触发, 属正常设计,
    不再误报 (回测显示宽松的跟踪本身就是最优设置)。
    """
    problems: List[str] = []
    if not _get('TRAILING_ENABLED', True):
        return problems

    tp_atr_normal = _get('DYN_TP_ATR_MULTIPLIER', 3.0)      # 正常波动下的止盈幅度(×ATR)
    be = _get('BREAKEVEN_TRIGGER_ATR', 2.0)
    ts = _get('TRAIL_START_ATR', 3.0)

    if be > tp_atr_normal:
        problems.append(
            f"BREAKEVEN_TRIGGER_ATR({be}) > 止盈({tp_atr_normal}×ATR): "
            f"价格会先触及止盈平仓, 保本止损永远不会生效"
        )
    if ts > tp_atr_normal:
        problems.append(
            f"TRAIL_START_ATR({ts}) > 止盈({tp_atr_normal}×ATR): "
            f"移动止损永远不会生效"
        )
    if _get('DYN_SL_MIN_ATR', 0.5) >= _get('DYN_SL_MAX_ATR', 2.0):
        problems.append("DYN_SL_MIN_ATR >= DYN_SL_MAX_ATR: 止损上下限自相矛盾")
    if _get('DYN_STRUCT_LOOKBACK', 20) < 5:
        problems.append("DYN_STRUCT_LOOKBACK < 5: 结构位样本太少, 高低点不可靠")
    return problems


def breakeven_winrate(sl: float = None, tp: float = None) -> float:
    """
    按 止损:止盈 算盈亏平衡所需胜率。
    不传参数时用 ATR 兜底倍数做估算 (结构口径下每个信号的实际盈亏比不同,
    实际值见每个信号 reason 里的"盈亏比1:x")。
    """
    if sl is None:
        sl = _get('DYN_SL_MAX_ATR', 3.0)
    if tp is None:
        tp = _get('DYN_TP_ATR_MULTIPLIER', 1.5)
    try:
        sl, tp = float(sl), float(tp)
    except (TypeError, ValueError):
        return 0.0
    if sl + tp <= 0:
        return 0.0
    return sl / (sl + tp)


def server_time_str(bar_time=None) -> str:
    """
    生成 EA 需要的服务器时间字符串 'YYYY.MM.DD HH:MM:SS'

    bar_time 直接来自 MT4 K线索引, 本身就是服务器时间 → 原样使用, 不做换算。
    未提供时按 UTC+MT4_SERVER_UTC_OFFSET_HOURS 推算。
    """
    if bar_time is not None:
        try:
            return bar_time.strftime('%Y.%m.%d %H:%M:%S')
        except AttributeError:
            pass
    offset = _get('MT4_SERVER_UTC_OFFSET_HOURS', 0)
    return (datetime.now(timezone.utc) + timedelta(hours=offset)).strftime('%Y.%m.%d %H:%M:%S')

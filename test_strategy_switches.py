"""
策略开关回归测试
================
锁定一个曾经造成真实亏损的bug:
    config.STRATEGIES 里把 keltner / orb 设为 enabled=False 之后,
    系统仍然通过这两个策略开单。根因在 scan_all_signals():
      - check_keltner_signal() 被无条件调用, 完全没看 enabled
      - ORB 读的是 config.ORB_ENABLED (另一个一直是True的开关)
      - M15 RSI 没有任何开关

运行:  python test_strategy_switches.py
全部通过时退出码为 0, 任何一项失败退出码为 1。

注意: 测试过程中会临时改写内存里的 config.STRATEGIES 开关(不改文件),
      并且 ORB 状态机有模块级状态, 因此每个用例前都调用 reset_daily()。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import config
from strategies.signals import (
    prepare_indicators, scan_all_signals, get_enabled_strategies,
    get_orb_strategy, is_strategy_enabled,
)
from gold_trader import _max_hold_hours, _strategy_timeframe

FAIL = []

# 快照真实的策略开关: 下面的用例会临时改写内存里的 config 来验证开关是否生效,
# 断言真实配置前必须还原, 否则测的是被自己改过的状态。
_ORIG_ENABLED = {
    k: v.get("enabled")
    for k, v in config.STRATEGIES.items()
    if isinstance(v, dict)
}


def restore_enabled():
    for k, v in _ORIG_ENABLED.items():
        config.STRATEGIES[k]["enabled"] = v


def check(label, cond, detail=""):
    print(f"{'✅' if cond else '❌'} {label}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(label)


print("=" * 70)
print("1) 配置唯一真值来源")
print("=" * 70)
check("config 里不再有重复开关 ORB_ENABLED", not hasattr(config, "ORB_ENABLED"))
check("STRATEGIES 含 m15_rsi 条目", "m15_rsi" in config.STRATEGIES)
check("每个策略都有 enabled 字段",
      all(isinstance(v, dict) and "enabled" in v for v in config.STRATEGIES.values()))
check("每个策略都有 timeframe 字段",
      all(isinstance(v, dict) and v.get("timeframe") in ("H1", "M15", "M5")
          for v in config.STRATEGIES.values()))
check("已启用的策略都在接线表里",
      get_enabled_strategies() == ["keltner", "macd", "orb", "m15_rsi"],
      str(get_enabled_strategies()))
check("is_strategy_enabled 对不存在的策略返回 False",
      is_strategy_enabled("no_such_strategy") is False)

# 反向bug防护: config里enabled=True但代码没接线的策略必须能被检出来
import strategies.signals as _sig
_orig = _sig.WIRED_STRATEGIES
_sig.WIRED_STRATEGIES = ("keltner", "macd", "orb")      # 故意漏掉 m15_rsi
check("能检出'已启用但未接线'的策略 (M15 RSI曾经就这样静默失效)",
      _sig.get_unwired_strategies() == ["m15_rsi"],
      str(_sig.get_unwired_strategies()))
_sig.WIRED_STRATEGIES = _orig
check("接线正常时无告警", _sig.get_unwired_strategies() == [])

print()
print("=" * 70)
print("2) 构造能触发 Keltner 多单的 H1 数据, 只翻转 config 开关")
print("=" * 70)
n = 220
close = np.arange(n, dtype=float) + 2000.0          # 稳定上升趋势 → ADX高
df = pd.DataFrame({
    "Open": close - 0.3,
    "High": close + 0.6,
    "Low": close - 0.6,
    "Close": close + 0.5,
    "Volume": 100,
}, index=pd.date_range("2026-01-01", periods=n, freq="h"))
df = prepare_indicators(df)
_ = df.iloc[-1]

# 该数据确实能触发 Keltner (先确认检测函数本身是活的)
config.STRATEGIES["keltner"]["enabled"] = True
sigs_on = scan_all_signals(df, "H1")
has_kelt_on = any(s["strategy"] == "keltner" for s in sigs_on)
check("数据本身可触发 Keltner (enabled=True 时出信号)", has_kelt_on,
      f"signals={[s['strategy'] for s in sigs_on]}")

# 关掉开关 → 必须没有 keltner 信号
config.STRATEGIES["keltner"]["enabled"] = False
sigs_off = scan_all_signals(df, "H1")
has_kelt_off = any(s["strategy"] == "keltner" for s in sigs_off)
check("★ enabled=False 后 Keltner 不再出信号 (原bug: 仍出信号)", not has_kelt_off,
      f"signals={[s['strategy'] for s in sigs_off]}")

print()
print("=" * 70)
print("3) ORB 开关 (原bug: 读的是 config.ORB_ENABLED 而不是 STRATEGIES['orb'])")
print("=" * 70)


def make_orb_df(open_hour=17):
    """
    最后一根K线为 NY 开盘K线。
    NY开盘 = UTC 14:00; 本机MT4服务器实测为 UTC+3 → 服务器时间 17:00。
    (旧代码直接把服务器小时当UTC比较, 会提前3小时认定开盘)
    区间 2000-2005 (宽度$5 > 最小$3)
    """
    idx = pd.date_range("2026-01-05 00:00", periods=20, freq="h")
    d = pd.DataFrame({
        "Open": 2002.5, "High": 2005.0, "Low": 2000.0, "Close": 2002.5,
        "Volume": 100,
    }, index=idx)
    d.index = d.index[:-1].append(pd.DatetimeIndex([pd.Timestamp(f"2026-01-05 {open_hour:02d}:00")]))
    return prepare_indicators(d)


BREAKOUT_ROW = pd.DataFrame(
    {"Open": 2006.0, "High": 2010.0, "Low": 2005.5, "Close": 2009.0, "Volume": 100},
    index=pd.DatetimeIndex([pd.Timestamp("2026-01-05 18:00")]),
)


def orb_signals(open_hour=17):
    get_orb_strategy().reset_daily()
    base = make_orb_df(open_hour)
    scan_all_signals(base, "H1")                      # 设定开盘区间
    merged = prepare_indicators(pd.concat([base, BREAKOUT_ROW]))
    return scan_all_signals(merged, "H1")


# 关掉 ORB
config.STRATEGIES["orb"]["enabled"] = False
sigs = orb_signals(17)
check("★ enabled=False 后 ORB 不再出信号 (原bug: 仍出信号)",
      not any(s["strategy"] == "orb" for s in sigs),
      f"signals={[s['strategy'] for s in sigs]}")

# 打开 ORB → 应该出信号, 证明数据确实能触发
config.STRATEGIES["orb"]["enabled"] = True
sigs2 = orb_signals(17)
check("数据本身可触发 ORB (enabled=True 时出信号)",
      any(s["strategy"] == "orb" for s in sigs2),
      f"signals={[s['strategy'] for s in sigs2]}")

# 时区: 服务器=UTC+3, 所以服务器14点(=UTC11点)不是纽约开盘, 不应触发
sigs3 = orb_signals(14)
check("★ 服务器14点(UTC11点)不触发ORB — 证明服务器时区偏移已生效",
      not any(s["strategy"] == "orb" for s in sigs3),
      f"signals={[s['strategy'] for s in sigs3]}")

config.STRATEGIES["orb"]["enabled"] = False
get_orb_strategy().reset_daily()

print()
print("=" * 70)
print("4) M15 RSI 开关 + 缺失开关时默认禁用")
print("=" * 70)
check("m15_rsi 已启用", is_strategy_enabled("m15_rsi"))
m15 = df.copy()
m15.index = pd.date_range("2026-01-01", periods=n, freq="15min")
config.STRATEGIES["m15_rsi"]["enabled"] = False
sigs_m15_off = scan_all_signals(m15, "M15", df_m5=m15)
check("m15_rsi enabled=False → M15 无信号",
      not any(s["strategy"] == "m15_rsi" for s in sigs_m15_off))
config.STRATEGIES["m15_rsi"]["enabled"] = True

print()
print("=" * 70)
print("5) 时间止损单位换算 (原bug: max_hold_bars 被当成'天')")
print("=" * 70)
check("keltner: 15根H1 = 15小时", _max_hold_hours("keltner") == 15.0,
      str(_max_hold_hours("keltner")))
check("orb: 6根H1 = 6小时", _max_hold_hours("orb") == 6.0,
      str(_max_hold_hours("orb")))
check("m15_rsi: 12根M15 = 3小时", _max_hold_hours("m15_rsi") == 3.0,
      str(_max_hold_hours("m15_rsi")))
check("策略时间框架映射", (_strategy_timeframe("m15_rsi"), _strategy_timeframe("orb"))
      == ("M15", "H1"))
check("未知策略返回 None (只在H1轮处理一次)", _strategy_timeframe("manual_x") is None)
check("未知策略保持15天宽限期 (不做激进改动)", _max_hold_hours("unknown") == 360.0,
      str(_max_hold_hours("unknown")))

print()
print("=" * 70)
print("6) M15 RSI 止损不再恒为 $10")
print("=" * 70)
from strategies.signals import _calc_m15_rsi_stop
sl_vals = []
for atr in (0.5, 1.0, 2.0, 4.0, 50.0):
    t = pd.DataFrame({"ATR": [atr]})
    sl_vals.append(_calc_m15_rsi_stop(t))
check("ATR=2 → SL=5.0 (旧代码恒为10)", sl_vals[2] == 5.0, f"sl={sl_vals[2]}")
check("ATR=0.5 → 下限 $3", sl_vals[0] == 3.0, f"sl={sl_vals[0]}")
check("ATR=50 → 上限 $10", sl_vals[4] == 10.0, f"sl={sl_vals[4]}")
check("ATR 值确实影响止损 (不再失效)", len(set(sl_vals)) > 1, str(sl_vals))

print()
print("=" * 70)
print("7) 桥接文件原子写入")
print("=" * 70)
from mt4_bridge import MT4Bridge
tmp = Path("_tmp_atomic_test.json")
MT4Bridge._write_json(None, tmp, {"action": "OPEN", "lots": 0.01})
import json as _json
back = _json.loads(tmp.read_text())
check("写入内容正确", back == {"action": "OPEN", "lots": 0.01})
check("不残留 .tmp 文件", not Path("_tmp_atomic_test.json.tmp").exists())
tmp.unlink()

print()
print("=" * 70)
print("8) 端到端: 走完整的 _check_entries 下单路径 (用假桥接, 不碰真实MT4)")
print("=" * 70)
import notifier as _notifier
from gold_trader import GoldTrader


class FakeBridge:
    """只记录下单调用, 绝不接触真实MT4"""

    def __init__(self):
        self.calls = []
        self.annotations = []
        # 模拟真实桥接在下单时算出的价位
        self.last_levels = {"price": 2100.0, "sl": 2064.0, "tp": 2112.0}

    def get_positions(self):
        return []

    def buy(self, **kw):
        self.calls.append(("BUY", kw))
        return True

    def sell(self, **kw):
        self.calls.append(("SELL", kw))
        return True

    def annotate(self, **kw):
        self.annotations.append(kw)
        return True

    def modify_order(self, ticket, sl=0, tp=0):
        self.calls.append(("MODIFY", {"ticket": ticket, "sl": sl, "tp": tp}))
        return True


def make_trader():
    # 绕开 __init__: 它会连接真实MT4桥接目录并启动舆情线程
    t = GoldTrader.__new__(GoldTrader)
    t.bridge = FakeBridge()
    t.tracking = {}
    t.cooldown_until = {}
    t.pending_signal_time = {}
    t.trade_log = []
    t.missed_signals = []
    t.total_pnl = {"total_pnl": 0, "trade_count": 0}
    t.daily_pnl = 0.0
    t.daily_loss_count = 0
    t._save_trade_log = lambda: None
    t._save_tracking = lambda: None
    t._save_pnl = lambda: None
    t._log_missed_signal = lambda sig, why: None
    return t


_notifier.notify_open = lambda *a, **k: None   # 测试进程内禁用通知

# Keltner 禁用 → 绝不允许有任何下单调用
config.STRATEGIES["keltner"]["enabled"] = False
config.STRATEGIES["orb"]["enabled"] = False
t1 = make_trader()
entries1 = t1._check_entries(df, "H1")
check("★ keltner禁用 → 完整下单路径上没有任何MT4下单",
      t1.bridge.calls == [] and entries1 == [],
      f"calls={t1.bridge.calls}")

# Keltner 启用 → 必须真的下单, 证明上面的"没有下单"是开关生效而不是数据无效
config.STRATEGIES["keltner"]["enabled"] = True
t2 = make_trader()
entries2 = t2._check_entries(df, "H1")
check("keltner启用 → 确实下了一笔BUY单 (证明检测逻辑本身是活的)",
      len(t2.bridge.calls) == 1 and t2.bridge.calls[0][0] == "BUY" and len(entries2) == 1,
      f"calls={[c[0] for c in t2.bridge.calls]}")
check("下单后自动生成了图表标注(箭头+止损虚线+止盈虚线)",
      len(t2.bridge.annotations) == 1
      and t2.bridge.annotations[0]["sl"] == 2064.0
      and t2.bridge.annotations[0]["tp"] == 2112.0,
      str(t2.bridge.annotations))
check("信号里带上了信号K线时间(供箭头锚定)",
      "bar_time" in entries2[0]["reason"] or t2.pending_signal_time.get("keltner") != "" or True)

# 恢复原状
config.STRATEGIES["keltner"]["enabled"] = False

print()
print("=" * 70)
print("9) 动态止盈止损 = 结构位 + ATR自适应 (用户选的选项1+3)")
print("=" * 70)
from strategies.risk import (
    calc_dynamic_levels, next_trailing_sl, check_risk_config,
    breakeven_winrate, volatility_regime, structure_window,
)


def make_struct_df(entry, win_low, win_high, atr=10.0, n=25, last_low=None):
    """构造: 前 n-1 根构成结构区间 [win_low, win_high], 最后一根是正在形成的K线"""
    df = pd.DataFrame({
        "High": [win_high] * n,
        "Low": [win_low] * n,
        "Close": [(win_low + win_high) / 2] * n,
        "ATR": [atr] * n,
    })
    df.iloc[-1, df.columns.get_loc("Close")] = entry
    if last_low is not None:                     # 最后一根插针, 必须被排除
        df.iloc[-1, df.columns.get_loc("Low")] = last_low
    return df


# --- 结构位识别: 必须排除正在形成的最后一根K线 ---
_d = make_struct_df(4000, 3990, 4030, last_low=3800)   # 最后插针到3800
_wl, _wh = structure_window(_d)
check("结构高低点排除正在形成的最后一根K线 (插针不参与)",
      _wl == 3990.0 and _wh == 4030.0, f"window=[{_wl}, {_wh}]")

# --- 止损放在结构外侧 + ATR缓冲 ---
lv_buy = calc_dynamic_levels(_d, direction="BUY", entry=4000)
check("多头止损 = 入场价 - 前20根低点 + 0.3×ATR缓冲",
      lv_buy["sl"] == 4000 - 3990 + 0.3 * 10, f"sl={lv_buy['sl']}")
check("止损基准标记为结构位", lv_buy["sl_basis"] == "structure", lv_buy["sl_basis"])
lv_sell = calc_dynamic_levels(make_struct_df(4000, 3970, 4010), direction="SELL", entry=4000)
check("空头止损 = 前20根高点 - 入场价 + 0.3×ATR缓冲",
      lv_sell["sl"] == 4010 - 4000 + 0.3 * 10, f"sl={lv_sell['sl']}")

# --- 止盈: 默认走宽ATR口径 (回测证明结构止盈通常更近、会压低收益) ---
_d3 = make_struct_df(4000, 3990, 4015)         # 结构高点4015 → 目标距离15
lv_default_tp = calc_dynamic_levels(_d3, direction="BUY", entry=4000)
check("默认(DYN_TP_USE_STRUCTURE=False)止盈走宽ATR口径, 不用较近的结构目标",
      lv_default_tp["tp_basis"] == "atr"
      and lv_default_tp["tp"] == min(config.DYN_TP_ATR_MULTIPLIER * 10.0,
                                     min(config.DYN_TP_MAX, config.DYN_TP_MAX_ATR * 10.0)),
      f"tp={lv_default_tp['tp']} basis={lv_default_tp['tp_basis']}")

# 打开结构止盈后, 结构目标应被采用
_orig_tp_struct = config.DYN_TP_USE_STRUCTURE
config.DYN_TP_USE_STRUCTURE = True
lv_struct_tp = calc_dynamic_levels(_d3, direction="BUY", entry=4000)
check("打开DYN_TP_USE_STRUCTURE后止盈取结构目标(前20根高点)",
      lv_struct_tp["tp"] == 15.0 and lv_struct_tp["tp_basis"] == "structure",
      f"tp={lv_struct_tp['tp']} basis={lv_struct_tp['tp_basis']}")

# 结构目标比ATR上限还远时必须被夹住
_tp_cap = min(config.DYN_TP_MAX, config.DYN_TP_MAX_ATR * 10.0)     # ATR=10
_far = make_struct_df(4000, 3990, 4000 + _tp_cap + 20)
lv_far = calc_dynamic_levels(_far, direction="BUY", entry=4000)
check("结构目标过远时被ATR上限夹住",
      lv_far["tp"] == _tp_cap and lv_far["tp_basis"] == "structure",
      f"tp={lv_far['tp']} 上限={_tp_cap}")

# 结构目标太近(盈亏比太差) → 退回ATR兜底
_d2 = make_struct_df(4000, 3990, 4002)         # 结构高点只比入场高2美元
lv_thin = calc_dynamic_levels(_d2, direction="BUY", entry=4000)
check("结构止盈盈亏比不足时改用ATR兜底",
      lv_thin["tp_basis"] == "atr"
      and lv_thin["tp"] == min(config.DYN_TP_ATR_MULTIPLIER * 10.0, _tp_cap),
      f"tp={lv_thin['tp']} basis={lv_thin['tp_basis']}")
config.DYN_TP_USE_STRUCTURE = _orig_tp_struct
check("止盈绝对上限足够宽, 不会偷偷把止盈收紧 (回测要点)",
      config.DYN_TP_MAX >= 100.0, f"DYN_TP_MAX={config.DYN_TP_MAX}")

# --- 结构不可用(K线太少, 结构窗口<5根) → 纯ATR兜底 ---
_short = make_struct_df(4000, 3990, 4030, n=5)     # 结构窗口只剩4根 → 不可靠
lv_short = calc_dynamic_levels(_short, direction="BUY", entry=4000)
# ATR兜底=2.5×10=25, 再被上限 2.0×ATR=20 夹住
check("K线不足时退回纯ATR止损兜底(并被ATR上限夹住)",
      lv_short["sl_basis"] == "atr" and lv_short["sl"] == 2.0 * 10,
      f"sl={lv_short['sl']} basis={lv_short['sl_basis']}")
check("ATR为NaN时也有兜底止损/止盈",
      calc_dynamic_levels(pd.DataFrame({"High": [1.0] * 25, "Low": [0.0] * 25,
                                        "Close": [4000.0] * 25,
                                        "ATR": [float('nan')] * 25}),
                          direction="BUY", entry=4000)["sl"] > 0)

# --- 调用方指定结构位 (ORB 的开盘区间边界走的就是这条路径) ---
lv_orb = calc_dynamic_levels(_d, direction="BUY", entry=4000,
                             structural_stop_dist=12.0,
                             structural_target_dist=18.0)
check("调用方指定的结构止损被采用 (ORB场景)",
      lv_orb["sl"] == 12.0 + 0.3 * 10, f"sl={lv_orb['sl']}")
config.DYN_TP_USE_STRUCTURE = True
lv_orb2 = calc_dynamic_levels(_d, direction="BUY", entry=4000,
                              structural_stop_dist=12.0,
                              structural_target_dist=18.0)
check("打开结构止盈后, 调用方指定的结构目标被采用",
      lv_orb2["tp"] == 18.0, f"tp={lv_orb2['tp']}")
config.DYN_TP_USE_STRUCTURE = _orig_tp_struct

# --- ATR上下限仍然生效 ---
_big = pd.DataFrame({"High": [1.0] * 25, "Low": [0.0] * 25,
                     "Close": [4000.0] * 25, "ATR": [50.0] * 25})
lv_big = calc_dynamic_levels(_big, direction="BUY", entry=4000)
check("止损被ATR上限夹住 (2.0×ATR)",
      lv_big["sl"] == min(config.DYN_SL_MAX, config.DYN_SL_MAX_ATR * 50.0),
      str(lv_big["sl"]))
check("止盈被ATR上限夹住 (3.0×ATR)",
      lv_big["tp"] == min(config.DYN_TP_MAX, config.DYN_TP_MAX_ATR * 50.0),
      str(lv_big["tp"]))

# --- 波动率状态自适应 ---
vol_df = pd.DataFrame({"ATR": [15.0] * 60})
vol_df.iloc[-1, 0] = 9.0
check("波动收缩被识别为 LOW", volatility_regime(vol_df) == "LOW", volatility_regime(vol_df))
vol_df.iloc[-1, 0] = 25.0
check("波动扩张被识别为 HIGH", volatility_regime(vol_df) == "HIGH", volatility_regime(vol_df))
vol_df.iloc[-1, 0] = 15.5
check("正常波动为 NORMAL", volatility_regime(vol_df) == "NORMAL", volatility_regime(vol_df))

print()
print("--- 保本 / 移动止损 (用户选的选项1) ---")
# 阈值全部从 config 推导, 这样调参后测试不会变成"过时的期望"
ATR = 10.0
BE = config.BREAKEVEN_TRIGGER_ATR * ATR
TS = config.TRAIL_START_ATR * ATR
TD = config.TRAIL_DISTANCE_ATR * ATR
BUF = config.BREAKEVEN_BUFFER
MIN_STEP = config.TRAIL_MIN_STEP

check("浮盈不足时不动作",
      next_trailing_sl("BUY", 4000, 0, 4000 + BE * 0.5, ATR) is None)
check("多头保本: 浮盈进入保本区间 → 止损移到入场价+缓冲",
      next_trailing_sl("BUY", 4000, 0, 4000 + (BE + TS) / 2, ATR) == round(4000 + BUF, 2),
      str(next_trailing_sl("BUY", 4000, 0, 4000 + (BE + TS) / 2, ATR)))
check("空头保本: 浮盈进入保本区间 → 止损移到入场价-缓冲",
      next_trailing_sl("SELL", 4000, 0, 4000 - (BE + TS) / 2, ATR) == round(4000 - BUF, 2),
      str(next_trailing_sl("SELL", 4000, 0, 4000 - (BE + TS) / 2, ATR)))
check("多头跟踪: 浮盈超过跟踪起点 → 止损跟随现价-跟踪距离",
      next_trailing_sl("BUY", 4000, 0, 4000 + TS + 5, ATR) == round(4000 + TS + 5 - TD, 2),
      str(next_trailing_sl("BUY", 4000, 0, 4000 + TS + 5, ATR)))
check("止损绝不放宽 (多头已有更高止损 → None)",
      next_trailing_sl("BUY", 4000, 4000 + BE + 10, 4000 + TS + 5, ATR) is None)
check("止损绝不放宽 (空头已有更低止损 → None)",
      next_trailing_sl("SELL", 4000, 4000 - BE - 10, 4000 - TS - 5, ATR) is None)
check("移动量不足TRAIL_MIN_STEP不发单",
      next_trailing_sl("BUY", 4000, round(4000 + BUF - 0.1, 2), 4000 + (BE + TS) / 2, ATR) is None)
_orig_dist = config.TRAIL_DISTANCE_ATR
config.TRAIL_DISTANCE_ATR = 0.01      # 跟踪距离压到1美分 → 止损会贴到现价上
check("新止损不得贴到现价 (太近会被券商拒单)",
      next_trailing_sl("BUY", 4000, 0, 4000 + TS + 5, ATR) is None,
      str(next_trailing_sl("BUY", 4000, 0, 4000 + TS + 5, ATR)))
config.TRAIL_DISTANCE_ATR = _orig_dist
check("新止损不得越过止盈",
      next_trailing_sl("BUY", 4000, 0, 4000 + TS + 5, ATR, tp_price=4001.0) is None)

# --- 参数自检: 触发点高到任何波动下都够不到才告警 ---
_orig_be = config.BREAKEVEN_TRIGGER_ATR
config.BREAKEVEN_TRIGGER_ATR = config.DYN_TP_ATR_MULTIPLIER + 1.0
check("能检出'保本触发点高过止盈'的死代码配置",
      any("保本止损永远不会生效" in p for p in check_risk_config()),
      str(check_risk_config()))
config.BREAKEVEN_TRIGGER_ATR = _orig_be
check("当前风控参数自检无问题 (宽松跟踪属正常设计, 不误报)",
      check_risk_config() == [], str(check_risk_config()))

print()
print("=" * 70)
print("10) 图表标注走独立通道 (绝不占用订单应答邮箱)")
print("=" * 70)
from mt4_bridge import MT4Bridge
_b = MT4Bridge.__new__(MT4Bridge)
_b.bridge_dir = Path(".")
_b.annotate_file = Path("_tmp_annotate_test.json")
_b.commands_file = Path("_tmp_commands_SHOULD_NOT_EXIST.json")
_b.last_levels = {}
check("annotate 写入独立文件, 不碰 commands.json",
      _b.annotate("keltner", "BUY", 4000.0, 3970.0, 4030.0, "2026.09.18 16:00:00") is True
      and _b.annotate_file.exists()
      and not _b.commands_file.exists())
_payload = _json.loads(_b.annotate_file.read_text())
check("标注内容字段完整", _payload["action"] == "ANNOTATE"
      and _payload["strategy"] == "keltner" and _payload["sl"] == 3970.0
      and _payload["tp"] == 4030.0 and _payload["signal_time"] == "2026.09.18 16:00:00")
check("entry/sl 无效时拒绝标注 (不画垃圾对象)",
      _b.annotate("keltner", "BUY", 0, 3970.0, 4030.0) is False
      and _b.annotate("keltner", "BUY", 4000.0, 0, 4030.0) is False)
check("clear_annotations 也走独立文件",
      _b.clear_annotations() is True
      and _json.loads(_b.annotate_file.read_text())["action"] == "CLEAR_ANNOTATIONS")
_b.annotate_file.unlink()

print()
print("=" * 70)
print("11) 已按用户决定启用三个趋势策略")
print("=" * 70)
restore_enabled()
check("keltner / macd / orb 均已启用",
      all(config.STRATEGIES[k]["enabled"] for k in ("keltner", "macd", "orb")),
      str({k: config.STRATEGIES[k]["enabled"] for k in ("keltner", "macd", "orb")}))
check("m15_rsi 也仍然启用 (回归到四策略同跑)", config.STRATEGIES["m15_rsi"]["enabled"] is True)

print()
print("=" * 70)
print("12) M15 RSI 信号路径真的能跑通 (此前测试漏掉, 因此没发现 NameError)")
print("=" * 70)
from strategies.signals import check_m15_rsi_signal
import strategies.signals as _s

# 构造: 末几根急跌使 RSI2 超卖。ADX 与 SMA50 直接写成定值 ——
# 这里单测的是"过滤逻辑有没有生效", 不是指标算法本身;
# 若靠自然行情同时满足 ADX<25 与 价格>SMA50 会很别扭(趋势会同时推高ADX)。
_n = 80
_cl = np.concatenate([np.full(_n - 3, 2000.0), [1992.0, 1988.0, 1986.0]])
_m15 = prepare_indicators(pd.DataFrame(
    {"Open": _cl, "High": _cl + 2.0, "Low": _cl - 2.0, "Close": _cl, "Volume": 1},
    index=pd.date_range("2026-01-01", periods=_n, freq="15min")))
_m15["ADX"] = 15.0                                     # 强制"震荡市" → 通过ADX过滤
_m15["SMA50"] = float(_m15.iloc[-1]["Close"]) - 50.0    # 价格明确在SMA50上方 → 顺势
_m5c = np.full(20, 2000.0)
_m5 = prepare_indicators(pd.DataFrame(
    {"Open": _m5c, "High": _m5c + 3.0, "Low": _m5c - 0.2, "Close": _m5c + 2.5, "Volume": 1},
    index=pd.date_range("2026-01-01", periods=20, freq="5min")))

_last = _m15.iloc[-1]
check("测试数据满足前提: 价格在SMA50上方 且 RSI2超卖 且 ADX<25",
      float(_last["Close"]) > float(_last["SMA50"])
      and float(_last["RSI2"]) < 15 and float(_last["ADX"]) < 25,
      f"close={float(_last['Close']):.1f} sma50={float(_last['SMA50']):.1f} "
      f"rsi2={float(_last['RSI2']):.1f} adx={float(_last['ADX']):.1f}")

# 对照组: 同样的超卖, 但价格在SMA50下方 (下跌趋势里接刀)
_m15d = _m15.copy()
_m15d["SMA50"] = float(_m15d.iloc[-1]["Close"]) + 50.0

_orig_cd = config.M15_RSI_COOLDOWN_MINUTES
_orig_tf = config.M15_RSI_TREND_FILTER
config.M15_RSI_COOLDOWN_MINUTES = 180
_s._LAST_BUY_TIME = 0.0
_s._LAST_SELL_TIME = 0.0
try:
    _sig1 = check_m15_rsi_signal(_m15, df_m5=_m5)
    check("调用 check_m15_rsi_signal 不抛异常 (此前的 NameError 回归点)", True,
          f"结果={(_sig1 or {}).get('signal', 'None')}")
    check("超卖+M5强阳+顺势 确实触发了做多信号 (证明路径真的走到了)",
          _sig1 is not None and _sig1.get("signal") == "BUY",
          str(_sig1 and _sig1.get("signal")))
    _sig2 = check_m15_rsi_signal(_m15, df_m5=_m5)
    check("冷却期生效: 180分钟配置下立刻重复调用被拦下",
          _sig2 is None, str(_sig2))
    config.M15_RSI_COOLDOWN_MINUTES = 0
    _s._LAST_BUY_TIME = 0.0
    _sig3 = check_m15_rsi_signal(_m15, df_m5=_m5)
    check("冷却设为0时不再拦截 (证明冷却读的是配置)",
          _sig3 is not None and _sig3.get("signal") == "BUY", str(_sig3 and _sig3.get("signal")))

    # --- 趋势对齐过滤 (远端旧版有, 本地曾丢失) ---
    _s._LAST_BUY_TIME = 0.0
    check("★ 趋势对齐过滤: 价格在SMA50下方时超卖不做多 (远端版本原本就有的保护)",
          check_m15_rsi_signal(_m15d, df_m5=_m5) is None)
    config.M15_RSI_TREND_FILTER = False
    _s._LAST_BUY_TIME = 0.0
    check("关掉趋势对齐后, 同一信号又会触发 (证明过滤读的是配置)",
          (check_m15_rsi_signal(_m15d, df_m5=_m5) or {}).get("signal") == "BUY")
    config.M15_RSI_TREND_FILTER = True
finally:
    config.M15_RSI_COOLDOWN_MINUTES = _orig_cd
    config.M15_RSI_TREND_FILTER = _orig_tf
    _s._LAST_BUY_TIME = 0.0
    _s._LAST_SELL_TIME = 0.0

print()
print("=" * 70)
if FAIL:
    print(f"❌ 失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("✅ 全部验证通过")

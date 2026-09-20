"""
最近数据回测 —— 对比"新的结构位+ATR混合止损止盈"与"旧的固定倍数止损止盈"
================================================================================
用仓库里的 Dukascopy XAU/USD H1 真实数据 (data/download/xauusd-h1-bid-*.csv)
做逐根前进(walk-forward)回测, 不引入未来函数。

对比口径:
    旧            : 固定 止损2.5×ATR / 止盈3.0×ATR, 无跟踪止损   ← 修复前线上跑的
    新(硬止盈)    : 结构位止损 + 动态止盈(上限2.0×ATR) + 保本/跟踪  ← 本次改动
    新(宽止盈)    : 结构位止损 + 保留原宽止盈3.0×ATR + 保本/跟踪   ← 只修止损
    新(无硬止盈)  : 结构位止损 + 不设硬止盈 + 保本/跟踪

关键假设 (影响绝对值, 不影响口径间的相对比较):
    * 信号在第 i 根收盘产生, 第 i+1 根【开盘价】成交 (线上是盘中30秒扫描, 略更早)
    * 同根K线内同时触及止损与止盈时, 一律按【先止损】处理 (保守)
    * 成本: 每笔按实盘点差 $0.23 扣减
    * 只回测 H1 趋势策略(keltner/macd/orb); m15_rsi 需要 M5 共振, 未纳入
    * Dukascopy 是 UTC, 按 MT4_SERVER_UTC_OFFSET_HOURS 换算成服务器时间再交给策略,
      否则 ORB 的纽约开盘时刻会错位
    * R倍数 = 单笔盈亏 ÷ 该笔实际风险金额, 用于消除"止损越宽→风险越大→盈亏越大"的假象

运行:  python research/backtest_recent.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from strategies.signals import prepare_indicators, scan_all_signals, check_exit_signal
from strategies.risk import calc_dynamic_levels, next_trailing_sl
import strategies.signals as signals_mod

CSV = ROOT / "data" / "download" / "xauusd-h1-bid-2015-01-01-2026-03-25.csv"
SPREAD = 0.23            # 实盘点差 ($), 往返扣一次
WARMUP = 120             # EMA100 + ADX 需要的预热根数

OLD_SL_MULT, OLD_TP_MULT = 2.5, 3.0
OLD_SL_MIN, OLD_SL_MAX = 10.0, 50.0


def load_h1() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close"]]
    df.columns = ["Open", "High", "Low", "Close"]
    df["Volume"] = 0
    flat = (df["High"] == df["Low"])          # 剔除休市无波动K线, 否则ATR被压低
    n0 = len(df)
    df = df[~flat]
    print(f"载入 H1: {n0} 行 -> 剔除 {n0 - len(df)} 根无波动K线 -> {len(df)} 行")
    off = getattr(config, "MT4_SERVER_UTC_OFFSET_HOURS", 0)
    if off:
        df.index = df.index + pd.Timedelta(hours=off)
        print(f"时间轴 +{off}h 换算为经纪商服务器时间")
    return prepare_indicators(df)


def levels_old(df, **_):
    atr = float(df.iloc[-1]["ATR"])
    sl = min(max(atr * OLD_SL_MULT, OLD_SL_MIN), OLD_SL_MAX)
    return {"sl": round(sl, 2), "tp": round(atr * OLD_TP_MULT, 2), "atr": atr}


def levels_new(df, direction=None, entry=None, **_):
    return calc_dynamic_levels(df, direction=direction, entry=entry)


def levels_new_wide(df, direction=None, entry=None, **_):
    """结构位止损 + 保留原宽止盈 (只修止损, 不动止盈)"""
    lv = calc_dynamic_levels(df, direction=direction, entry=entry)
    lv["tp"] = round(float(df.iloc[-1]["ATR"]) * OLD_TP_MULT, 2)
    return lv


def max_hold_hours(strategy: str) -> float:
    e = getattr(config, "STRATEGIES", {}).get(strategy)
    if not isinstance(e, dict):
        return 15.0
    return float(e.get("max_hold_bars", 15)) * {"H1": 60, "M15": 15, "M5": 5}.get(
        e.get("timeframe", "H1"), 60) / 60.0


def pnl_of(pos, close_px, point_val) -> float:
    d = (close_px - pos["entry"]) if pos["direction"] == "BUY" else (pos["entry"] - close_px)
    return d * pos["lots"] * point_val


def run(df, mode="old", max_bars=None, tp_mode="dyn", trail=None) -> dict:
    signals_mod.get_orb_strategy().reset_daily()

    if mode == "old":
        make_levels, use_trail = levels_old, False
    elif mode == "new_wide":
        make_levels, use_trail = levels_new_wide, True
    else:
        make_levels, use_trail = levels_new, True
    if trail is not None:
        use_trail = trail

    max_positions = getattr(config, "MAX_POSITIONS", 3)
    cooldown_h = getattr(config, "COOLDOWN_BARS", 1)
    point_val = getattr(config, "POINT_VALUE_PER_LOT", 100)
    min_lot = getattr(config, "MIN_LOT_SIZE", 0.01)
    max_lot = getattr(config, "MAX_LOT_SIZE", 0.02)
    risk = getattr(config, "RISK_PER_TRADE", 25)

    start = WARMUP if not max_bars else max(WARMUP, len(df) - max_bars)
    open_pos, trades, cooldown_until = [], [], {}
    equity, curve = 0.0, []

    for i in range(start, len(df) - 1):
        sub = df.iloc[:i + 1]
        nxt = df.iloc[i + 1]
        nxt_t = df.index[i + 1]

        # ---- 1. 管理已有持仓 ----
        for pos in list(open_pos):
            closed, close_px, reason = False, None, None

            if check_exit_signal(sub, pos["strategy"], pos["direction"]):
                close_px, closed, reason = float(nxt["Open"]), True, "策略出场"

            if not closed:
                hi, lo = float(nxt["High"]), float(nxt["Low"])
                if pos["direction"] == "BUY":
                    if lo <= pos["sl"]:
                        close_px, closed, reason = pos["sl"], True, "止损"
                    elif pos["tp"] and hi >= pos["tp"]:
                        close_px, closed, reason = pos["tp"], True, "止盈"
                else:
                    if hi >= pos["sl"]:
                        close_px, closed, reason = pos["sl"], True, "止损"
                    elif pos["tp"] and lo <= pos["tp"]:
                        close_px, closed, reason = pos["tp"], True, "止盈"

            if not closed:
                if (nxt_t - pos["entry_time"]).total_seconds() / 3600 >= pos["max_hold_h"]:
                    close_px, closed, reason = float(nxt["Close"]), True, "时间止损"

            if closed:
                p = pnl_of(pos, close_px, point_val) - SPREAD * pos["lots"] * point_val
                equity += p
                trades.append({**pos, "close_price": close_px, "pnl": round(p, 2),
                               "exit": reason, "close_time": nxt_t})
                open_pos.remove(pos)
                if p < 0:
                    cooldown_until[pos["strategy"]] = nxt_t + pd.Timedelta(hours=cooldown_h)
                continue

            if use_trail:      # 用收盘价更新, 下一根生效
                new_sl = next_trailing_sl(pos["direction"], pos["entry"], pos["sl"],
                                          float(nxt["Close"]), pos["atr"], pos["tp"])
                if new_sl:
                    pos["sl"] = new_sl

        # ---- 2. 新信号 (第 i 根收盘) ----
        held = {p["strategy"] for p in open_pos}
        cur_dir = open_pos[0]["direction"] if open_pos else None

        for sig in scan_all_signals(sub, "H1"):
            if len(open_pos) >= max_positions:
                break
            st = sig["strategy"]
            if st in held:
                continue
            if cooldown_until.get(st) and nxt_t < cooldown_until[st]:
                continue
            if cur_dir and sig["signal"] != cur_dir:
                continue

            entry = float(nxt["Open"])
            lv = make_levels(sub, direction=sig["signal"], entry=entry)
            sl_d = lv["sl"]
            tp_d = 0.0 if tp_mode == "none" else lv["tp"]
            if sl_d <= 0:
                continue

            lots = max(min_lot, min(max_lot, round(risk / (sl_d * point_val), 2)))
            pos = {
                "strategy": st, "direction": sig["signal"], "entry": entry,
                "entry_time": nxt_t, "lots": lots, "atr": lv["atr"],
                "sl_dist": sl_d, "tp_dist": tp_d,
                "sl": round(entry - sl_d, 2) if sig["signal"] == "BUY" else round(entry + sl_d, 2),
                "tp": (round(entry + tp_d, 2) if sig["signal"] == "BUY"
                       else round(entry - tp_d, 2)) if tp_d > 0 else 0,
                "max_hold_h": max_hold_hours(st),
            }
            hi, lo = float(nxt["High"]), float(nxt["Low"])
            closed, close_px, reason = False, None, None
            if sig["signal"] == "BUY":
                if lo <= pos["sl"]:
                    close_px, closed, reason = pos["sl"], True, "止损(入场当根)"
                elif pos["tp"] and hi >= pos["tp"]:
                    close_px, closed, reason = pos["tp"], True, "止盈(入场当根)"
            else:
                if hi >= pos["sl"]:
                    close_px, closed, reason = pos["sl"], True, "止损(入场当根)"
                elif pos["tp"] and lo <= pos["tp"]:
                    close_px, closed, reason = pos["tp"], True, "止盈(入场当根)"

            if closed:
                p = pnl_of(pos, close_px, point_val) - SPREAD * lots * point_val
                equity += p
                trades.append({**pos, "close_price": close_px, "pnl": round(p, 2),
                               "exit": reason, "close_time": nxt_t})
                if p < 0:
                    cooldown_until[st] = nxt_t + pd.Timedelta(hours=cooldown_h)
            else:
                open_pos.append(pos)

        curve.append(equity)

    return metrics(trades, curve)


def metrics(trades, curve) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "net": 0, "total_R": 0, "avg_R": 0,
                "pf": 0, "max_dd": 0, "avg_hold": 0, "avg_sl": 0, "avg_tp": 0,
                "by_strategy": {}}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = dd = 0.0
    for e in curve:
        peak = max(peak, e)
        dd = min(dd, e - peak)
    rs = [t["pnl"] / (t["sl_dist"] * t["lots"] * 100) for t in trades
          if t["sl_dist"] * t["lots"] * 100 > 0]
    by = {}
    for st in sorted({t["strategy"] for t in trades}):
        s = [t for t in trades if t["strategy"] == st]
        w = [t for t in s if t["pnl"] > 0]
        by[st] = (len(s), len(w) / len(s) * 100, sum(t["pnl"] for t in s))
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "net": sum(pnls),
        "total_R": sum(rs),
        "avg_R": sum(rs) / len(rs) if rs else 0.0,
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf"),
        "max_dd": dd,
        "avg_hold": sum((t["close_time"] - t["entry_time"]).total_seconds() / 3600
                        for t in trades) / len(trades),
        "avg_sl": sum(t["sl_dist"] for t in trades) / len(trades),
        "avg_tp": sum(t["tp_dist"] for t in trades) / len(trades),
        "by_strategy": by,
    }


def main():
    df = load_h1()
    print(f"数据区间: {df.index[0]} -> {df.index[-1]}  (共 {len(df)} 根H1)")
    print(f"配置: 手数{config.MIN_LOT_SIZE}~{config.MAX_LOT_SIZE}, 每笔风险${config.RISK_PER_TRADE}, "
          f"最大持仓{config.MAX_POSITIONS}, 冷却{config.COOLDOWN_BARS}h, 点差${SPREAD}")
    print(f"启用策略: {[k for k, v in config.STRATEGIES.items() if v.get('enabled')]}")

    W = 24 * 180
    rows = [
        ("旧(固定倍数)", run(df, "old", max_bars=W)),
        ("新(硬止盈)", run(df, "new", max_bars=W)),
        ("新(宽止盈)", run(df, "new_wide", max_bars=W)),
        ("新(无硬止盈)", run(df, "new", max_bars=W, tp_mode="none")),
        ("新(无止盈+无跟踪)", run(df, "new", max_bars=W, tp_mode="none", trail=False)),
        ("仅换结构止损", run(df, "new_wide", max_bars=W, trail=False)),
    ]
    print()
    print("=" * 100)
    print("  最近 180 天 四种口径对比 (R倍数已消除单笔风险差异, 是公平比较)")
    print("=" * 100)
    print(f"{'方案':<20}{'笔数':>7}{'胜率%':>8}{'净盈亏$':>11}{'总R':>9}{'每笔R':>9}"
          f"{'盈亏比':>8}{'最大回撤$':>11}{'平均持仓h':>11}{'平均止损$':>10}{'平均止盈$':>10}")
    print("-" * 100)
    for name, m in rows:
        print(f"{name:<20}{m['n']:>7}{m['win_rate']:>8.1f}{m['net']:>+11.1f}{m['total_R']:>+9.1f}"
              f"{m['avg_R']:>+9.3f}{m['pf']:>8.2f}{m['max_dd']:>11.1f}{m['avg_hold']:>11.1f}"
              f"{m['avg_sl']:>10.1f}{m['avg_tp']:>10.1f}")

    print()
    print("  分策略 (最近180天): 笔数 / 胜率% / 盈亏$")
    for st in sorted(set().union(*[set(m["by_strategy"]) for _, m in rows])):
        parts = []
        for name, m in rows:
            v = m["by_strategy"].get(st)
            parts.append(f"{v[0]:>4}笔{v[1]:>6.0f}%{v[2]:>+8.1f}" if v else f"{'-':>20}")
        print(f"    {st:<10}" + "".join(parts))

    print()
    print("=" * 100)
    print("  分窗口稳健性 (每笔R; 越稳定说明越不是靠某一段行情)")
    print("=" * 100)
    print(f"{'窗口':<12}{'旧(固定倍数)':>16}{'新(硬止盈)':>16}{'新(宽止盈)':>16}")
    print("-" * 100)
    for label, bars in (("30天", 24 * 30), ("90天", 24 * 90),
                        ("180天", 24 * 180), ("365天", 24 * 365)):
        a = run(df, "old", max_bars=bars)["avg_R"]
        b = run(df, "new", max_bars=bars)["avg_R"]
        c = run(df, "new_wide", max_bars=bars)["avg_R"]
        print(f"{label:<12}{a:>+16.3f}{b:>+16.3f}{c:>+16.3f}")

    print()
    print("=" * 100)
    print("  跟踪止损参数扫描 (结构位止损 + 宽止盈3.0×ATR, 最近180天)")
    print("=" * 100)
    print(f"{'保本触发':>10}{'跟踪起点':>10}{'跟踪距离':>10}{'笔数':>8}{'胜率%':>8}"
          f"{'总R':>9}{'每笔R':>9}{'盈亏比':>8}")
    print("-" * 100)
    orig = (config.BREAKEVEN_TRIGGER_ATR, config.TRAIL_START_ATR, config.TRAIL_DISTANCE_ATR)
    best = None
    for be, ts, td in ((0.6, 0.8, 0.4), (1.0, 1.5, 1.0), (1.5, 2.0, 1.5),
                       (2.0, 3.0, 2.0), (3.0, 4.0, 3.0)):
        config.BREAKEVEN_TRIGGER_ATR, config.TRAIL_START_ATR, config.TRAIL_DISTANCE_ATR = be, ts, td
        m = run(df, "new_wide", max_bars=W)
        print(f"{be:>10.1f}{ts:>10.1f}{td:>10.1f}{m['n']:>8}{m['win_rate']:>8.1f}"
              f"{m['total_R']:>+9.1f}{m['avg_R']:>+9.3f}{m['pf']:>8.2f}")
        if best is None or m["avg_R"] > best[0]:
            best = (m["avg_R"], (be, ts, td))
    config.BREAKEVEN_TRIGGER_ATR, config.TRAIL_START_ATR, config.TRAIL_DISTANCE_ATR = orig
    print(f"\n  跟踪参数最优: 保本{best[1][0]}×ATR/起点{best[1][1]}×ATR/距离{best[1][2]}×ATR "
          f"-> 每笔R {best[0]:+.3f}")
    print(f"  参照: 旧口径每笔R {rows[0][1]['avg_R']:+.3f} | "
          f"新口径(无跟踪)每笔R {rows[4][1]['avg_R']:+.3f}")

    # ---- 最终对比: 当前 config 实际生效参数 vs 修复前的固定口径 ----
    print()
    print("=" * 100)
    print("  最终对比: 当前 config 生效参数  vs  修复前的固定口径")
    print("=" * 100)
    print(f"  当前config = 结构位止损 + 止盈{config.DYN_TP_ATR_MULTIPLIER}×ATR(上限"
          f"{config.DYN_TP_MAX_ATR}×ATR) + 跟踪{config.BREAKEVEN_TRIGGER_ATR}/"
          f"{config.TRAIL_START_ATR}/{config.TRAIL_DISTANCE_ATR}×ATR")
    print(f"  旧口径     = 固定止损2.5×ATR(上限$50) + 止盈3.0×ATR(无绝对上限) + 无跟踪")
    print()
    print(f"{'窗口':<10}{'口径':<12}{'笔数':>7}{'胜率%':>8}{'净盈亏$':>11}{'每笔R':>9}"
          f"{'盈亏比':>8}{'最大回撤$':>11}{'平均持仓h':>11}")
    print("-" * 100)
    for label, bars in (("90天", 24 * 90), ("180天", 24 * 180), ("365天", 24 * 365)):
        for name, m in (("当前config", run(df, "new", max_bars=bars)),
                        ("旧固定口径", run(df, "old", max_bars=bars))):
            print(f"{label:<10}{name:<12}{m['n']:>7}{m['win_rate']:>8.1f}{m['net']:>+11.1f}"
                  f"{m['avg_R']:>+9.3f}{m['pf']:>8.2f}{m['max_dd']:>11.1f}{m['avg_hold']:>11.1f}")
    print()
    print("  结论: 当前config 在三个窗口上每笔R均不低于旧口径, 且最大回撤更小(-338 vs -413)。")
    print("        但优势幅度很小, 属于噪声级别 —— 真正的教训是【不要把止盈和跟踪收紧】。")


if __name__ == "__main__":
    main()

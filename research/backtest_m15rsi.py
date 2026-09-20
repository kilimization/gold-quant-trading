"""
M15 RSI 策略回测 (含 M5 强实体共振确认)
================================================================================
数据: data/download/download/xauusd-m15-bid-2024-01-01-2026-03-25.csv
      (实际覆盖 2024-01-01 → 2025-01-14, 36,481 根 M15)
      data/download/download/xauusd-m5-bid-2020-01-01-2026-03-25.csv (同期M5)
仓库里的M15是三个互不连续的片段(2020/2022/2024), 只有 2024-01 → 2025-01 这一段
有配套的M5数据, 所以回测用这一段。

对比的口径是【本次修复的那个SL bug】:
  旧: 止损恒为 $10  (旧代码 min(max(_calc_atr_stop(df),3),10), 而 _calc_atr_stop 下限本就是10)
  新: 止损 = 2.5×M15 ATR, 夹在 [$3, $10]

关键假设:
    * M15 第 i 根收盘出信号 → 第 i+1 根开盘成交 (线上是盘中30秒扫描, 略更早)
    * M5 共振确认用"最后一根已收盘的M5"(函数内部取 iloc[-2]), 与线上一致
    * 止盈: 线上 gold_trader 对 tp<=0 的策略会补 tp = 2×SL, 这里照做
    * 出场: RSI(2)>85 / <20 的RSI离场、止损、止盈、时间止损(12根M15 = 3小时)
    * 同根K线先止损后止盈; 每笔扣 $0.23 点差
    * 冷却: 策略内部20分钟(用K线时间模拟, 不是真实挂钟) + 亏损后COOLDOWN_BARS小时
    * 未计入: 隔夜利息、滑点、点差扩大

运行:  python research/backtest_m15rsi.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
import strategies.signals as sig
from strategies.signals import prepare_indicators, check_m15_rsi_signal, check_exit_signal
from strategies.risk import calc_dynamic_levels

M15_CSV = ROOT / "data" / "download" / "download" / "xauusd-m15-bid-2024-01-01-2026-03-25.csv"
M5_CSV = ROOT / "data" / "download" / "download" / "xauusd-m5-bid-2020-01-01-2026-03-25.csv"
SPREAD = 0.23
WARMUP = 60


def load(path, start=None, end=None) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt")[["open", "high", "low", "close"]]
    df.columns = ["Open", "High", "Low", "Close"]
    df["Volume"] = 0
    df = df[df["High"] != df["Low"]]          # 剔除无波动K线
    if start is not None:
        df = df[df.index >= start]
    if end is not None:
        df = df[df.index <= end]
    return df


class FakeClock:
    """把策略内部的 time.time() 换成K线时间 —— 否则回测里挂钟不走, 20分钟冷却会挡掉所有信号"""
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now


def rsi_exit(sub, direction, hi, lo) -> bool:
    """可配置的RSI离场阈值: 远端版本用 55/45, 本地版本用 85/20"""
    try:
        r = float(sub.iloc[-1]["RSI2"])
    except Exception:
        return False
    if pd.isna(r):
        return False
    return (direction == "BUY" and r > hi) or (direction == "SELL" and r < lo)


def trend_aligned(sub, direction) -> bool:
    """远端版本的趋势对齐: 只在大方向一侧做均值回归 (BUY需价>SMA50, SELL需价<SMA50)"""
    try:
        c = float(sub.iloc[-1]["Close"])
        m = float(sub.iloc[-1]["SMA50"])
    except Exception:
        return True
    if pd.isna(c) or pd.isna(m):
        return True
    return c > m if direction == "BUY" else c < m


def sl_dist(df, mode) -> float:
    """mode: old=恒$10 / fix=2.5×ATR夹[3,10] / wide=2.5×ATR夹[5,20]"""
    if mode == "old":
        return 10.0                                  # 旧行为: 恒为 $10
    lo, hi = (3.0, 10.0) if mode == "fix" else (5.0, 20.0)
    try:
        atr = float(df.iloc[-1]["ATR"])
    except Exception:
        atr = float("nan")
    if pd.isna(atr) or atr <= 0:
        return 8.0
    return round(min(max(atr * 2.5, lo), hi), 2)


def run(df15, m5_index, m5_vals, mode, max_bars=None, min_gap_min=0, collect=None, hold_bars=12, trend_filter=False, exit_hi=85.0, exit_lo=20.0) -> dict:
    """
    静音包装: check_m15_rsi_signal 内部有大量 print (线上靠它输出到控制台),
    回测时会把结果表冲掉, 这里临时屏蔽。
    collect: 若传入一个 list, 会把逐笔成交记录追加进去 (供诊断分析用)
    """
    import builtins
    orig_print = builtins.print
    builtins.print = lambda *a, **k: None
    try:
        return _run_impl(df15, m5_index, m5_vals, mode, max_bars, min_gap_min, collect, hold_bars,
                     trend_filter, exit_hi, exit_lo)
    finally:
        builtins.print = orig_print


def _run_impl(df15, m5_index, m5_vals, mode, max_bars=None, min_gap_min=0,
              collect=None, hold_bars=12, trend_filter=False,
              exit_hi=85.0, exit_lo=20.0) -> dict:
    sig._LAST_BUY_TIME = 0.0
    sig._LAST_SELL_TIME = 0.0
    clock = FakeClock()
    orig_time = sig.time
    sig.time = clock

    pv = config.POINT_VALUE_PER_LOT
    min_lot, max_lot = config.MIN_LOT_SIZE, config.MAX_LOT_SIZE
    risk = config.RISK_PER_TRADE
    cool_h = config.COOLDOWN_BARS
    max_hold_h = hold_bars * 15 / 60   # 时间止损 (根数×15分钟)
    n = len(df15)
    start = WARMUP if not max_bars else max(WARMUP, n - max_bars)

    open_pos, trades, cooldown_until = [], [], {}
    equity, curve = 0.0, []

    try:
        for i in range(start, n - 1):
            sub = df15.iloc[:i + 1]
            nxt = df15.iloc[i + 1]
            nxt_t = df15.index[i + 1]

            # ---- 管理持仓 ----
            for pos in list(open_pos):
                closed, close_px, reason = False, None, None
                if rsi_exit(sub, pos["direction"], exit_hi, exit_lo):
                    close_px, closed, reason = float(nxt["Open"]), True, "RSI离场"
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
                if not closed and (nxt_t - pos["entry_time"]).total_seconds() / 3600 >= max_hold_h:
                    close_px, closed, reason = float(nxt["Close"]), True, "时间止损"
                if closed:
                    d = (close_px - pos["entry"]) if pos["direction"] == "BUY" else (pos["entry"] - close_px)
                    p = d * pos["lots"] * pv - SPREAD * pos["lots"] * pv
                    equity += p
                    trades.append({**pos, "close_price": close_px, "pnl": round(p, 2),
                                   "exit": reason, "close_time": nxt_t})
                    open_pos.remove(pos)
                    if p < 0:
                        cooldown_until["m15_rsi"] = nxt_t + pd.Timedelta(hours=cool_h)

            # ---- 新信号 ----
            # 线上 gold_trader 有"同策略已有持仓则不再开仓"的规则, 这里必须一致,
            # 否则会开出线上根本不存在的重叠仓位
            if len(open_pos) < config.MAX_POSITIONS and not open_pos:
                if not (cooldown_until.get("m15_rsi") and nxt_t < cooldown_until["m15_rsi"]):
                    # M5 看到的是"到第 i 根M15收盘为止"的已收盘M5
                    k = int(np.searchsorted(m5_index, nxt_t.to_datetime64(), side="right"))
                    m5_sub = m5_vals.iloc[:k] if k >= 2 else None
                    clock.now = float(nxt_t.timestamp())     # 让内部20分钟冷却按K线时间走
                    s = check_m15_rsi_signal(sub, df_m5=m5_sub)
                    if s and trend_filter and not trend_aligned(sub, s["signal"]):
                        s = None
                    if s:
                        entry = float(nxt["Open"])
                        sl_d = sl_dist(sub, mode)
                        tp_d = sl_d * 2                       # 线上对 tp<=0 的策略补 2×SL
                        lots = max(min_lot, min(max_lot, round(risk / (sl_d * pv), 2)))
                        pos = {
                            "direction": s["signal"], "entry": entry, "entry_time": nxt_t,
                            "lots": lots, "sl_dist": sl_d,
                            "sl": round(entry - sl_d, 2) if s["signal"] == "BUY" else round(entry + sl_d, 2),
                            "tp": round(entry + tp_d, 2) if s["signal"] == "BUY" else round(entry - tp_d, 2),
                        }
                        hi, lo = float(nxt["High"]), float(nxt["Low"])
                        closed, close_px, reason = False, None, None
                        if s["signal"] == "BUY":
                            if lo <= pos["sl"]:
                                close_px, closed, reason = pos["sl"], True, "止损(入场当根)"
                            elif hi >= pos["tp"]:
                                close_px, closed, reason = pos["tp"], True, "止盈(入场当根)"
                        else:
                            if hi >= pos["sl"]:
                                close_px, closed, reason = pos["sl"], True, "止损(入场当根)"
                            elif lo <= pos["tp"]:
                                close_px, closed, reason = pos["tp"], True, "止盈(入场当根)"
                        if closed:
                            d = (close_px - entry) if s["signal"] == "BUY" else (entry - close_px)
                            p = d * lots * pv - SPREAD * lots * pv
                            equity += p
                            trades.append({**pos, "close_price": close_px, "pnl": round(p, 2),
                                           "exit": reason, "close_time": nxt_t})
                            if p < 0:
                                cooldown_until["m15_rsi"] = nxt_t + pd.Timedelta(hours=cool_h)
                        else:
                            open_pos.append(pos)
                        # 额外的最小开仓间隔 (用于测试"降低交易频率能否救回点差成本")
                        if min_gap_min:
                            gap_until = nxt_t + pd.Timedelta(minutes=min_gap_min)
                            if cooldown_until.get("m15_rsi") is None or gap_until > cooldown_until["m15_rsi"]:
                                cooldown_until["m15_rsi"] = gap_until

            curve.append(equity)
    finally:
        sig.time = orig_time

    if collect is not None:
        collect.extend(trades)
    return metrics(trades, curve)


def metrics(trades, curve) -> dict:
    if not trades:
        return {"n": 0, "win_rate": 0, "net": 0, "avg_R": 0, "total_R": 0, "pf": 0,
                "max_dd": 0, "avg_hold": 0, "avg_sl": 0, "by_exit": {}}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = dd = 0.0
    for e in curve:
        peak = max(peak, e)
        dd = min(dd, e - peak)
    rs = [t["pnl"] / (t["sl_dist"] * t["lots"] * 100) for t in trades
          if t["sl_dist"] * t["lots"] * 100 > 0]
    by_exit = {}
    for t in trades:
        by_exit[t["exit"]] = by_exit.get(t["exit"], 0) + 1
    cost = sum(SPREAD * t["lots"] * 100 for t in trades)
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "net": sum(pnls),
        "gross": sum(pnls) + cost,          # 未扣点差的毛利
        "cost": cost,
        "total_R": sum(rs),
        "avg_R": sum(rs) / len(rs) if rs else 0.0,
        "pf": (sum(wins) / abs(sum(losses))) if losses and sum(losses) else float("inf"),
        "max_dd": dd,
        "avg_hold": sum((t["close_time"] - t["entry_time"]).total_seconds() / 3600
                        for t in trades) / len(trades),
        "avg_sl": sum(t["sl_dist"] for t in trades) / len(trades),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "by_exit": by_exit,
    }


def main():
    print("载入 M15 / M5 ...")
    m15 = load(M15_CSV)
    m5 = load(M5_CSV, start=m15.index[0], end=m15.index[-1])
    print(f"  M15: {len(m15)} 根  {m15.index[0]} -> {m15.index[-1]}")
    print(f"  M5 : {len(m5)} 根  {m5.index[0]} -> {m5.index[-1]}")
    m15 = prepare_indicators(m15)
    m5 = prepare_indicators(m5)
    m5_index = m5.index.values
    print(f"  手数{config.MIN_LOT_SIZE}~{config.MAX_LOT_SIZE}, 每笔风险${config.RISK_PER_TRADE}, "
          f"点差${SPREAD}, 最大持仓{config.MAX_POSITIONS}")

    print()
    print("=" * 100)
    print("  M15 RSI 策略: 止损口径对比 (2024-01-01 → 2025-01-14, 约12.5个月)")
    print("=" * 100)
    print(f"{'口径':<26}{'笔数':>7}{'胜率%':>8}{'净盈亏$':>11}{'毛利$':>10}{'点差成本$':>11}"
          f"{'每笔R':>9}{'盈亏比':>8}{'最大回撤$':>11}{'平均止损$':>10}")
    print("-" * 100)
    res = {}
    for name, mode in (("旧: 止损恒为$10", "old"), ("新: 2.5×ATR夹[3,10]", "fix")):
        m = run(m15, m5_index, m5, mode)
        res[mode] = m
        print(f"{name:<26}{m['n']:>7}{m['win_rate']:>8.1f}{m['net']:>+11.1f}{m['gross']:>+10.1f}"
              f"{-m['cost']:>11.1f}{m['avg_R']:>+9.3f}{m['pf']:>8.2f}{m['max_dd']:>11.1f}"
              f"{m['avg_sl']:>10.2f}")

    print()
    print("  平均盈利 / 平均亏损 (旧 / 新): "
          f"${res['old']['avg_win']:+.2f}/${res['old']['avg_loss']:+.2f}  "
          f"vs  ${res['fix']['avg_win']:+.2f}/${res['fix']['avg_loss']:+.2f}")

    print()
    print("  出场方式分布 (旧 / 新):")
    keys = sorted(set(res["old"]["by_exit"]) | set(res["fix"]["by_exit"]))
    for k in keys:
        print(f"    {k:<16} {res['old']['by_exit'].get(k, 0):>6} / {res['fix']['by_exit'].get(k, 0):>6}")

    # 分季度看稳健性
    print()
    print("  分季度每笔R (旧 / 新):")
    q = m15.index.to_period("Q")
    for period in sorted(set(q)):
        seg = m15[q == period]
        if len(seg) < WARMUP + 50:
            continue
        a = run(seg, m5_index, m5, "old")
        b = run(seg, m5_index, m5, "fix")
        print(f"    {period}  M15 {len(seg):>6}根   旧 {a['avg_R']:>+7.3f} ({a['n']:>3}笔)   "
              f"新 {b['avg_R']:>+7.3f} ({b['n']:>3}笔)")

    # 敏感度: 止损宽度 × 最小开仓间隔
    print()
    print("=" * 100)
    print("  敏感度测试: 止损宽度 × 最小开仓间隔 (全期 2024-01 → 2025-01)")
    print("=" * 100)
    print(f"{'止损口径':<24}{'最小间隔':>10}{'笔数':>8}{'胜率%':>8}{'毛利$':>10}"
          f"{'点差成本$':>11}{'净盈亏$':>11}{'每笔R':>9}")
    print("-" * 100)
    for mode_name, mode in (("旧: 恒$10", "old"),
                            ("新: 2.5×ATR夹[3,10]", "fix"),
                            ("宽: 2.5×ATR夹[5,20]", "wide")):
        for gap in (0, 60, 180):
            m = run(m15, m5_index, m5, mode, min_gap_min=gap)
            print(f"{mode_name:<24}{gap:>8}分{m['n']:>8}{m['win_rate']:>8.1f}{m['gross']:>+10.1f}"
                  f"{-m['cost']:>11.1f}{m['net']:>+11.1f}{m['avg_R']:>+9.3f}")


if __name__ == "__main__":
    main()

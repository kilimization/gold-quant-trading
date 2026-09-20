"""
M15 RSI 亏损归因诊断
================================================================================
回答两个具体问题:
  假设A "在趋势过程中不断下单, 然后被止损"   → 看"重复进场簇"里第2/3笔是否明显更差
  假设B "止盈设置太低"                      → 看止盈到底触发了多少次, MFE离止盈有多远

运行:  python research/diagnose_m15rsi.py
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from strategies.signals import prepare_indicators
from research.backtest_m15rsi import load, run, M15_CSV, M5_CSV


def seg_excursion(m15, t):
    """成交期间的最大有利/不利波动 (美元)"""
    try:
        w = m15.loc[t["entry_time"]:t["close_time"]]
    except Exception:
        return np.nan, np.nan
    if len(w) == 0:
        return np.nan, np.nan
    if t["direction"] == "BUY":
        return float(w["High"].max() - t["entry"]), float(t["entry"] - w["Low"].min())
    return float(t["entry"] - w["Low"].min()), float(w["High"].max() - t["entry"])


def show(title, df, col="pnl"):
    print(f"\n{title}")
    print(f"  {'分组':<26}{'笔数':>7}{'胜率%':>8}{'总盈亏$':>11}{'平均$':>9}")
    for k, g in df.groupby("grp", sort=True):
        wr = (g[col] > 0).mean() * 100
        print(f"  {str(k):<26}{len(g):>7}{wr:>8.1f}{g[col].sum():>+11.1f}{g[col].mean():>+9.2f}")


def main():
    print("载入 M15 / M5 ...")
    m15 = load(M15_CSV)
    m5 = load(M5_CSV, start=m15.index[0], end=m15.index[-1])
    m15i = prepare_indicators(m15)
    m5 = prepare_indicators(m5)
    print(f"  M15 {len(m15)} 根  {m15.index[0]} -> {m15.index[-1]}")

    trades = []
    m = run(m15i, m5.index.values, m5, "fix", collect=trades)
    df = pd.DataFrame(trades)
    df["entry_time"] = pd.to_datetime(df["entry_time"])
    df["close_time"] = pd.to_datetime(df["close_time"])
    df["hold_min"] = (df["close_time"] - df["entry_time"]).dt.total_seconds() / 60
    print(f"\n总样本: {len(df)} 笔, 净盈亏 {df['pnl'].sum():+.1f}, "
          f"毛利 {df['pnl'].sum() + (0.23 * df['lots'] * 100).sum():+.1f}")

    # ── 1. 出场方式 ──
    print("\n" + "=" * 78)
    print("1) 钱是从哪个出口漏掉的? (出场方式 × 盈亏)")
    print("=" * 78)
    g = df.groupby("exit").agg(笔数=("pnl", "size"), 总盈亏=("pnl", "sum"),
                               平均=("pnl", "mean"), 胜率=("pnl", lambda s: (s > 0).mean() * 100))
    g = g.sort_values("总盈亏")
    for k, r in g.iterrows():
        print(f"  {k:<14}{int(r['笔数']):>7}{r['胜率']:>8.1f}%{r['总盈亏']:>+12.1f}{r['平均']:>+9.2f}")

    # ── 2. 假设B: 止盈是否太低 ──
    print("\n" + "=" * 78)
    print("2) 假设B检验: 止盈是不是太低?")
    print("=" * 78)
    hit_tp = df["exit"].str.contains("止盈").sum()
    hit_sl = df["exit"].str.contains("止损").sum()
    print(f"  触及止盈: {hit_tp} 笔 ({hit_tp/len(df)*100:.1f}%)    "
          f"触及止损: {hit_sl} 笔 ({hit_sl/len(df)*100:.1f}%)")
    ex = df.apply(lambda t: seg_excursion(m15, t), axis=1, result_type="expand")
    df["mfe"], df["mae"] = ex[0], ex[1]
    df["tp_dist"] = df["sl_dist"] * 2          # 线上 tp = 2×SL
    df["mfe_vs_tp"] = df["mfe"] / df["tp_dist"]
    print(f"  止盈距离(2×SL) 平均 ${df['tp_dist'].mean():.2f}")
    print(f"  成交期间最大有利波动MFE: 中位 ${df['mfe'].median():.2f}  "
          f"P75 ${df['mfe'].quantile(.75):.2f}  P90 ${df['mfe'].quantile(.90):.2f}")
    print(f"  → MFE 达到止盈的比例: {(df['mfe_vs_tp'] >= 1).mean()*100:.1f}%")
    print(f"  平均盈利 ${df.loc[df['pnl']>0,'pnl'].mean():+.2f} vs 平均亏损 "
          f"${df.loc[df['pnl']<=0,'pnl'].mean():+.2f}  "
          f"(设计盈亏比 1:2, 实际 {abs(df.loc[df['pnl']>0,'pnl'].mean()/df.loc[df['pnl']<=0,'pnl'].mean()):.2f}:1)")

    # ── 3. 假设A: 趋势中反复进场 ──
    print("\n" + "=" * 78)
    print("3) 假设A检验: 是不是在趋势里反复下单然后被止损?")
    print("=" * 78)
    d = df.sort_values("entry_time").reset_index(drop=True)
    clusters, seqs = [], []
    cur_id, cur_seq = -1, 0
    prev_t, prev_dir = None, None
    for _, t in d.iterrows():
        if prev_t is None or t["direction"] != prev_dir or \
           (t["entry_time"] - prev_t).total_seconds() > 3 * 3600:
            cur_id += 1
            cur_seq = 1
        else:
            cur_seq += 1
        clusters.append(cur_id)
        seqs.append(cur_seq)
        prev_t, prev_dir = t["entry_time"], t["direction"]
    d["cluster"], d["seq"] = clusters, seqs
    d["grp"] = d["seq"].clip(upper=4).map({1: "簇内第1笔", 2: "簇内第2笔",
                                           3: "簇内第3笔", 4: "簇内第4笔+"})
    show("  同一方向、间隔<3小时视为一簇 (检验'追着趋势反复下单')", d)

    # ── 4. 方向 / ADX / 趋势一致性 ──
    print("\n" + "=" * 78)
    print("4) 其他可能的结构性原因")
    print("=" * 78)
    d2 = d.copy()
    d2["grp"] = d2["direction"]
    show("  按方向", d2)

    adx = m15i["ADX"].reindex(d["entry_time"], method="ffill").values
    d2["adx"] = adx
    d2["grp"] = pd.cut(d2["adx"], [0, 15, 20, 25, 100],
                       labels=["ADX<15(很震荡)", "ADX15-20", "ADX20-25", "ADX>=25"]).astype(str)
    show("  按入场时ADX (过滤线是25)", d2)

    ema = m15i["EMA100"].reindex(d["entry_time"], method="ffill").values
    d2["with_trend"] = np.where(
        (d["direction"].values == "BUY") == (d["entry"].values > ema),
        "顺EMA100方向", "逆EMA100方向")
    d2["grp"] = d2["with_trend"]
    show("  按是否顺M15 EMA100", d2)

    d2["grp"] = d["hold_min"].clip(upper=180).astype(int) // 30 * 30
    d2["grp"] = d2["grp"].map(lambda x: f"持仓{x}-{x+30}分钟")
    show("  按持仓时长", d2)

    # ── 5. 若把止盈/止损换个位置会怎样 (同一批信号) ──
    print("\n" + "=" * 78)
    print("5) 反事实: 同一批信号, 只改出场参数 (用MFE/MAE近似)")
    print("=" * 78)
    print(f"  {'假设出场':<34}{'会先触发的比例':>16}")
    for sl_mult in (0.5, 1.0, 2.0):
        for tp_mult in (0.5, 1.0, 2.0):
            sl_d = df["sl_dist"] * sl_mult
            tp_d = df["sl_dist"] * tp_mult * 2
            first_tp = (df["mfe"] >= tp_d)
            first_sl = (df["mae"] >= sl_d)
            # 保守: 同根两者都中算止损
            win = (first_tp & ~first_sl).mean() * 100
            print(f"  止损{sl_mult}× 止盈{tp_mult}×2×SL  ->  止盈先触发 {win:>5.1f}%")


if __name__ == "__main__":
    main()

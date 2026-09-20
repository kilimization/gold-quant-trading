"""
A/B: 远端仓库(8846c92) 的 m15_rsi 设计 vs 你本地现在的 m15_rsi 设计
================================================================================
两者同名但其实是不同策略。关键差异:

  维度            远端(8846c92)                    本地(现在)
  趋势对齐        BUY需 close>SMA50, SELL需 close<SMA50   无
  RSI触发         只看当前bar RSI2<15 / >85          前一根或当前 RSI2<15 / >85
  M5共振确认      无                                需要M5强实体
  ADX过滤         无                                要求 ADX<25
  RSI离场阈值     RSI2>55(多) / <45(空)              RSI2>85(多) / <20(空)
  止损            min(_calc_atr_stop,20) → [10,20]   2.5×ATR 夹 [3,10]
  冷却            无                                20分钟

用同一段数据(2024-01→2025-01)逐项拆开测, 看是哪一项造成了差异。

运行:  python research/ab_remote_vs_local.py
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from strategies.signals import prepare_indicators
from research.backtest_m15rsi import load, run, M15_CSV, M5_CSV

m15 = load(M15_CSV)
m5 = load(M5_CSV, start=m15.index[0], end=m15.index[-1])
m15i = prepare_indicators(m15)
m5 = prepare_indicators(m5)
idx = m5.index.values
SL = 0.23

COLS = f"{'方案':<40}{'笔数':>7}{'胜率%':>8}{'毛利$':>10}{'成本$':>9}{'净盈亏$':>11}{'每笔R':>9}{'回撤$':>10}"


def row(name, m):
    print(f"{name:<40}{m['n']:>7}{m['win_rate']:>8.1f}{m['gross']:>+10.1f}"
          f"{-m['cost']:>9.1f}{m['net']:>+11.1f}{m['avg_R']:>+9.3f}{m['max_dd']:>10.1f}")


print("=" * 104)
print("  A/B 对比: 远端(8846c92)设计 vs 本地(现在)设计   [2024-01 → 2025-01, 无额外间隔限制]")
print("=" * 106)
print(COLS)
print("-" * 106)

# 本地现状 (基准): 无止盈对齐, 离场85/20, 20分钟冷却(已内置), 无额外间隔
base = run(m15i, idx, m5, "fix")
row("本地现状 (离场85/20, 无趋势对齐)", base)

# 只加趋势对齐 (远端设计最关键的一条)
tf = run(m15i, idx, m5, "fix", trend_filter=True)
row("+ 只加趋势对齐 (BUY>SMA50 / SELL<SMA50)", tf)

# 只改离场阈值为远端口径 55/45
ex = run(m15i, idx, m5, "fix", exit_hi=55.0, exit_lo=45.0)
row("+ 只用远端离场阈值 55/45", ex)

# 两者都加 = 近似远端设计
both = run(m15i, idx, m5, "fix", trend_filter=True, exit_hi=55.0, exit_lo=45.0)
row("+ 两者都加 (≈远端设计)", both)

print()
print("=" * 106)
print("  叠加最小开仓间隔(180分钟)后")
print("=" * 106)
print(COLS)
print("-" * 106)
for name, kw in (("本地现状 + 180分间隔", {}),
                 ("趋势对齐 + 180分间隔", dict(trend_filter=True)),
                 ("趋势对齐 + 远端离场 + 180分", dict(trend_filter=True, exit_hi=55.0, exit_lo=45.0))):
    row(name, run(m15i, idx, m5, "fix", min_gap_min=180, **kw))

print()
print("=" * 106)
print("  按方向拆解 (趋势对齐应该主要救回空头)")
print("=" * 106)
for name, kw in (("本地现状", {}), ("加趋势对齐", dict(trend_filter=True))):
    tr = []
    run(m15i, idx, m5, "fix", collect=tr, **kw)
    d = pd.DataFrame(tr)
    for direc in ("BUY", "SELL"):
        s = d[d["direction"] == direc]
        if not len(s):
            continue
        cost = (SL * s["lots"] * 100).sum()
        print(f"  {name:<12}{direc:<5}{len(s):>6}笔  胜率{(s['pnl']>0).mean()*100:>5.1f}%  "
              f"毛利{s['pnl'].sum()+cost:>+8.1f}  净{s['pnl'].sum():>+8.1f}")

"""M15 RSI: 时间止损 & 方向 & 频率 的组合测试 (基于回测发现的持仓时长悬崖)"""
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

print("=" * 96)
print("  时间止损 (持仓上限) × 最小开仓间隔  —— 全期 2024-01 → 2025-01")
print("  背景: 逐笔分析显示 持仓30-90分钟胜率75-78%, 一旦超过120分钟胜率崩到20-46%")
print("=" * 96)
print(f"{'持仓上限':<14}{'最小间隔':>9}{'笔数':>8}{'胜率%':>8}{'毛利$':>10}"
      f"{'点差成本$':>11}{'净盈亏$':>11}{'每笔R':>9}{'最大回撤$':>11}")
print("-" * 96)
for hold in (4, 6, 8, 12):
    for gap in (0, 180):
        m = run(m15i, idx, m5, "fix", min_gap_min=gap, hold_bars=hold)
        label = f"{hold*15}分钟({hold}根)"
        print(f"{label:<14}{gap:>7}分{m['n']:>8}{m['win_rate']:>8.1f}{m['gross']:>+10.1f}"
              f"{-m['cost']:>11.1f}{m['net']:>+11.1f}{m['avg_R']:>+9.3f}{m['max_dd']:>11.1f}")

print()
print("=" * 96)
print("  只做多 vs 只做空 (逐笔分析显示 空头 -$661 / 多头 +$360)")
print("=" * 96)
print(f"{'组合':<34}{'笔数':>8}{'胜率%':>8}{'毛利$':>10}{'点差成本$':>11}{'净盈亏$':>11}")
print("-" * 96)
best = dict(mode="fix", min_gap_min=180, hold_bars=6)
for name, kw in (("全部单 (对照)", {}),
                 ("只做多", {}), ("只做空", {})):
    pass
full = run(m15i, idx, m5, **best)
print(f"{'当前最优组合 (6根/180分)':<34}{full['n']:>8}{full['win_rate']:>8.1f}"
      f"{full['gross']:>+10.1f}{-full['cost']:>11.1f}{full['net']:>+11.1f}")

# 分方向统计 (复用 collect)
trades = []
run(m15i, idx, m5, collect=trades, **best)
df = pd.DataFrame(trades)
for d in ("BUY", "SELL"):
    s = df[df["direction"] == d]
    if len(s) == 0:
        continue
    gross = s["pnl"].sum() + (0.23 * s["lots"] * 100).sum()
    print(f"{'  仅' + ('多头' if d == 'BUY' else '空头') + ' (估算)':<34}{len(s):>8}"
          f"{(s['pnl']>0).mean()*100:>8.1f}{gross:>+10.1f}{-(0.23*s['lots']*100).sum():>11.1f}"
          f"{s['pnl'].sum():>+11.1f}")

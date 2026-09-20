"""
临时分析: 用真实成交记录 + MT4真实K线, 反推趋势型策略的止盈止损是否过高
(分析完可删)
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import statistics as st

BRIDGE = Path(r"C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal\AB75DD8A03E8CC693E1336EB0D50BA2D\MQL4\Files\DWX")
LOG = Path("data/gold_trade_log.json")


def load_bars(fn):
    data = json.loads((BRIDGE / fn).read_text(encoding="utf-8"))
    bars = []
    for b in data["bars"]:
        t = datetime.strptime(b["t"], "%Y.%m.%d %H:%M:%S")
        bars.append({"t": t, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
    return bars


trades = [r for r in json.loads(LOG.read_text(encoding="utf-8")) if r.get("action") == "OPEN"]
print(f"交易日志中的OPEN记录: {len(trades)} 条")
from collections import Counter
print("按策略:", Counter(t["strategy"] for t in trades))

h1 = load_bars("bars_h1.json")
m15 = load_bars("bars_m15.json")
print(f"H1 K线: {len(h1)} 根, 覆盖 {h1[0]['t']} → {h1[-1]['t']} (服务器时间)")
print(f"M15 K线: {len(m15)} 根, 覆盖 {m15[0]['t']} → {m15[-1]['t']}")

# --- 校准时区偏移: 日志用本地时间(新加坡UTC+8), K线用服务器时间 ---
# 逐个候选偏移, 看入场价落在哪根K线的高低区间内
def best_offset(bars, subset):
    best = None
    for off_h in range(-16, 17):
        hit = 0
        for tr in subset:
            try:
                lt = datetime.fromisoformat(tr["time"])
            except Exception:
                continue
            target = lt - timedelta(hours=off_h)
            for b in bars:
                if b["t"] <= target <= b["t"] + timedelta(hours=1) and b["l"] <= tr["price"] <= b["h"]:
                    hit += 1
                    break
        if best is None or hit > best[1]:
            best = (off_h, hit)
    return best


trend = [t for t in trades if t["strategy"] in ("keltner", "macd", "orb")]
print(f"\n趋势型(keltner/macd/orb) OPEN: {len(trend)} 条")
if trend:
    off_h, hit = best_offset(h1, trend)
    print(f"最佳时区偏移: 日志时间 - {off_h}h = 服务器时间 (匹配 {hit}/{len(trend)} 笔)")

print("\n" + "=" * 78)
print("逐笔: 入场后最长24根H1内的最大有利/不利波动 (美元/盎司)")
print("=" * 78)
print(f"{'时间':<17}{'策略':<9}{'方向':<5}{'设定SL':>7}{'设定TP':>7}{'MFE':>7}{'MAE':>7}{'MFE/TP':>8}{'MAE/SL':>8}")

rows = []
for tr in trend:
    try:
        lt = datetime.fromisoformat(tr["time"])
    except Exception:
        continue
    target = lt - timedelta(hours=off_h)
    idx = None
    for i, b in enumerate(h1):
        if b["t"] <= target <= b["t"] + timedelta(hours=1):
            idx = i
            break
    if idx is None or idx + 1 >= len(h1):
        continue
    fwd = h1[idx + 1: idx + 25]
    if not fwd:
        continue
    sl = tr.get("sl_pips") or 0
    tp = tr.get("tp_pips") or 0
    if tr["direction"] == "BUY":
        mfe = max(b["h"] for b in fwd) - tr["price"]
        mae = tr["price"] - min(b["l"] for b in fwd)
    else:
        mfe = tr["price"] - min(b["l"] for b in fwd)
        mae = max(b["h"] for b in fwd) - tr["price"]
    rows.append((tr, sl, tp, mfe, mae))
    print(f"{lt:%m-%d %H:%M}    {tr['strategy']:<9}{tr['direction']:<5}{sl:>7.1f}{tp:>7.1f}"
          f"{mfe:>7.1f}{mae:>7.1f}{(mfe/tp if tp else 0):>8.2f}{(mae/sl if sl else 0):>8.2f}")

if rows:
    print("\n" + "=" * 78)
    print("统计汇总 (只含能对齐到K线的成交)")
    print("=" * 78)
    mfes = sorted(r[3] for r in rows)
    maes = sorted(r[4] for r in rows)

    def pct(v, p):
        if not v:
            return 0
        k = min(len(v) - 1, int(round((len(v) - 1) * p)))
        return v[k]

    print(f"样本数: {len(rows)}")
    for label, arr in (("MFE(最大有利)", mfes), ("MAE(最大不利)", maes)):
        print(f"  {label}: 中位 {st.median(arr):.1f}  P75 {pct(arr,0.75):.1f}  "
              f"P90 {pct(arr,0.90):.1f}  最大 {max(arr):.1f}")

    tps = [r[2] for r in rows if r[2] > 0]
    sls = [r[1] for r in rows if r[1] > 0]
    if tps:
        print(f"\n  设定的TP: 中位 {st.median(tps):.1f}  范围 {min(tps):.1f}~{max(tps):.1f}")
        reach = sum(1 for r in rows if r[2] > 0 and r[3] >= r[2])
        print(f"  → 实际触及TP的只有 {reach}/{len([r for r in rows if r[2]>0])} 笔 "
              f"({reach/len([r for r in rows if r[2]>0])*100:.0f}%)")
    if sls:
        print(f"  设定的SL: 中位 {st.median(sls):.1f}  范围 {min(sls):.1f}~{max(sls):.1f}")
        hit = sum(1 for r in rows if r[1] > 0 and r[4] >= r[1])
        print(f"  → 实际触及SL的有 {hit}/{len([r for r in rows if r[1]>0])} 笔")

    print("\n  若把TP收紧到下列水平, 可触及比例:")
    for mult in (0.5, 0.75, 1.0, 1.5):
        base = st.median(tps) if tps else 0
        if base:
            lvl = base * mult
            k = sum(1 for r in rows if r[3] >= lvl)
            print(f"    TP={lvl:>5.1f} (即当前中位的{mult:>4.1f}×): {k}/{len(rows)} 笔能触及 "
                  f"({k/len(rows)*100:.0f}%)")

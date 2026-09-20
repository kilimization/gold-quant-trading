"""验算 WhaleFlow_EA.mq4 的解析逻辑。

在 Python 里一比一复刻 EA 里的 FindObjectRange / NthObject / JNum / JStr，
对真实的 whale_flow.json 跑一遍，确认 EA 取到的字段和值是它期望的。
（MT4 的 Strategy Tester 在本机无法登录/无历史数据，故用这种方式替代运行验证。）

建议复制到 gold-quant-trading/research/ 下长期保留。
"""

import json
import re
from pathlib import Path

JSON_PATH = Path(
    r"C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal"
    r"\AB75DD8A03E8CC693E1336EB0D50BA2D\MQL4\Files\DWX\whale_flow.json"
)


# ---------- 以下函数与 MQL4 源码逐行对应 ----------
def find_object_range(s: str, key: str, start: int = 0):
    """对应 FindObjectRange(): 返回 { } 内部的子串"""
    pat = f'"{key}":{{'
    p = s.find(pat, start)
    if p < 0:
        return None
    i = p + len(pat)
    depth = 1
    begin = i
    while i < len(s):
        ch = s[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[begin:i]
        i += 1
    return None


def find_array(s: str, key: str, start: int = 0) -> int:
    """对应 FindArray(): 返回 '[' 的下标"""
    pat = f'"{key}":['
    p = s.find(pat, start)
    return -1 if p < 0 else p + len(pat) - 1


def nth_object(s: str, arr_pos: int, index: int):
    """对应 NthObject(): 取数组里第 index 个 { } 对象的内容"""
    i = arr_pos + 1
    depth = 0
    found = 0
    begin = -1
    while i < len(s):
        ch = s[i]
        if ch == "{":
            if depth == 0:
                begin = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin >= 0:
                if found == index:
                    return s[begin + 1:i]
                found += 1
                begin = -1
        elif ch == "]" and depth == 0:
            return None
        i += 1
    return None


def jnum(s: str, key: str, default: float = 0.0, start: int = 0) -> float:
    """对应 JNum()"""
    pat = f'"{key}":'
    p = s.find(pat, start)
    if p < 0:
        return default
    i = p + len(pat)
    buf = ""
    while i < len(s):
        ch = s[i]
        if ch in "-+.eE" or ch.isdigit():
            buf += ch
            i += 1
        else:
            break
    return float(buf) if buf else default


def jstr(s: str, key: str, default: str = "", start: int = 0) -> str:
    """对应 JStr()"""
    pat = f'"{key}":"'
    p = s.find(pat, start)
    if p < 0:
        return default
    i = p + len(pat)
    q = s.find('"', i)
    return default if q < 0 else s[i:q]


def symbol_matches(json_symbol: str, chart_symbol: str = "BTCUSD") -> bool:
    """对应 SymbolMatches()"""
    clean = lambda x: re.sub(r"[.#_\-]", "", x.upper())
    return clean(json_symbol) in clean(chart_symbol)


def main() -> int:
    raw = JSON_PATH.read_text(encoding="utf-8")
    print(f"文件: {JSON_PATH.name}  长度 {len(raw)} 字节\n")

    failures = []
    truth = json.loads(raw)

    crypto_arr = find_array(raw, "crypto")
    if crypto_arr < 0:
        print("!! FindArray('crypto') 失败")
        return 1

    idx = 0
    matched = False
    while idx < 20:
        obj = nth_object(raw, crypto_arr, idx)
        if obj is None:
            break
        sym = jstr(obj, "symbol")
        if not symbol_matches(sym):
            idx += 1
            continue
        matched = True

        print(f"命中图表品种: {sym}")
        print(f"  last_price      = {jnum(obj, 'last_price')}")
        print(f"  window_minutes  = {jnum(obj, 'window_minutes')}")
        print(f"  trades_used     = {jnum(obj, 'trades_used')}")

        f2 = find_object_range(obj, "flow_2h")
        smart = find_object_range(obj, "smart")
        top = find_object_range(obj, "top_account")
        retail = find_object_range(obj, "retail")
        oi = find_object_range(obj, "oi")
        fund = find_object_range(obj, "funding")

        checks = {
            "flow_2h.net_usd": (jnum(f2, "net_usd") if f2 else None),
            "flow_2h.ratio": (jnum(f2, "ratio") if f2 else None),
            "smart.long_pct": (jnum(smart, "long_pct") if smart else None),
            "smart.ratio": (jnum(smart, "ratio") if smart else None),
            "top_account.ratio": (jnum(top, "ratio") if top else None),
            "retail.ratio": (jnum(retail, "ratio") if retail else None),
            "oi.oi_usd": (jnum(oi, "oi_usd") if oi else None),
            "oi.oi_change_pct": (jnum(oi, "oi_change_pct") if oi else None),
            "funding.mark_price": (jnum(fund, "mark_price") if fund else None),
            "funding.funding_rate_pct": (jnum(fund, "funding_rate_pct") if fund else None),
        }
        for k, v in checks.items():
            print(f"  {k:26s} = {v}")
            if v is None:
                failures.append(f"{k} 解析为 None")

        t = next(c for c in truth["crypto"] if c["symbol"] == sym)
        if abs(checks["flow_2h.net_usd"] - t["flow_2h"]["net_usd"]) > 1:
            failures.append("flow_2h.net_usd 与真实值不一致(可能误取 zones 里的 net_usd)")
        if abs(checks["smart.ratio"] - t["smart"]["ratio"]) > 1e-9:
            failures.append("smart.ratio 与真实值不一致")
        if abs(checks["top_account.ratio"] - t["top_account"]["ratio"]) > 1e-9:
            failures.append("top_account.ratio 与真实值不一致")

        z_arr = find_array(obj, "zones")
        zones = []
        while z_arr >= 0:
            zo = nth_object(obj, z_arr, len(zones))
            if zo is None:
                break
            zones.append((jstr(zo, "kind"), jnum(zo, "low"), jnum(zo, "high"),
                          jnum(zo, "net_usd"), jnum(zo, "strength")))
        print(f"  zones 解析出 {len(zones)} 个 (真实 {len(t['zones'])} 个)")
        for z in zones[:3]:
            print(f"     {z[0]:10s} {z[1]}~{z[2]} 净{z[3]:+,.0f} 强度{int(z[4])}")
        if len(zones) != len(t["zones"]):
            failures.append("zones 数量与真实值不一致")

        w_arr = find_array(obj, "whales")
        whales = []
        while w_arr >= 0:
            wo = nth_object(obj, w_arr, len(whales))
            if wo is None:
                break
            whales.append((jnum(wo, "ts"), jnum(wo, "price"), jnum(wo, "usd"),
                           jstr(wo, "side")))
        print(f"  whales 解析出 {len(whales)} 笔 (真实 {len(t['whales'])} 笔)")
        if len(whales) != len(t["whales"]):
            failures.append("whales 数量与真实值不一致")
        for w in whales[:3]:
            print(f"     ts={int(w[0])} price={w[1]} usd={w[2]:,.0f} side={w[3]}")
        break

    if not matched:
        print("!! 没有任何 crypto 品种与图表品种 BTCUSD 匹配")
        failures.append("品种匹配失败")

    gold_pos = raw.find('"gold":{')
    cot = find_object_range(raw, "cot", gold_pos) if gold_pos > 0 else None
    gld = find_object_range(raw, "gld", gold_pos) if gold_pos > 0 else None
    print("\ngold 段:")
    if cot:
        print(f"  cot: date={jstr(cot, 'report_date')} net={jnum(cot, 'net'):,.0f} "
              f"chg={jnum(cot, 'net_change'):,.0f} index={jnum(cot, 'cot_index')} "
              f"weeks={jnum(cot, 'history_weeks'):.0f} extreme={jstr(cot, 'extreme')}")
        truth_gold = truth["gold"]["cot"]
        if abs(jnum(cot, "net") - truth_gold["net"]) > 1:
            failures.append("cot.net 与真实值不一致")
    else:
        failures.append("gold.cot 解析失败")
    if gld:
        print(f"  gld: last={jnum(gld, 'last')} chg%={jnum(gld, 'chg_pct')} "
              f"volratio={jnum(gld, 'volume_vs_avg20')}")
    else:
        failures.append("gold.gld 解析失败")
    print(f"  ref_price={jnum(raw, 'ref_price', 0, gold_pos)}")

    print("\n" + "=" * 60)
    if failures:
        print("发现问题:")
        for f in failures:
            print("  -", f)
        return 2
    print("解析层校验通过: EA 取到的字段/数量与 JSON 真实值完全一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

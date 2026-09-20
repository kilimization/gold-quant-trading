#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whale_health.py — 巨鲸/聪明钱链路体检 (采集器 -> JSON -> EA)

检查这条链路的每一环，任何一环断了都会明确告诉你断在哪里：

  [1] whale_flow.json 是否存在、是否新鲜(未过期)
  [2] JSON 结构/字段是否完整(schema、字段、数量)
  [3] 数值是否离谱(价格>0、买卖额一致、strength 在 0-100)
  [4] MT4 侧是否真的读到了(EA 会写 whale_parse_result.txt / whale_probe_heartbeat.txt)
  [5] 画图所需的关键字段是否齐全(没有这些 EA 画不出东西)

用法:
    python whale_health.py            # 体检一次
    python whale_health.py --watch    # 每 60 秒体检一次
退出码: 0=全绿(链路通)  1=有问题
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

MT4_DATA = Path(
    r"C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal"
    r"\AB75DD8A03E8CC693E1336EB0D50BA2D"
)
MQL4_FILES = MT4_DATA / "MQL4" / "Files"
FEED = MQL4_FILES / "DWX" / "whale_flow.json"
EA_RESULT = MQL4_FILES / "whale_parse_result.txt"          # Test_WhaleParse 脚本产出
EA_HEARTBEAT = MQL4_FILES / "whale_probe_heartbeat.txt"    # Probe EA 产出

GREEN, RED, YELLOW, GRAY, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[90m", "\033[0m"
if sys.platform != "win32":  # 非 Windows 直接关掉颜色
    GREEN = RED = YELLOW = GRAY = RESET = ""
# Windows 老控制台可能不支持 ANSI，用 colorama 关掉更稳妥
if sys.platform == "win32":
    try:
        import os
        os.system("")  # 打开 VT 处理
    except Exception:
        GREEN = RED = YELLOW = GRAY = RESET = ""


class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []

    def ok(self, msg: str) -> None:
        print(f"  {GREEN}[OK]{RESET}   {msg}")

    def fail(self, msg: str) -> None:
        print(f"  {RED}[FAIL]{RESET} {msg}")
        self.fails.append(msg)

    def warn(self, msg: str) -> None:
        print(f"  {YELLOW}[WARN]{RESET} {msg}")
        self.warns.append(msg)

    def info(self, msg: str) -> None:
        print(f"  {GRAY}[--]{RESET}   {msg}")


def check_feed(rep: Report) -> dict | None:
    print("\n[1] 数据文件")
    if not FEED.exists():
        rep.fail(f"{FEED} 不存在 —— 采集器没跑过或路径不对")
        return None

    age = time.time() - FEED.stat().st_mtime
    size = FEED.stat().st_size
    rep.info(f"路径: {FEED}")
    if age <= 180:
        rep.ok(f"新鲜度: {age:.0f} 秒前更新 ({size} 字节)")
    elif age <= 900:
        rep.warn(f"新鲜度: {age:.0f} 秒前更新 —— 采集器可能卡住或间隔被调大")
    else:
        rep.fail(f"新鲜度: {age:.0f} 秒前更新 —— 数据已过期，检查采集器是否在运行")

    try:
        data = json.loads(FEED.read_text(encoding="utf-8"))
    except Exception as e:
        rep.fail(f"JSON 解析失败: {e}")
        return None
    rep.ok("JSON 可解析")
    return data


def check_schema(rep: Report, data: dict) -> None:
    print("\n[2] 结构与字段")
    for key in ("schema", "generated_at", "stale_after_sec", "crypto", "gold", "meta"):
        if key in data:
            rep.ok(f"顶层字段 {key} 存在")
        else:
            rep.fail(f"顶层字段 {key} 缺失")

    crypto = data.get("crypto") or []
    if not crypto:
        rep.fail("crypto 数组为空 —— EA 在 BTC/ETH 图上没有东西可画")
        return
    rep.ok(f"crypto 品种数: {len(crypto)} ({', '.join(c.get('symbol', '?') for c in crypto)})")

    for c in crypto:
        sym = c.get("symbol", "?")
        need = ["last_price", "zones", "whales", "flow_2h", "smart", "top_account",
                "retail", "oi", "funding"]
        missing = [k for k in need if k not in c]
        if missing:
            rep.fail(f"{sym} 缺字段: {', '.join(missing)}")
        else:
            rep.ok(f"{sym} 字段齐全 (zones={len(c['zones'])} whales={len(c['whales'])})")
        for sub in ("flow_2h", "smart", "top_account", "retail", "oi", "funding"):
            if not isinstance(c.get(sub), dict) or not c.get(sub):
                rep.warn(f"{sym}.{sub} 为空 —— 面板对应行会显示 0")

    gold = data.get("gold") or {}
    if gold.get("cot"):
        rep.ok(f"gold.cot 有数据 (报告日 {gold['cot'].get('report_date')})")
    else:
        rep.fail("gold.cot 为空 —— 黄金面板的 COT 行会空着")
    if gold.get("ref_price"):
        rep.ok(f"gold.ref_price = {gold['ref_price']}")
    else:
        rep.warn("gold.ref_price 为空 (Yahoo GC=F 可能抓取失败)")


def check_values(rep: Report, data: dict) -> None:
    print("\n[3] 数值合理性")
    for c in data.get("crypto", []):
        sym = c.get("symbol", "?")
        lp = float(c.get("last_price") or 0)
        if lp <= 0:
            rep.fail(f"{sym}.last_price = {lp} —— 抓取失败(EA 换算价位会出错)")
        else:
            rep.ok(f"{sym}.last_price = {lp}")

        f2 = c.get("flow_2h") or {}
        buy, sell, net = (float(f2.get(k) or 0) for k in ("buy_usd", "sell_usd", "net_usd"))
        if abs((buy - sell) - net) <= max(1.0, abs(net) * 0.001):
            rep.ok(f"{sym} 买卖净额自洽 (买{buy:,.0f} - 卖{sell:,.0f} = 净{net:,.0f})")
        else:
            rep.fail(f"{sym} 买卖净额不自洽: 买{buy:,.0f} 卖{sell:,.0f} 净{net:,.0f}")

        for i, z in enumerate(c.get("zones", [])):
            st = float(z.get("strength") or 0)
            lo, hi = float(z.get("low") or 0), float(z.get("high") or 0)
            if not (0 <= st <= 100):
                rep.fail(f"{sym}.zones[{i}].strength={st} 超出 0-100")
            if hi <= lo:
                rep.fail(f"{sym}.zones[{i}] 价格区间异常: {lo}~{hi}")
        if c.get("zones"):
            rep.ok(f"{sym} {len(c['zones'])} 个价位区区间/强度合理")
        else:
            rep.warn(f"{sym} 窗口内没有价位区(成交额未达门槛) —— 图上不会画价位带")

        whales = c.get("whales", [])
        bad_side = [w for w in whales if w.get("side") not in ("buy", "sell")]
        if bad_side:
            rep.fail(f"{sym} 有 {len(bad_side)} 笔大单 side 非法")
        if whales:
            biggest = max(float(w.get("usd") or 0) for w in whales)
            rep.ok(f"{sym} 巨鲸大单 {len(whales)} 笔, 最大一笔 ${biggest:,.0f}")


def check_ea_side(rep: Report) -> None:
    print("\n[4] MT4 侧(EA/脚本)是否读到数据")
    saw_any = False

    if EA_HEARTBEAT.exists():
        saw_any = True
        age = time.time() - EA_HEARTBEAT.stat().st_mtime
        txt = EA_HEARTBEAT.read_text(encoding="utf-8", errors="replace").strip()
        if age <= 300:
            rep.ok(f"Probe 心跳新鲜({age:.0f} 秒前): {txt}")
        else:
            rep.warn(f"Probe 心跳已过期({age/60:.1f} 分钟前): {txt}")
    if EA_RESULT.exists():
        saw_any = True
        age = time.time() - EA_RESULT.stat().st_mtime
        txt = EA_RESULT.read_text(encoding="utf-8", errors="replace")
        if "FAIL" in txt:
            rep.fail(f"EA 自检报告失败({age/60:.1f} 分钟前): {txt[:200]}")
        else:
            rep.ok(f"EA 自检报告存在({age/60:.1f} 分钟前), 读取 {len(txt)} 字符")

    # EA 自身不落盘，但它会在专家日志里留痕 —— 这是最直接的"EA 正在跑且读到了"证据
    # 注意: MT4 的专家日志编码随系统而变(本机是 GBK/mbcs，不是 UTF-8)，必须逐个试
    def read_log(path: Path) -> str:
        for enc in ("utf-16", "mbcs", "gbk", "utf-8"):
            try:
                return path.read_text(encoding=enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return path.read_bytes().decode("utf-8", errors="replace")

    marks = []
    log_dir = MT4_DATA / "MQL4" / "Logs"
    for lf in sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]:
        text = read_log(lf)
        for line in text.splitlines():
            if "WhaleFlow" not in line:
                continue
            if any(k in line for k in ("WhaleFlow 启动", "巨鲸大单", "无法打开", "解析失败", "读取:")):
                marks.append((lf, line.strip()))
    if marks:
        saw_any = True
        last_log, last_line = marks[-1]
        fails = [m for m in marks if ("无法打开" in m[1] or "解析失败" in m[1])]
        alerts = [m for m in marks if "巨鲸大单" in m[1]]
        if fails:
            rep.fail(f"EA 日志里有读文件/解析错误: {fails[-1][1][:160]}")
        else:
            rep.ok(f"EA 在 MT4 里正常运行({last_log.name}): {last_line[:150]}")
        if alerts:
            rep.ok(f"已触发 {len(alerts)} 次巨鲸大单提醒，最近一次: {alerts[-1][1][-70:]}")

    if not saw_any:
        rep.warn("还没看到 MT4 侧产物 —— 需要你做一次: 把 WhaleFlow_EA 挂到图表上, "
                 "或把 Test_WhaleParse 脚本拖到图表上跑一次(它会写 whale_parse_result.txt)")


def check_chart_ready(rep: Report, data: dict) -> None:
    print("\n[5] 画图所需关键字段(EA 逻辑依赖)")
    for c in data.get("crypto", []):
        sym = c.get("symbol", "?")
        checks = {
            "zone: low/high/net/strength/kind":
                all(all(k in z for k in ("low", "high", "net_usd", "strength", "kind"))
                    for z in c.get("zones", [])),
            "whale: ts/price/usd/side":
                all(all(k in w for k in ("ts", "price", "usd", "side"))
                    for w in c.get("whales", [])),
            "flow_2h: net_usd/ratio": all(k in (c.get("flow_2h") or {}) for k in ("net_usd", "ratio")),
            "smart: long_pct/ratio": all(k in (c.get("smart") or {}) for k in ("long_pct", "ratio")),
            "funding: mark_price(用于消除基差)": "mark_price" in (c.get("funding") or {}),
        }
        for name, ok in checks.items():
            (rep.ok if ok else rep.fail)(f"{sym} {name}")


def run_once() -> int:
    print("=" * 66)
    print(f"巨鲸/聪明钱链路体检  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print("=" * 66)
    rep = Report()
    data = check_feed(rep)
    if data:
        check_schema(rep, data)
        check_values(rep, data)
        check_chart_ready(rep, data)
    check_ea_side(rep)

    print("\n" + "=" * 66)
    if rep.fails:
        print(f"{RED}结论: 链路有问题 —— {len(rep.fails)} 项失败, {len(rep.warns)} 项警告{RESET}")
        for f in rep.fails:
            print(f"   - {f}")
        return 1
    if rep.warns:
        print(f"{YELLOW}结论: 链路通, 但有 {len(rep.warns)} 项提醒{RESET}")
        for w in rep.warns:
            print(f"   - {w}")
        return 0
    print(f"{GREEN}结论: 全绿 —— 采集器、JSON、EA 读取端都正常{RESET}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="循环体检(默认 60 秒一次)")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    if not args.watch:
        return run_once()
    while True:
        run_once()
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""whale_collector.py — 巨鲸 / 聪明钱数据采集侧车 (写入 MT4 文件桥)

架构
----
MT4 的 EA 无法联网，所以由本脚本抓取外部数据，落地成 JSON 文件，
EA(WhaleFlow_EA.mq4) 每 N 秒读一次文件并在图表上标注。

    互联网数据源 -> whale_collector.py -> MQL4\\Files\\DWX\\whale_flow.json -> WhaleFlow_EA

数据源 (全部免费公开接口，无需 key)
-----------------------------------
1) 加密(BTC/ETH)：Binance USDT-M 合约公开接口
   - /fapi/v1/aggTrades                 逐笔聚合成交 -> 筛出大单(巨鲸)，按价格桶聚合成 吸筹/派发区
   - /futures/data/topLongShortPositionRatio  顶级交易者(按持仓)多空比 = "聪明钱"
   - /futures/data/topLongShortAccountRatio   顶级交易者(按账户)多空比
   - /futures/data/globalLongShortAccountRatio 全市场账户多空比 = "散户"
   - /futures/data/openInterestHist      持仓量变化
   - /fapi/v1/premiumIndex               资金费率 + 标记价
2) 黄金(XAUUSD)：CFTC Commitments of Traders (每周五公布)
   - fut_disagg_txt_<year>.zip          管理基金(M_Money)净持仓 + COT 指数(拥挤度)
   - Yahoo Finance GLD                  黄金 ETF 资金流(份额×净值估算)

用法
----
    python whale_collector.py                 # 常驻，每 60 秒刷新一次
    python whale_collector.py --once          # 只跑一轮(调试用)
    python whale_collector.py --once --print  # 跑一轮并打印摘要
    python whale_collector.py --interval 30   # 自定义轮询秒数

注意
----
* 价格取自 Binance/现货与 CFTC；EA 画图时按【当前图表价格】做线性缩放平移，
  以消除券商报价(BTCUSD CFD)与交易所报价之间的基差。
* 本脚本只做数据采集与标注，不参与下单。
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:  # pragma: no cover
    print("需要 requests: python -m pip install requests")
    raise

# ============================================================
# 配置
# ============================================================
BASE_DIR = Path(__file__).resolve().parent

# MT4 数据文件夹(与 gold-quant-trading/config.py 保持一致)
MT4_DATA_DIR = Path(
    r"C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal\AB75DD8A03E8CC693E1336EB0D50BA2D"
)
BRIDGE_DIR = MT4_DATA_DIR / "MQL4" / "Files" / "DWX"      # EA 读取目录
OUT_FILE = BRIDGE_DIR / "whale_flow.json"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "whale_collector.log"

UA = {"User-Agent": "Mozilla/5.0 (compatible; whale_collector/1.0)"}
HTTP_TIMEOUT = 20

# 加密品种配置：Binance 合约符号 / 输出品种名 / 大单门槛(USD) / 价格桶步长(USD)
CRYPTO_SYMBOLS = [
    {"binance": "BTCUSDT", "name": "BTCUSD", "whale_usd": 500_000,
     "zone_usd": 400_000, "bin_step": 100.0, "oi_symbol": "BTCUSDT"},
    {"binance": "ETHUSDT", "name": "ETHUSD", "whale_usd": 200_000,
     "zone_usd": 150_000, "bin_step": 5.0, "oi_symbol": "ETHUSDT"},
]

AGG_TRADES_LIMIT = 1000          # Binance 单次最多 1000 条聚合成交
AGG_TRADES_PAGES = 8             # 翻页次数(每页1000条)，用来凑够 TRADE_WINDOW_MINUTES
TRADE_WINDOW_MINUTES = 120       # 只用最近 N 分钟的成交做吸筹/派发区
MAX_WHALE_EVENTS = 40            # 最多输出多少条巨鲸成交事件(按金额取前 N)
MAX_ZONES = 6                    # 每个品种最多输出多少个价位区
ZONE_MIN_NET_USD = 1_000_000     # 一个价位区的最小净额门槛，过滤噪音

COT_URL_TMPL = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
GLD_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/GLD?range=3mo&interval=1d"
GC_YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/GC=F?range=5d&interval=1d"

OUT_SCHEMA = 1
STALE_AFTER_SEC = 900            # EA 判定"数据过期"的秒数(15分钟)

log = logging.getLogger("whale")


# ============================================================
# 工具
# ============================================================
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_session() -> "requests.Session":
    """带重试的会话：本机到 fapi.binance.com 偶发 SSL EOF，需要自动重试。"""
    s = requests.Session()
    s.headers.update(UA)
    try:
        from requests.adapters import HTTPAdapter
        try:
            from urllib3.util.retry import Retry
        except ImportError:  # urllib3 v1 路径
            from requests.packages.urllib3.util.retry import Retry
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    except Exception as e:  # pragma: no cover
        log.warning(f"重试适配器初始化失败(不影响主流程): {e}")
    return s


SESSION = _build_session()


def http_get(url: str, **kw) -> requests.Response:
    kw.setdefault("timeout", HTTP_TIMEOUT)
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            r = SESSION.get(url, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            last_exc = e
            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ============================================================
# 加密：Binance 数据抓取
# ============================================================
def fetch_agg_trades(symbol: str) -> List[Dict[str, Any]]:
    """翻页抓取聚合成交，凑够 TRADE_WINDOW_MINUTES 分钟的数据。

    Binance 单次最多 1000 条，行情活跃时 1000 条只覆盖十几分钟，
    因此用 endTime 往前翻页，最多 AGG_TRADES_PAGES 页。
    """
    url = "https://fapi.binance.com/fapi/v1/aggTrades"
    cutoff_ms = int(time.time() * 1000) - TRADE_WINDOW_MINUTES * 60_000
    out: List[Dict[str, Any]] = []
    end_time: Optional[int] = None
    seen: set = set()

    for _ in range(AGG_TRADES_PAGES):
        params = {"symbol": symbol, "limit": AGG_TRADES_LIMIT}
        if end_time is not None:
            params["endTime"] = end_time
        try:
            rows = http_get(url, params=params).json()
        except Exception as e:
            log.warning(f"aggTrades {symbol} 抓取失败: {e}")
            break
        if not isinstance(rows, list) or not rows:
            break

        new_rows = 0
        for r in rows:
            key = r.get("a")
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            new_rows += 1

        oldest = min(int(r["T"]) for r in rows if "T" in r)
        if new_rows == 0 or oldest <= cutoff_ms:
            break
        end_time = oldest - 1

    out.sort(key=lambda r: int(r.get("T", 0)))
    return out


def fetch_top_position_ratio(symbol: str, period: str = "5m") -> Dict[str, Any]:
    url = (f"https://fapi.binance.com/futures/data/topLongShortPositionRatio"
           f"?symbol={symbol}&period={period}&limit=2")
    try:
        rows = http_get(url).json()
        latest = rows[-1] if rows else {}
        prev = rows[-2] if len(rows) > 1 else {}
        return {
            "long_pct": round(safe_float(latest.get("longAccount")) * 100, 1),
            "short_pct": round(safe_float(latest.get("shortAccount")) * 100, 1),
            "ratio": round(safe_float(latest.get("longShortRatio")), 2),
            "prev_ratio": round(safe_float(prev.get("longShortRatio")), 2),
        }
    except Exception as e:
        log.warning(f"topLongShortPositionRatio {symbol} 失败: {e}")
        return {}


def fetch_top_account_ratio(symbol: str, period: str = "5m") -> Dict[str, Any]:
    url = (f"https://fapi.binance.com/futures/data/topLongShortAccountRatio"
           f"?symbol={symbol}&period={period}&limit=2")
    try:
        rows = http_get(url).json()
        latest = rows[-1] if rows else {}
        return {"ratio": round(safe_float(latest.get("longShortRatio")), 2)}
    except Exception as e:
        log.warning(f"topLongShortAccountRatio {symbol} 失败: {e}")
        return {}


def fetch_global_account_ratio(symbol: str, period: str = "5m") -> Dict[str, Any]:
    url = (f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
           f"?symbol={symbol}&period={period}&limit=2")
    try:
        rows = http_get(url).json()
        latest = rows[-1] if rows else {}
        return {"ratio": round(safe_float(latest.get("longShortRatio")), 2)}
    except Exception as e:
        log.warning(f"globalLongShortAccountRatio {symbol} 失败: {e}")
        return {}


def fetch_open_interest_hist(symbol: str, period: str = "1h", limit: int = 25) -> Dict[str, Any]:
    url = (f"https://fapi.binance.com/futures/data/openInterestHist"
           f"?symbol={symbol}&period={period}&limit={limit}")
    try:
        rows = http_get(url).json()
        if not rows:
            return {}
        first = safe_float(rows[0].get("sumOpenInterestValue"))
        last = safe_float(rows[-1].get("sumOpenInterestValue"))
        chg = ((last - first) / first * 100.0) if first > 0 else 0.0
        return {
            "oi_usd": round(last),
            "oi_change_pct": round(chg, 2),
            "lookback_hours": len(rows),
        }
    except Exception as e:
        log.warning(f"openInterestHist {symbol} 失败: {e}")
        return {}


def fetch_premium_index(symbol: str) -> Dict[str, Any]:
    url = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}"
    for attempt in range(3):
        try:
            d = http_get(url).json()
            return {
                "mark_price": round(safe_float(d.get("markPrice")), 4),
                "funding_rate_pct": round(safe_float(d.get("lastFundingRate")) * 100, 4),
            }
        except Exception as e:
            if attempt == 2:
                log.warning(f"premiumIndex {symbol} 失败: {e}")
            else:
                time.sleep(0.5)
    return {}


# ============================================================
# 加密：大单 -> 巨鲸事件 / 价位区
# ============================================================
def build_crypto_block(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """抓取单个加密品种，输出 zones / whales / smart / oi / funding."""
    sym_b = cfg["binance"]
    sym_out = cfg["name"]
    result: Dict[str, Any] = {"symbol": sym_out, "source": f"binance:{sym_b}"}

    trades = fetch_agg_trades(sym_b)
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - TRADE_WINDOW_MINUTES * 60_000

    agg_buy = 0.0
    agg_sell = 0.0
    whales: List[Dict[str, Any]] = []
    bins: Dict[float, Dict[str, float]] = {}

    step = float(cfg["bin_step"])
    for t in trades:
        try:
            price = safe_float(t["p"])
            qty = safe_float(t["q"])
            ts = int(t["T"])
            is_buyer_maker = bool(t["m"])   # m=True -> 主动卖单砸盘
        except (KeyError, TypeError, ValueError):
            continue
        usd = price * qty
        side = "sell" if is_buyer_maker else "buy"

        if ts >= cutoff_ms:
            if side == "buy":
                agg_buy += usd
            else:
                agg_sell += usd
            # 价位分桶(只统计窗口内的成交)
            bucket = round(price / step) * step
            b = bins.setdefault(bucket, {"buy": 0.0, "sell": 0.0, "n": 0.0})
            b[side] += usd
            b["n"] += 1

        if usd >= cfg["whale_usd"]:
            whales.append({
                "ts": int(ts / 1000),
                "price": round(price, 2),
                "usd": round(usd),
                "qty": qty,
                "side": side,
            })

    whales.sort(key=lambda w: w["usd"], reverse=True)
    whales = whales[:MAX_WHALE_EVENTS]
    whales.sort(key=lambda w: w["ts"])

    # 价位区：按桶聚合成 吸筹(buy占优) / 派发(sell占优)
    zones: List[Dict[str, Any]] = []
    for price, b in bins.items():
        net = b["buy"] - b["sell"]
        if abs(net) < ZONE_MIN_NET_USD:
            continue
        zones.append({
            "price": round(price, 2),
            "low": round(price - step / 2, 2),
            "high": round(price + step / 2, 2),
            "net_usd": round(net),
            "buy_usd": round(b["buy"]),
            "sell_usd": round(b["sell"]),
            "trades": int(b["n"]),
            "kind": "accumulate" if net > 0 else "distribute",
        })
    zones.sort(key=lambda z: abs(z["net_usd"]), reverse=True)
    zones = zones[:MAX_ZONES]
    for z in zones:  # 强度 0-100，用于图上标注
        z["strength"] = min(100, int(abs(z["net_usd"]) / max(ZONE_MIN_NET_USD, 1) * 8))

    last_price = safe_float(trades[-1]["p"]) if trades else 0.0
    oldest_ts = int(trades[0]["T"] / 1000) if trades else 0
    result.update({
        "last_price": round(last_price, 2),
        "zones": zones,
        "whales": whales,
        "window_minutes": (int((now_ms / 1000 - oldest_ts) / 60) if oldest_ts else 0),
        "trades_used": len(trades),
        "flow_2h": {
            "buy_usd": round(agg_buy),
            "sell_usd": round(agg_sell),
            "net_usd": round(agg_buy - agg_sell),
            "ratio": round(agg_buy / agg_sell, 2) if agg_sell > 0 else 0.0,
        },
        "smart": fetch_top_position_ratio(sym_b),
        "top_account": fetch_top_account_ratio(sym_b),
        "retail": fetch_global_account_ratio(sym_b),
        "oi": fetch_open_interest_hist(cfg["oi_symbol"]),
        "funding": fetch_premium_index(sym_b),
    })
    return result


# ============================================================
# 黄金：CFTC COT + GLD 资金流
# ============================================================
def fetch_cot_gold(year: Optional[int] = None) -> Dict[str, Any]:
    """CFTC 持仓报告：管理基金(M_Money)净持仓 + COT 指数(0-100 拥挤度)."""
    year = year or now_utc().year
    url = COT_URL_TMPL.format(year=year)
    try:
        raw = http_get(url, timeout=60).content
    except Exception as e:
        log.warning(f"COT {year} 下载失败: {e}")
        return {}

    if raw[:2] != b"PK":
        log.warning(f"COT {year} 返回内容不是 zip")
        return {}

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        text = zf.read(zf.namelist()[0]).decode("latin-1")
    except Exception as e:
        log.warning(f"COT {year} 解压失败: {e}")
        return {}

    import csv as _csv
    rows = list(_csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        return {}
    header = rows[0]

    def col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    c_name = col("Market_and_Exchange_Names")
    c_date = col("Report_Date_as_YYYY-MM-DD")
    c_oi = col("Open_Interest_All")
    c_lng = col("M_Money_Positions_Long_All")
    c_sht = col("M_Money_Positions_Short_All")
    if min(c_name, c_date, c_oi, c_lng, c_sht) < 0:
        log.warning("COT 字段缺失，跳过")
        return {}

    series: List[Tuple[str, float, float, float, float]] = []  # (date, net, oi, long, short)
    for row in rows[1:]:
        if len(row) <= max(c_name, c_date, c_oi, c_lng, c_sht):
            continue
        name = row[c_name].upper()
        # 只要标准黄金合约，排除 micro / E-mini 等
        if "GOLD" not in name or "MICRO" in name or "E-MINI" in name:
            continue
        lng = safe_float(row[c_lng])
        sht = safe_float(row[c_sht])
        series.append((row[c_date], lng - sht, safe_float(row[c_oi]), lng, sht))

    if not series:
        log.warning("COT 里没找到黄金记录")
        return {}

    series.sort(key=lambda x: x[0])
    date, net, oi, lng, sht = series[-1]
    prev_net = series[-2][1] if len(series) > 1 else net
    nets = [s[1] for s in series]
    lo, hi = min(nets), max(nets)
    cot_index = ((net - lo) / (hi - lo) * 100.0) if hi > lo else 50.0

    # 极值判定：指数 >90 多头极度拥挤；<10 空头极度拥挤
    if cot_index >= 90:
        extreme = "crowded_long"
    elif cot_index <= 10:
        extreme = "crowded_short"
    else:
        extreme = "neutral"

    # 报告新鲜度：CFTC 每周五公布截至周二的数据
    try:
        age_days = (now_utc().date() - datetime.strptime(date, "%Y-%m-%d").date()).days
    except ValueError:
        age_days = -1

    return {
        "report_date": date,
        "age_days": age_days,
        "open_interest": int(oi),
        "mm_long": int(lng),
        "mm_short": int(sht),
        "net": int(net),
        "net_prev": int(prev_net),
        "net_change": int(net - prev_net),
        "cot_index": round(cot_index, 1),
        "history_weeks": len(series),
        "range_low": int(lo),
        "range_high": int(hi),
        "extreme": extreme,
    }


def fetch_gld_flow() -> Dict[str, Any]:
    """GLD 资金流近似值：用当日涨跌 × 规模估算资金方向(免费口径，非精确份额申赎)."""
    try:
        d = http_get(GLD_YAHOO).json()
        res = d["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        vols = [v for v in res["indicators"]["quote"][0]["volume"] if v is not None]
        if len(closes) < 2:
            return {}
        chg_pct = (closes[-1] - closes[-2]) / closes[-2] * 100.0
        avg_vol = sum(vols[-20:]) / max(len(vols[-20:]), 1)
        # 用成交量相对均值的放大倍数作为"资金关注度"
        vol_ratio = (vols[-1] / avg_vol) if avg_vol > 0 else 1.0
        return {
            "last": round(closes[-1], 2),
            "chg_pct": round(chg_pct, 2),
            "volume": int(vols[-1]),
            "volume_vs_avg20": round(vol_ratio, 2),
            "up_days_5": sum(1 for i in range(-5, 0) if len(closes) > abs(i) and closes[i] > closes[i - 1]),
        }
    except Exception as e:
        log.warning(f"GLD 抓取失败: {e}")
    return {}


def fetch_gold_price() -> float:
    try:
        d = http_get(GC_YAHOO).json()
        res = d["chart"]["result"][0]
        return round(safe_float(res["meta"].get("regularMarketPrice")), 2)
    except Exception as e:
        log.warning(f"GC=F 抓取失败: {e}")
        return 0.0


# ============================================================
# 汇总输出
# ============================================================
def build_payload(last_good: Dict[str, Any]) -> Dict[str, Any]:
    started = time.time()
    crypto: List[Dict[str, Any]] = []
    for cfg in CRYPTO_SYMBOLS:
        try:
            block = build_crypto_block(cfg)
        except Exception as e:
            log.error(f"{cfg['name']} 采集异常: {e}")
            block = {}
        # 抓取失败(没有成交数据)时，沿用上一次的好数据，避免图上出现 0 值
        if not block or safe_float(block.get("last_price")) <= 0:
            prev = last_good.get(cfg["name"])
            if prev:
                block = dict(prev)
                block["stale"] = True
                log.warning(f"{cfg['name']} 本轮抓取失败，沿用上一次数据")
            elif block:
                block["stale"] = True
        else:
            block["stale"] = False
            last_good[cfg["name"]] = block
        if block:
            crypto.append(block)
        time.sleep(0.4)          # 轻微错峰，避免触发交易所限流

    cot = fetch_cot_gold()
    if not cot:
        cot = last_good.get("gold_cot") or {}
        if cot:
            log.warning("COT 本轮抓取失败，沿用上一次数据")
    else:
        last_good["gold_cot"] = cot

    gold_price = fetch_gold_price()
    gld = fetch_gld_flow()
    payload = {
        "schema": OUT_SCHEMA,
        "generated_at": iso(now_utc()),
        "generated_at_local": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stale_after_sec": STALE_AFTER_SEC,
        "crypto": crypto,
        "gold": {
            "cot": cot,
            "gld": gld,
            "ref_price": gold_price,
        },
        "meta": {
            "elapsed_sec": round(time.time() - started, 2),
            "sources": ["binance-fapi", "cftc-cot", "yahoo-gld"],
        },
    }
    return payload


def write_payload(payload: Dict[str, Any]) -> None:
    BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_FILE)      # 原子替换：EA 永远读到完整文件


def summarize(payload: Dict[str, Any]) -> str:
    lines = [f"生成时间: {payload['generated_at_local']}"]
    for c in payload.get("crypto", []):
        flow = c.get("flow_2h", {})
        smart = c.get("smart", {})
        lines.append(
            f"  [{c['symbol']}] 价 {c.get('last_price')} | 2h净流入 {flow.get('net_usd', 0):+,} USD "
            f"(买卖比 {flow.get('ratio')}) | 顶级持仓多空 {smart.get('ratio')} "
            f"({smart.get('long_pct')}%多) | 大单 {len(c.get('whales', []))} 笔 | 价位区 {len(c.get('zones', []))} 个"
        )
        for z in c.get("zones", [])[:3]:
            lines.append(
                f"      {z['kind']:10s} {z['low']}~{z['high']}  净 {z['net_usd']:+,} USD  强度{z['strength']}"
            )
    g = payload.get("gold", {})
    cot = g.get("cot") or {}
    if cot:
        lines.append(
            f"  [XAUUSD] COT {cot.get('report_date')} 管理基金净持仓 {cot.get('net'):+,} 手 "
            f"(周变化 {cot.get('net_change'):+,}) COT指数 {cot.get('cot_index')} -> {cot.get('extreme')}"
        )
    gld = g.get("gld") or {}
    if gld:
        lines.append(f"  [GLD] {gld.get('last')} ({gld.get('chg_pct'):+}%) 量比 {gld.get('volume_vs_avg20')}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="巨鲸/聪明钱数据采集 -> MT4 文件桥")
    ap.add_argument("--once", action="store_true", help="只采集一轮后退出")
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔(秒)，默认 60")
    ap.add_argument("--print", dest="do_print", action="store_true", help="打印摘要")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE, encoding="utf-8")],
    )
    log.info(f"巨鲸/聪明钱采集启动 | 输出: {OUT_FILE} | 间隔 {args.interval}s")

    last_good: Dict[str, Any] = {}
    while True:
        try:
            payload = build_payload(last_good)
            write_payload(payload)
            summary = summarize(payload)
            if args.do_print or args.once:
                print(summary)
            log.info("已刷新 whale_flow.json | " + summary.replace("\n", "\n    "))
        except Exception as e:
            log.error(f"采集轮次失败: {e}", exc_info=True)

        if args.once:
            return 0
        time.sleep(max(10, args.interval))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已停止")

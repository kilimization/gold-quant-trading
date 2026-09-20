"""
黄金多时间框架量化交易信号引擎 v3
====================================
v3 核心升级 (基于GitHub backtrader-pullback-window-xauusd项目研究):
1. 4阶段状态机入场 — SCANNING→ARMED→WINDOW_OPEN→ENTRY
   不再"突破就入场"，改为等待回撤确认后再入场
2. EMA100趋势过滤 — 只在大趋势方向上交易
3. ADX趋势强度过滤 — ADX>25才允许趋势策略开仓
4. ATR自适应止损 — 2.5×ATR (基于backtrader项目实测参数)
5. 做空条件放宽 — 用ADX+EMA100过滤

策略组合:
1. H1 Keltner通道突破 (主力) + 状态机 + ADX + EMA100
2. H1 MACD+SMA50趋势 (补充) + ADX + EMA100
3. M15 RSI均值回归 (低风险补充)
"""
import logging
import numpy as np
import pandas as pd
from typing import Dict, Optional, List
from datetime import datetime

import config

from .risk import calc_dynamic_levels, server_time_str

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# 技术指标计算
# ═══════════════════════════════════════════════════════════════

def calc_rsi(series: pd.Series, period: int = 2) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算ADX (平均趋向指标)"""
    high, low, close = df['High'], df['Low'], df['Close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = pd.Series(0.0, index=df.index)
    minus_dm = pd.Series(0.0, index=df.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move
    atr = tr.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di))
    return dx.ewm(alpha=1/period, min_periods=period).mean()


def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算所有技术指标"""
    df = df.copy()
    df['SMA50'] = df['Close'].rolling(50).mean()
    df['EMA100'] = df['Close'].ewm(span=100).mean()   # v3新增: 趋势过滤
    df['EMA9'] = df['Close'].ewm(span=9).mean()
    df['EMA12'] = df['Close'].ewm(span=12).mean()
    df['EMA21'] = df['Close'].ewm(span=21).mean()
    df['EMA26'] = df['Close'].ewm(span=26).mean()
    df['ATR'] = (df['High'] - df['Low']).rolling(14).mean()
    df['KC_mid'] = df['Close'].ewm(span=20).mean()
    df['KC_upper'] = df['KC_mid'] + 1.5 * df['ATR']
    df['KC_lower'] = df['KC_mid'] - 1.5 * df['ATR']
    df['MACD'] = df['EMA12'] - df['EMA26']
    df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
    df['MACD_hist'] = df['MACD'] - df['MACD_signal']
    df['RSI2'] = calc_rsi(df['Close'], 2)
    df['RSI14'] = calc_rsi(df['Close'], 14)
    df['ADX'] = calc_adx(df, 14)
    return df


# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════
ADX_TREND_THRESHOLD = 24    # 回测最优: Sharpe 0.60 (25→0.52, 23→0.45)
ATR_SL_MULTIPLIER = 2.5     # v3: 改为2.5×ATR (backtrader项目实测值)
ATR_SL_MIN = 10
ATR_SL_MAX = 50
ATR_TP_MULTIPLIER = 3.0     # v5: 止盈3.0×ATR (6.5×太远, MFE P90才$17, 几乎永远不触发)


def _calc_atr_stop(df: pd.DataFrame) -> float:
    atr = float(df.iloc[-1]['ATR'])
    if pd.isna(atr) or atr <= 0:
        return 20
    sl = round(atr * ATR_SL_MULTIPLIER, 2)
    return max(ATR_SL_MIN, min(ATR_SL_MAX, sl))


def _calc_atr_tp(df: pd.DataFrame) -> float:
    atr = float(df.iloc[-1]['ATR'])
    if pd.isna(atr) or atr <= 0:
        return 50
    return round(atr * ATR_TP_MULTIPLIER, 2)


# ═══════════════════════════════════════════════════════════════
# 4阶段状态机 (核心v3升级)
# ═══════════════════════════════════════════════════════════════

class KeltnerStateMachine:
    """
    Keltner突破的4阶段状态机入场系统
    基于 backtrader-pullback-window-xauusd 项目

    Phase 1 SCANNING:  检测到Keltner突破信号
    Phase 2 ARMED:     等待1-2根回撤K线确认
    Phase 3 WINDOW:    在回撤K线高低点设置突破窗口
    Phase 4 ENTRY:     价格突破窗口 → 真正入场

    如果窗口超时或反向突破 → 重置
    """

    # 状态常量
    SCANNING = "SCANNING"
    ARMED = "ARMED"
    WINDOW = "WINDOW"

    def __init__(self):
        self.state = self.SCANNING
        self.direction = None          # 'BUY' / 'SELL'
        self.pullback_count = 0
        self.window_top = None
        self.window_bottom = None
        self.window_bars_left = 0
        self.armed_bar_count = 0       # ARMED状态持续的bar数
        self.last_signal_reason = ""
        self._reset_count = 0

    def reset(self, reason: str = ""):
        """重置到扫描状态"""
        if self.state != self.SCANNING:
            log.debug(f"  [状态机] 重置: {self.state}→SCANNING ({reason})")
        self.state = self.SCANNING
        self.direction = None
        self.pullback_count = 0
        self.window_top = self.window_bottom = None
        self.window_bars_left = 0
        self.armed_bar_count = 0

    def update(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        每根新K线调用一次，返回入场信号或None

        Args:
            df: 包含指标的DataFrame (最新K线在最后)
        Returns:
            入场信号dict 或 None
        """
        if len(df) < 105:  # 需要EMA100有值
            return None

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        close = float(latest['Close'])
        open_ = float(latest['Open']) if 'Open' in latest else close
        high = float(latest['High'])
        low = float(latest['Low'])
        kc_upper = float(latest['KC_upper'])
        kc_lower = float(latest['KC_lower'])
        ema100 = float(latest['EMA100'])
        adx = float(latest['ADX'])
        atr = float(latest['ATR'])

        if any(pd.isna(v) for v in [kc_upper, kc_lower, ema100, adx, atr]):
            return None

        # ── Phase 1: SCANNING ──
        if self.state == self.SCANNING:
            return self._phase_scanning(close, kc_upper, kc_lower, ema100, adx)

        # ── Phase 2: ARMED (等待回撤) ──
        if self.state == self.ARMED:
            return self._phase_armed(close, open_, high, low, atr, df)

        # ── Phase 3: WINDOW (等待突破确认) ──
        if self.state == self.WINDOW:
            return self._phase_window(high, low, close, df)

        return None

    def _phase_scanning(self, close, kc_upper, kc_lower, ema100, adx):
        """Phase 1: 扫描信号"""
        # ADX过滤
        if adx < ADX_TREND_THRESHOLD:
            return None

        # 做多: 价格突破上轨 + 价格>EMA100 (大趋势过滤)
        if close > kc_upper and close > ema100:
            self.state = self.ARMED
            self.direction = 'BUY'
            self.pullback_count = 0
            self.armed_bar_count = 0
            self.last_signal_reason = f"Keltner做多: 价格{close:.2f} > 上轨{kc_upper:.2f} (ADX={adx:.1f})"
            log.info(f"  [状态机] SCANNING→ARMED(BUY): {self.last_signal_reason}")
            return None  # 不立即入场，等回撤

        # 做空: 价格跌破下轨 + 价格<EMA100
        if close < kc_lower and close < ema100:
            self.state = self.ARMED
            self.direction = 'SELL'
            self.pullback_count = 0
            self.armed_bar_count = 0
            self.last_signal_reason = f"Keltner做空: 价格{close:.2f} < 下轨{kc_lower:.2f} (ADX={adx:.1f})"
            log.info(f"  [状态机] SCANNING→ARMED(SELL): {self.last_signal_reason}")
            return None

        return None

    def _phase_armed(self, close, open_, high, low, atr, df):
        """Phase 2: 等待回撤K线"""
        self.armed_bar_count += 1

        # 超时保护: 最多等5根K线 (5小时H1 / 75分钟M15)
        if self.armed_bar_count > 5:
            self.reset("ARMED超时(5根K线)")
            return None

        # 判断是否为回撤K线
        is_pullback = False
        if self.direction == 'BUY':
            is_pullback = close < open_  # 红色K线 = 回撤
        elif self.direction == 'SELL':
            is_pullback = close > open_  # 绿色K线 = 回撤

        if is_pullback:
            self.pullback_count += 1
            log.info(f"  [状态机] 回撤K线 #{self.pullback_count} (H={high:.2f} L={low:.2f})")

            # 1根回撤即可进入窗口 (H1时间框架不能等太久)
            if self.pullback_count >= 1:
                # 计算突破窗口
                candle_range = high - low
                offset = candle_range * 0.5

                self.window_top = high + offset
                self.window_bottom = low - offset
                self.window_bars_left = 3  # 窗口持续3根K线

                self.state = self.WINDOW
                log.info(f"  [状态机] ARMED→WINDOW: 窗口[{self.window_bottom:.2f}, {self.window_top:.2f}] 持续{self.window_bars_left}根K线")
                return None
        else:
            # 非回撤K线：如果是顺向强势K线，可能是有效突破，不重置
            # 但如果方向完全反转，重置
            if self.direction == 'BUY' and close < df.iloc[-3]['Low'] if len(df) >= 3 else False:
                self.reset("BUY信号失效(价格大幅回落)")
            elif self.direction == 'SELL' and close > df.iloc[-3]['High'] if len(df) >= 3 else False:
                self.reset("SELL信号失效(价格大幅反弹)")

        return None

    def _phase_window(self, high, low, close, df):
        """Phase 3: 等待突破确认"""
        self.window_bars_left -= 1

        # 与 check_keltner_signal 保持一致的动态档位 (状态机版本当前未被启用)
        _lv = calc_dynamic_levels(df, direction=self.direction, entry=close)
        sl, tp = _lv['sl'], _lv['tp']

        if self.direction == 'BUY':
            # 价格突破窗口上沿 → 入场
            if high >= self.window_top:
                signal = {
                    'strategy': 'keltner',
                    'signal': 'BUY',
                    'reason': f"✅ Keltner做多确认: 回撤后突破{self.window_top:.2f} ({self.last_signal_reason})",
                    'close': close,
                    'sl': sl,
                    'tp': tp,
                }
                log.info(f"  [状态机] WINDOW→ENTRY(BUY): 突破确认!")
                self.reset("入场完成")
                return signal

            # 价格跌破窗口下沿 → 失效
            if low <= self.window_bottom:
                log.info(f"  [状态机] WINDOW失效: 价格{low:.2f} < 下沿{self.window_bottom:.2f}")
                self.reset("窗口失效(反向突破)")
                return None

        elif self.direction == 'SELL':
            if low <= self.window_bottom:
                signal = {
                    'strategy': 'keltner',
                    'signal': 'SELL',
                    'reason': f"✅ Keltner做空确认: 回撤后突破{self.window_bottom:.2f} ({self.last_signal_reason})",
                    'close': close,
                    'sl': sl,
                    'tp': tp,
                }
                log.info(f"  [状态机] WINDOW→ENTRY(SELL): 突破确认!")
                self.reset("入场完成")
                return signal

            if high >= self.window_top:
                log.info(f"  [状态机] WINDOW失效: 价格{high:.2f} > 上沿{self.window_top:.2f}")
                self.reset("窗口失效(反向突破)")
                return None

        # 窗口超时
        if self.window_bars_left <= 0:
            log.info(f"  [状态机] WINDOW超时，重置")
            self.reset("窗口超时")

        return None

    def get_status(self) -> str:
        """返回当前状态描述（用于日志）"""
        if self.state == self.SCANNING:
            return "扫描中"
        elif self.state == self.ARMED:
            return f"等回撤({self.direction}, {self.pullback_count}根)"
        elif self.state == self.WINDOW:
            return f"窗口({self.direction}, 剩{self.window_bars_left}根, [{self.window_bottom:.0f}-{self.window_top:.0f}])"
        return self.state


# ═══════════════════════════════════════════════════════════════
# 全局状态机实例 (在模块级别保持状态)
# ═══════════════════════════════════════════════════════════════
_keltner_sm = KeltnerStateMachine()


def get_keltner_state_machine() -> KeltnerStateMachine:
    """获取全局状态机实例"""
    return _keltner_sm


# ═══════════════════════════════════════════════════════════════
# 信号检测函数
# ═══════════════════════════════════════════════════════════════

def check_keltner_signal(df: pd.DataFrame) -> Optional[Dict]:
    """
    Keltner通道突破信号 v4 — 简单版 + ADX过滤 + EMA100趋势过滤
    
    回测验证: 11年Sharpe 0.52, 特朗普2期Sharpe 1.47
    状态机版本回测后Sharpe仅0.52降到0.23，确认简单版更优
    """
    if len(df) < 105:
        return None
    
    latest = df.iloc[-1]
    close = float(latest['Close'])
    kc_upper = float(latest['KC_upper'])
    kc_lower = float(latest['KC_lower'])
    ema100 = float(latest['EMA100'])
    adx = float(latest['ADX'])
    
    if any(pd.isna(v) for v in [kc_upper, kc_lower, ema100, adx]):
        return None
    
    # ADX过滤: 趋势不够强不开仓
    if adx < ADX_TREND_THRESHOLD:
        return None
    
    # 做多: 突破上轨 + 价格>EMA100
    if close > kc_upper and close > ema100:
        # 动态档位: 止损放前N根K线低点外侧; 止盈取结构目标/ATR兜底
        lv = calc_dynamic_levels(df, direction='BUY', entry=close)
        return {
            'strategy': 'keltner',
            'signal': 'BUY',
            'reason': f"Keltner做多: 价格{close:.2f} > 上轨{kc_upper:.2f} (ADX={adx:.1f}) | {lv['reason']}",
            'close': close,
            'sl': lv['sl'],
            'tp': lv['tp'],
            'atr': lv['atr'],
            'regime': lv['regime'],
            'bar_time': df.index[-1],
        }
    
    # 做空: 跌破下轨 + 价格<EMA100
    if close < kc_lower and close < ema100:
        lv = calc_dynamic_levels(df, direction='SELL', entry=close)
        return {
            'strategy': 'keltner',
            'signal': 'SELL',
            'reason': f"Keltner做空: 价格{close:.2f} < 下轨{kc_lower:.2f} (ADX={adx:.1f}) | {lv['reason']}",
            'close': close,
            'sl': lv['sl'],
            'tp': lv['tp'],
            'atr': lv['atr'],
            'regime': lv['regime'],
            'bar_time': df.index[-1],
        }
    
    return None


def check_macd_signal(df: pd.DataFrame) -> Optional[Dict]:
    """
    MACD趋势信号 v3 — 增加EMA100趋势过滤
    """
    if len(df) < 105:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(latest['Close'])
    macd_hist = float(latest['MACD_hist'])
    macd_hist_prev = float(prev['MACD_hist'])
    ema100 = float(latest['EMA100'])
    adx = float(latest['ADX'])

    if any(pd.isna(v) for v in [macd_hist, macd_hist_prev, ema100, adx]):
        return None

    if adx < ADX_TREND_THRESHOLD:
        return None

    # 做多: MACD转正 + 价格>EMA100
    if macd_hist > 0 and macd_hist_prev <= 0 and close > ema100:
        lv = calc_dynamic_levels(df, direction='BUY', entry=close)
        return {
            'strategy': 'macd',
            'signal': 'BUY',
            'reason': f"MACD做多: 柱状图转正, 价格{close:.2f} > EMA100 (ADX={adx:.1f}) | {lv['reason']}",
            'close': close,
            'sl': lv['sl'],
            'tp': lv['tp'],
            'atr': lv['atr'],
            'regime': lv['regime'],
            'bar_time': df.index[-1],
        }

    # 做空: MACD转负 + 价格<EMA100
    if macd_hist < 0 and macd_hist_prev >= 0 and close < ema100:
        lv = calc_dynamic_levels(df, direction='SELL', entry=close)
        return {
            'strategy': 'macd',
            'signal': 'SELL',
            'reason': f"MACD做空: 柱状图转负, 价格{close:.2f} < EMA100 (ADX={adx:.1f}) | {lv['reason']}",
            'close': close,
            'sl': lv['sl'],
            'tp': lv['tp'],
            'atr': lv['atr'],
            'regime': lv['regime'],
            'bar_time': df.index[-1],
        }

    return None


def check_exit_signal(df: pd.DataFrame, strategy: str, direction: str) -> Optional[str]:
    """检查出场信号"""
    if len(df) < 5:
        return None
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(latest['Close'])

    if strategy == 'keltner':
        # v5优化: 去掉KC中轨出场 (回测验证: 中轨出场94.7%是亏损平仓，
        # 去掉后Sharpe 0.74→0.80, 特朗普2期 1.98→2.10, 6个时段5个提升)
        # Keltner现在只靠 TP(6.5×ATR) / SL(2.5×ATR) / 超时(15根K线) 出场
        pass

    elif strategy == 'macd':
        macd_hist = float(latest['MACD_hist'])
        macd_hist_prev = float(prev['MACD_hist'])
        if not pd.isna(macd_hist):
            if direction == 'BUY' and macd_hist < 0 and macd_hist_prev >= 0:
                return "MACD多头出场: 柱状图转负"
            elif direction == 'SELL' and macd_hist > 0 and macd_hist_prev <= 0:
                return "MACD空头出场: 柱状图转正"

    elif strategy == 'm15_rsi':
        rsi2 = float(latest["RSI2"]) if "RSI2" in latest else None

        if rsi2 is not None and not pd.isna(rsi2):
            # 【做多离场】：仅当 RSI(2) 达到极值 > 85 时平仓
            if direction == "BUY" and rsi2 > 85:
                return f"M15 RSI多头离场: RSI(2)={rsi2:.1f} > 85 (动能饱和平仓)"

            # 【做空离场】：仅当 RSI(2) 达到极值 < 20 时平仓
            elif direction == "SELL" and rsi2 < 20:
                return f"M15 RSI空头离场: RSI(2)={rsi2:.1f} < 20 (超卖探底平仓)"

    return None


from typing import Dict, Optional
import pandas as pd


import logging
from typing import Dict, Optional
import pandas as pd

log = logging.getLogger(__name__)

#===========================================================================
import time
import pandas as pd
from typing import Optional, Dict

# --- 记录上一次开仓时间 (全局变量/模块级变量) ---
_LAST_BUY_TIME: float = 0.0
_LAST_SELL_TIME: float = 0.0

# M15 RSI 止损参数
# 说明: 旧代码写成 `sl = min(max(_calc_atr_stop(df), 3.0), 10.0)`, 但
# _calc_atr_stop() 的下限本身就是 10, 所以结果恒等于 $10 —— ATR 完全失效,
# 而且 min(...) 的 10.0 上限把 [3, 10] 的区间夹成了单点。
M15_RSI_SL_ATR_MULTIPLIER = 2.5   # 止损 = 2.5 × M15 ATR
M15_RSI_SL_MIN = 3.0              # 最紧 $3
M15_RSI_SL_MAX = 10.0             # 最松 $10
M15_RSI_SL_DEFAULT = 8.0          # ATR 无效时的默认值


def _calc_m15_rsi_stop(df: pd.DataFrame) -> float:
    """M15 RSI 止损: 2.5×ATR, 收窄到 [$3, $10]"""
    try:
        atr = float(df.iloc[-1]['ATR'])
    except (KeyError, IndexError, TypeError, ValueError):
        atr = float('nan')
    if pd.isna(atr) or atr <= 0:
        return M15_RSI_SL_DEFAULT
    sl = atr * M15_RSI_SL_ATR_MULTIPLIER
    return round(min(max(sl, M15_RSI_SL_MIN), M15_RSI_SL_MAX), 2)


def check_m15_rsi_signal(
    df: pd.DataFrame, df_m5: Optional[pd.DataFrame] = None
) -> Optional[Dict]:
    """M15 RSI均值回归信号 (支持 10 分钟冷却限制)"""
    global _LAST_BUY_TIME, _LAST_SELL_TIME

    # --- 校验 0: 数据长度 ---
    if df is None:
        print("[M15-RSI] ❌ 失败: M15 数据源为空 (df is None)")
        return None
    if len(df) < 55:
        print(f"[M15-RSI] ❌ 失败: M15 数据行数不足 (当前: {len(df)} 行, 需要 >= 55)")
        return None

    # 确保 df (M15) 按时间升序排列
    time_col = "t" if "t" in df.columns else ("Time" if "Time" in df.columns else None)
    if time_col:
        df[time_col] = pd.to_datetime(df[time_col])
        df = df.sort_values(time_col, ascending=True).reset_index(drop=True)

    latest = df.iloc[-1]
    prev = df.iloc[-2]  # 提取前一根 M15 K 线

    close = float(latest["Close"])

    # 提取前一根与当前实时 RSI2
    prev_rsi2 = float(prev["RSI2"]) if "RSI2" in prev else None
    curr_rsi2 = float(latest["RSI2"]) if "RSI2" in latest else None

    # --- 校验 1: RSI2 数据完整性 ---
    if (
        prev_rsi2 is None
        or pd.isna(prev_rsi2)
        or curr_rsi2 is None
        or pd.isna(curr_rsi2)
    ):
        print(
            f"[M15-RSI] ❌ 失败: RSI2 数据缺失 (前1: {prev_rsi2}, 实时: {curr_rsi2})"
        )
        return None

    # --- 校验 2: ADX 震荡硬过滤 ---
    adx14 = (
        float(latest["ADX14"])
        if "ADX14" in latest and not pd.isna(latest["ADX14"])
        else (
            float(latest["ADX"])
            if "ADX" in latest and not pd.isna(latest["ADX"])
            else None
        )
    )

    if adx14 is not None and adx14 >= 25:
        print(
            f"[M15-RSI] ❌ 过滤: M15 处于趋势行情 (ADX={adx14:.1f} >= 25, 排除震荡策略)"
        )
        return None

    # --- 校验 3: M5 形态确认 ---
    is_m5_bullish = None
    is_m5_bearish = None

    if df_m5 is not None and len(df_m5) >= 2:
        m5_time_col = (
            "t"
            if "t" in df_m5.columns
            else ("Time" if "Time" in df_m5.columns else None)
        )
        if m5_time_col:
            df_m5[m5_time_col] = pd.to_datetime(df_m5[m5_time_col])
            df_m5 = df_m5.sort_values(m5_time_col, ascending=True).reset_index(
                drop=True
            )

        last_closed_m5 = df_m5.iloc[-2]
        m5_time = (
            last_closed_m5[m5_time_col] if m5_time_col else "未提供时间戳"
        )
        m5_open = float(last_closed_m5["Open"])
        m5_high = float(last_closed_m5["High"])
        m5_low = float(last_closed_m5["Low"])
        m5_close = float(last_closed_m5["Close"])

        m5_body = abs(m5_close - m5_open)
        m5_total = m5_high - m5_low

        has_strong_body = m5_total > 0 and m5_body >= 0.2 * m5_total
        is_m5_bullish = (m5_close > m5_open) and has_strong_body
        is_m5_bearish = (m5_close < m5_open) and has_strong_body

        print(
            f"[M5校验] 时间: {m5_time} | Open: {m5_open:.2f} | Close: {m5_close:.2f} | "
            f"实体: {m5_body:.2f}/{m5_total:.2f} | 强阳止跌: {is_m5_bullish} | 强阴见顶: {is_m5_bearish}"
        )

    elif df_m5 is None:
        # M5 数据缺失 → is_m5_bullish/is_m5_bearish 保持 None → 永远不会开仓。
        # 必须写进日志, 否则 M15 RSI 会"静默死亡"(只print在控制台, 不落 gold_runner.log)。
        log.warning(
            "  ⚠️ [M15-RSI] 缺少 M5 数据 (bars_m5.json 不存在/yfinance失败)，"
            "无法做止跌·见顶校验 → 本策略无法开仓! 请检查EA是否在写 bars_m5.json"
        )
        print("[M15-RSI] ⚠️ 警告: 未传入 M5 数据 (df_m5 is None)，无法校验止跌/见顶")
    elif len(df_m5) < 2:
        print(
            f"[M15-RSI] ❌ 失败: M5 数据量不足 (当前: {len(df_m5)} 行, 需要 >= 2)"
        )
        return None

    # ADX 可能是 None (数据不足/全平时 ADX 会算成 NaN), 不能直接拿去格式化,
    # 否则这里会抛 TypeError, 把整个扫描周期(所有策略)一起打断。
    adx_txt = f"{adx14:.1f}" if adx14 is not None else "N/A"

    # --- 校验 4: 触发条件判断 ---
    is_rsi_oversold = (prev_rsi2 < 15) or (curr_rsi2 < 15)
    is_rsi_overbought = (prev_rsi2 > 85) or (curr_rsi2 > 85)

    # --- 校验 5: 趋势对齐过滤 ---
    # 旧版(远端 8846c92)原本有这条: 做多要求 价格>SMA50, 做空要求 价格<SMA50,
    # 即"只在SMA50的顺势一侧做均值回归"。本地重写时把它丢掉了。
    # 回测(research/ab_remote_vs_local.py, 2024-01→2025-01)显示这正是关键差异:
    #   无对齐: 1181笔 净 -$488 (其中空头 -$706, 牛市里逆势抄顶被打爆)
    #   有对齐:  434笔 净   -$5 (其中空头  -$92)
    if getattr(config, 'M15_RSI_TREND_FILTER', True):
        try:
            _sma50 = float(latest["SMA50"])
        except Exception:
            _sma50 = float('nan')
        if not pd.isna(_sma50):
            if is_rsi_oversold and close < _sma50:
                return None      # 超卖但价在SMA50下方 = 下跌趋势里接刀, 放弃
            if is_rsi_overbought and close > _sma50:
                return None      # 超买但价在SMA50上方 = 上涨趋势里摸顶, 放弃

    current_time = time.time()
    # 同方向两次开仓的最小间隔。原来硬编码 1200 秒(20分钟)。
    # 回测(research/backtest_m15rsi.py, 2024-01→2025-01)显示: 该策略毛利为正,
    # 但一年约1300笔 × 点差$0.46 ≈ $600 的成本会把毛利全部吃掉。
    # 把间隔放宽到180分钟后, 净亏损从 -$300 收敛到约 -$6 (基本打平)。
    cooldown_seconds = int(getattr(config, 'M15_RSI_COOLDOWN_MINUTES', 20)) * 60

    # 4.1 做多判断
    if is_rsi_oversold:
        if is_m5_bullish:
            # --- 10分钟防重复开多单检查 ---
            if current_time - _LAST_BUY_TIME < cooldown_seconds:
                remaining_sec = int(cooldown_seconds - (current_time - _LAST_BUY_TIME))
                print(
                    f"[M15-RSI] ⏳ 冷却中: {cooldown_seconds//60}分钟内已开过多单，还需等待 {remaining_sec} 秒"
                )
                return None

            sl = _calc_m15_rsi_stop(df)

            # 更新做多开仓时间戳
            _LAST_BUY_TIME = current_time

            log.info(
                f"🚀 [M15-RSI] ✅ 触发做多信号! RSI2(前1={prev_rsi2:.1f},"
                f" 实时={curr_rsi2:.1f}), M5强阳, SL=${sl:.2f}"
            )
            return {
                "strategy": "m15_rsi",
                "signal": "BUY",
                "reason": (
                    f"M15 RSI做多: RSI(2)(前1={prev_rsi2:.1f}, 实时={curr_rsi2:.1f}"
                    f" < 15), M5强阳止跌 (ADX={adx_txt})"
                ),
                "close": close,
                "sl": sl,
                "tp": 0,
            }
        else:
            print(
                f"[M15-RSI] ⏳ 未触发做多: M15 RSI2 已超卖 (前1={prev_rsi2:.1f},"
                f" 实时={curr_rsi2:.1f} < 15)，但 M5 未出现强实体阳线止跌"
            )

    # 4.2 做空判断
    elif is_rsi_overbought:
        if is_m5_bearish:
            # --- 10分钟防重复开空单检查 ---
            if current_time - _LAST_SELL_TIME < cooldown_seconds:
                remaining_sec = int(cooldown_seconds - (current_time - _LAST_SELL_TIME))
                print(
                    f"[M15-RSI] ⏳ 冷却中: {cooldown_seconds//60}分钟内已开过空单，还需等待 {remaining_sec} 秒"
                )
                return None

            sl = _calc_m15_rsi_stop(df)

            # 更新做空开仓时间戳
            _LAST_SELL_TIME = current_time

            log.info(
                f"🚀 [M15-RSI] ✅ 触发做空信号! RSI2(前1={prev_rsi2:.1f},"
                f" 实时={curr_rsi2:.1f}), M5强阴, SL=${sl:.2f}"
            )
            return {
                "strategy": "m15_rsi",
                "signal": "SELL",
                "reason": (
                    f"M15 RSI做空: RSI(2)(前1={prev_rsi2:.1f}, 实时={curr_rsi2:.1f}"
                    f" > 85), M5强阴见顶 (ADX={adx_txt})"
                ),
                "close": close,
                "sl": sl,
                "tp": 0,
            }
        else:
            print(
                f"[M15-RSI] ⏳ 未触发做空: M15 RSI2 已超买 (前1={prev_rsi2:.1f},"
                f" 实时={curr_rsi2:.1f} > 85)，但 M5 未出现强实体阴线见顶"
            )

    else:
        # 既不超买也不超卖时的常规汇报
        print(
            f"[M15-RSI] ⚪ 无信号: RSI2(前1={prev_rsi2:.1f},"
            f" 实时={curr_rsi2:.1f}) 处于常态区间 [15 ~ 85]"
        )

    return None
# ═══════════════════════════════════════════════════════════════
# NY开盘区间突破 (ORB) 策略
# ═══════════════════════════════════════════════════════════════

import config as _cfg


class ORBStrategy:
    """
    NY开盘区间突破策略 (Opening Range Breakout)

    原理:
    - 纽约开盘后前15分钟的高低点形成当日区间
    - 价格突破区间上沿→做多
    - 价格跌破区间下沿→做空
    - 止损=区间宽度, 止盈=2.2×区间宽度
    - 窗口有效期2小时 (过时不入场)
    - 每日只交易一次

    胜率61%, RR 1:2.2 (根据历史研究)
    """

    def __init__(self):
        self.range_high = None
        self.range_low = None
        self.range_set_date = None     # 区间设定日期
        self.traded_today = False      # 今日是否已交易
        self.window_open = False
        self.window_expiry = None

    def reset_daily(self):
        """每日重置"""
        self.range_high = None
        self.range_low = None
        self.range_set_date = None
        self.traded_today = False
        self.window_open = False
        self.window_expiry = None

    def update(self, df: pd.DataFrame) -> Optional[Dict]:
        """
        用H1数据检测ORB信号

        逻辑:
        1. 识别NY开盘K线 (UTC 14:xx, 经 MT4_SERVER_UTC_OFFSET_HOURS 换算) → 设定区间
        2. 后续的K线检查是否突破
        """

        if len(df) < 10:
            return None

        latest = df.iloc[-1]
        close = float(latest['Close'])
        high = float(latest['High'])
        low = float(latest['Low'])

        bar_time = df.index[-1]
        today = bar_time.date() if hasattr(bar_time, 'date') else None

        # 新的一天重置
        if today and self.range_set_date and today != self.range_set_date:
            self.reset_daily()

        # Step 1: 识别NY开盘K线 → 设定区间
        # 回溯最近3根K线，防止因扫描间隔错过开盘K线
        if self.range_high is None:
            for lookback in range(min(3, len(df))):
                idx = -(lookback + 1)
                check_bar = df.iloc[idx]
                check_time = df.index[idx]
                # K线时间戳来自MT4 = 经纪商服务器时间, 需换算成UTC再和 ORB_NY_OPEN_HOUR_UTC 比较
                check_hour = (
                    (check_time.hour - getattr(_cfg, 'MT4_SERVER_UTC_OFFSET_HOURS', 0)) % 24
                    if hasattr(check_time, 'hour') else -1
                )
                check_date = check_time.date() if hasattr(check_time, 'date') else None
                
                if check_hour == _cfg.ORB_NY_OPEN_HOUR_UTC:
                    self.range_high = float(check_bar['High'])
                    self.range_low = float(check_bar['Low'])
                    self.range_set_date = check_date
                    self.window_open = True
                    # 窗口有效期减去已过去的K线数
                    self.window_expiry = max(1, _cfg.ORB_EXPIRY_MINUTES // 60 - lookback)

                    range_width = self.range_high - self.range_low
                    if lookback > 0:
                        log.info(f"  [🇺🇸 ORB] 回溯{lookback}根K线找到NY开盘")
                    log.info(f"  [🇺🇸 ORB] NY开盘区间设定: [{self.range_low:.2f} - {self.range_high:.2f}] "
                             f"宽度=${range_width:.2f} 窗口{self.window_expiry}根K线")
                    
                    # 如果是回溯找到的, 当前K线可能已经在突破 → 继续走Step 2检查
                    if lookback > 0:
                        break
                    return None  # 开盘K线本身不交易

        # Step 2: 检查突破
        if self.window_open and self.range_high is not None and not self.traded_today:
            self.window_expiry -= 1

            # 窗口超时
            if self.window_expiry <= 0:
                log.info(f"  [🇺🇸 ORB] 窗口超时，今日不再触发")
                self.window_open = False
                return None

            range_width = self.range_high - self.range_low
            if range_width < 3:  # 区间太窄，不可靠
                return None
            if range_width > 60:  # 区间太宽，风险太大
                log.info(f"  [🇺🇸 ORB] 区间宽度${range_width:.2f}太大，跳过")
                self.window_open = False
                return None

            # 突破上沿 → 做多
            if high > self.range_high:
                self.traded_today = True
                self.window_open = False
                # 结构位口径: 止损放到开盘区间【下沿】外侧, 止盈取区间宽度(等幅目标)
                lv = calc_dynamic_levels(
                    df, direction='BUY', entry=close,
                    structural_stop_dist=max(0.01, close - self.range_low),
                    structural_target_dist=range_width,
                )
                return {
                    'strategy': 'orb',
                    'signal': 'BUY',
                    'reason': f"🇺🇸 ORB做多: 价格{high:.2f} 突破开盘区间上沿{self.range_high:.2f} (区间${range_width:.1f}) | {lv['reason']}",
                    'close': close,
                    'sl': lv['sl'],
                    'tp': lv['tp'],
                    'atr': lv['atr'],
                    'regime': lv['regime'],
                    'bar_time': df.index[-1],
                }

            # 跌破下沿 → 做空
            if low < self.range_low:
                self.traded_today = True
                self.window_open = False
                lv = calc_dynamic_levels(
                    df, direction='SELL', entry=close,
                    structural_stop_dist=max(0.01, self.range_high - close),
                    structural_target_dist=range_width,
                )
                return {
                    'strategy': 'orb',
                    'signal': 'SELL',
                    'reason': f"🇺🇸 ORB做空: 价格{low:.2f} 跌破开盘区间下沿{self.range_low:.2f} (区间${range_width:.1f}) | {lv['reason']}",
                    'close': close,
                    'sl': lv['sl'],
                    'tp': lv['tp'],
                    'atr': lv['atr'],
                    'regime': lv['regime'],
                    'bar_time': df.index[-1],
                }

        return None

    def get_status(self) -> str:
        if self.range_high is None:
            return "等待NY开盘"
        if self.traded_today:
            return "今日已交易"
        if self.window_open:
            return f"窗口开启 [{self.range_low:.0f}-{self.range_high:.0f}] 剩{self.window_expiry}根K线"
        return "窗口已关闭"


# 全局ORB实例
_orb_strategy = ORBStrategy()

def get_orb_strategy() -> ORBStrategy:
    return _orb_strategy

def check_orb_signal(df: pd.DataFrame) -> Optional[Dict]:
    """检查ORB信号"""
    return _orb_strategy.update(df)


# ═══════════════════════════════════════════════════════════════
# ATR自动调仓
# ═══════════════════════════════════════════════════════════════

def calc_auto_lot_size(atr: float, sl_distance: float) -> float:
    """
    根据ATR/止损距离自动计算手数，保持每笔风险金额恒定

    公式: lots = RISK_PER_TRADE / (sl_distance × POINT_VALUE_PER_LOT)

    例如:
    - RISK_PER_TRADE=$100, sl=$37.5, POINT_VALUE=100
    - lots = 100 / (37.5 × 100) = 0.027 → 0.03手
    - 实际风险 = 37.5 × 0.03 × 100 = $112.5

    - RISK_PER_TRADE=$100, sl=$77.5 (高ATR), POINT_VALUE=100
    - lots = 100 / (77.5 × 100) = 0.013 → 0.01手
    - 实际风险 = 77.5 × 0.01 × 100 = $77.5
    """
    if not _cfg.AUTO_LOT_SIZING:
        return _cfg.LOT_SIZE

    if sl_distance <= 0:
        return _cfg.LOT_SIZE

    lots = _cfg.RISK_PER_TRADE / (sl_distance * _cfg.POINT_VALUE_PER_LOT)
    # 四舍五入到小数点后两位
    lots = round(lots, 2)
    # 限制范围
    lots = max(_cfg.MIN_LOT_SIZE, min(_cfg.MAX_LOT_SIZE, lots))
    return lots


# ═══════════════════════════════════════════════════════════════
# 信号扫描入口
# ═══════════════════════════════════════════════════════════════

def is_strategy_enabled(name: str) -> bool:
    """
    读取策略开关 — 唯一真值来源 config.STRATEGIES[name]['enabled']

    历史bug (本次修复):
      1. check_keltner_signal() 被无条件调用, 完全没看 STRATEGIES['keltner']['enabled'],
         所以 config 里把 keltner 设为 False 之后仍然继续开单。
      2. ORB 读的是 config.ORB_ENABLED (另一个独立开关, 一直是 True),
         而不是 STRATEGIES['orb']['enabled'], 所以设为 False 也无效。
      3. M15 RSI 没有任何开关, 永远执行。
    现在所有策略统一走这个函数, 并且缺少配置项时一律视为"未启用"。
    """
    import config as _cfg
    entry = _cfg.STRATEGIES.get(name)
    if not isinstance(entry, dict):
        log.warning(
            f"  ⚠️ 策略 '{name}' 在 config.STRATEGIES 中没有配置项，按【未启用】处理"
        )
        return False
    return bool(entry.get('enabled', False))


def get_enabled_strategies() -> List[str]:
    """返回当前已启用的策略名列表 (用于启动日志自检)"""
    import config as _cfg
    return [
        name for name, entry in _cfg.STRATEGIES.items()
        if isinstance(entry, dict) and entry.get('enabled', False)
    ]


# 策略名 → 检测函数 (只在启用的前提下才会被调用)
_H1_CHECKERS = (
    ('keltner', check_keltner_signal),
    ('macd', check_macd_signal),
)

# 真正接入了 scan_all_signals() 的策略名清单。
# 新增策略时必须同步这个元组, 否则 get_unwired_strategies() 会在启动时报警 —
# 防止出现"config里enabled=True但代码根本没调用"的反向bug
# (M15 RSI 就曾经因为 STRATEGIES 里没有 m15_rsi 条目而一直不交易)。
WIRED_STRATEGIES = ('keltner', 'macd', 'orb', 'm15_rsi')


def get_unwired_strategies() -> List[str]:
    """返回 config 中已启用、但代码里没有对应检测函数的策略名 (应报警)"""
    import config as _cfg
    return [
        name for name, entry in _cfg.STRATEGIES.items()
        if isinstance(entry, dict)
        and entry.get('enabled', False)
        and name not in WIRED_STRATEGIES
    ]


def scan_all_signals(df, timeframe='H1', df_m5=None):
    """
    扫描所有【已启用】策略的信号

    每个策略都必须经过 is_strategy_enabled() 判断, 不允许出现无条件调用。
    """
    signals = []

    if timeframe == 'H1':
        for name, checker in _H1_CHECKERS:
            if not is_strategy_enabled(name):
                log.debug(f"    ⏭️ {name} 已在 config 中禁用，跳过检测")
                continue
            sig = checker(df)
            if sig:
                signals.append(sig)

        # ORB 也用 H1 数据, 开关同样只看 STRATEGIES['orb']['enabled']
        if is_strategy_enabled('orb'):
            sig = check_orb_signal(df)
            if sig:
                signals.append(sig)
        else:
            log.debug("    ⏭️ orb 已在 config 中禁用，跳过检测")

    elif timeframe in ('M5', 'M15'):
        if is_strategy_enabled('m15_rsi'):
            sig = check_m15_rsi_signal(df, df_m5=df_m5)
            if sig:
                signals.append(sig)
        else:
            log.debug("    ⏭️ m15_rsi 已在 config 中禁用，跳过检测")

    return signals

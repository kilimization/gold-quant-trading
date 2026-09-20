"""黄金量化交易本地运行器 (Windows)

================================
24/5 持续运行，每分钟扫描一次

使用方法:
  1. 先配置 config.py 中的 METATRADER_DIR_PATH
  2. 在MT4上加载 mt4_ea/GoldBridge_EA.mq4
  3. python gold_runner.py

按 Ctrl+C 停止
"""

from datetime import datetime
from pathlib import Path
import logging
# --- 方法：筛选并屏蔽 httpx / httpcore 模块的报错 ---
class SuppressHTTPXErrorsFilter(logging.Filter):

  def filter(self, record):
    # 如果日志来自 httpx 或 httpcore，直接屏蔽（返回 False）
    if record.name and (
        record.name.startswith("httpx") or record.name.startswith("httpcore")
    ):
      return False
    return True


# 挂载到根 Logger 或特定的 Logger
logging.getLogger().addFilter(SuppressHTTPXErrorsFilter())

# 另外，将第三方库的日志级别提高到 CRITICAL，减少无用输出
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpcore").setLevel(logging.CRITICAL)
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

import config
from gold_trader import GoldTrader
from strategies.signals import get_enabled_strategies, get_unwired_strategies
from strategies.risk import check_risk_config, breakeven_winrate
import notifier

# ============================================================
# 时区
# ============================================================
ET = ZoneInfo("America/New_York")  # 用于判断周末
LOCAL_TZ = ZoneInfo("Asia/Singapore")

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            config.LOG_DIR / "gold_runner.log", encoding="utf-8"
        ),
    ],
)
log = logging.getLogger(__name__)

# 全局交易实例
trader: GoldTrader = None


# ============================================================
# Telegram 状态与控制接口回调
# ============================================================


def get_current_system_status() -> dict:
    """安全读取交易系统状态供 Telegram 机器人查询"""
    global trader

    if trader is None:
        return {"system_status": "⚠️ 交易系统正在启动中，请稍后..."}

    try:
        # 判断运行暂停状态
        is_paused = getattr(trader, "is_paused", False)
        sys_status = (
            "⏸️ 已暂停交易 (暂停新开仓)" if is_paused else "🟢 正常运行中"
        )
        
        # 1. 提取账户信息 (从 trader.bridge 中获取 account.json)
        account_info = {}
        if hasattr(trader, "bridge") and trader.bridge is not None:
            account_info = trader.bridge.get_account() or {}
        elif hasattr(trader, "account_info"):
            acc_info = getattr(trader, "account_info")
            account_info = acc_info() if callable(acc_info) else acc_info

        if not isinstance(account_info, dict):
            account_info = {}
            
        # 兼容大小写字段名 (balance / Balance, equity / Equity)
        balance = account_info.get("balance")
        if balance is None:
            balance = account_info.get("Balance", 0.0)

        equity = account_info.get("equity")
        if equity is None:
            equity = account_info.get("Equity", 0.0)

        # 2. 提取持仓列表
        positions = getattr(trader, "get_strategy_positions", None)
        if callable(positions):
            positions = positions()
        else:
            positions = getattr(trader, "positions", [])
            if callable(positions):
                positions = positions()
        if not isinstance(positions, list):
            positions = []

        # 3. 提取盈亏等关键数值
        def _to_float(val, default=0.0):
            if isinstance(val, dict):
                val = list(val.values())[0] if val else default
            try:
                return float(val) if val is not None else default
            except (TypeError, ValueError):
                return default

        # 兼容获取 total_pnl 结构
        pnl_data = getattr(trader, "total_pnl", {})
        if isinstance(pnl_data, dict):
            total_pnl = _to_float(pnl_data.get("total_pnl", 0.0))
            trade_count = int(_to_float(pnl_data.get("trade_count", 0)))
        else:
            total_pnl = _to_float(pnl_data)
            trade_count = int(
                _to_float(getattr(trader, "total_trade_count", 0))
            )

        daily_pnl = _to_float(getattr(trader, "daily_pnl", 0.0))
        daily_losses = int(
            _to_float(
                getattr(
                    trader,
                    "daily_loss_count",
                    getattr(trader, "daily_losses", 0),
                )
            )
        )

        return {
            "system_status": sys_status,
            "balance": balance,
            "equity": equity,
            "daily_pnl": daily_pnl,
            "total_pnl": total_pnl,
            "daily_losses": daily_losses,
            "trade_count": trade_count,
            "positions": positions,
        }
    except Exception as e:
        log.exception(f"获取系统状态异常: {e}")
        return {"system_status": f"⚠️ 交互读取数据异常: {e}"}

def handle_telegram_control(action: str) -> tuple[bool, str]:
  """处理来自 Telegram 的控制指令 (/pause, /resume)"""
  global trader

  if trader is None:
    return False, "交易系统未完全初始化"

  if action == "pause":
    if hasattr(trader, "pause_trading"):
      trader.pause_trading()
    else:
      trader.is_paused = True
    log.warning("⏸️ 收到 Telegram 指令：已暂停开仓交易")
    return True, "指令执行成功：系统已暂停开启新仓位"

  elif action == "resume":
    if hasattr(trader, "resume_trading"):
      trader.resume_trading()
    else:
      trader.is_paused = False
    log.info("▶️ 收到 Telegram 指令：已恢复开仓交易")
    return True, "指令执行成功：系统已恢复自动开仓功能"

  return False, f"未知的控制动作: {action}"


# ============================================================
# 交易逻辑与主循环
# ============================================================


def is_market_open():
  """黄金交易时间: 周日22:00 UTC - 周五21:00 UTC (几乎24/5)

  简化判断: 周六全天 + 周日早些时候 = 休市
  """
  now_utc = datetime.now(ZoneInfo("UTC"))
  weekday = now_utc.weekday()  # 0=Mon, 6=Sun

  if weekday == 5:  # 周六
    return False, "周六休市"
  if weekday == 6 and now_utc.hour < 22:  # 周日22:00 UTC前
    return False, "周日尚未开市 (22:00 UTC开市)"
  if weekday == 4 and now_utc.hour >= 21:  # 周五21:00 UTC后
    return False, "周五已收市"

  return True, f"交易中 ({now_utc.strftime('%H:%M')} UTC)"


def main():
  global trader

  log.info("🥇 黄金量化交易系统启动")
  log.info(f"   品种: {config.SYMBOL}")
  log.info(f"   手数: {config.LOT_SIZE}")
  log.info(
      f"   本金: ${config.CAPITAL}  止损上限: ${config.MAX_TOTAL_LOSS}"
  )
  log.info(f"   扫描频率: 每{config.SCAN_INTERVAL_SECONDS}秒")

  # 启动自检: 打印真正生效的策略开关 (避免"以为关了其实没关")
  enabled = get_enabled_strategies()
  disabled = [n for n, e in config.STRATEGIES.items()
              if not (isinstance(e, dict) and e.get("enabled", False))]
  if enabled:
    log.info("   已启用策略: " + ", ".join(
        f"{n}({config.STRATEGIES[n].get('name', n)})" for n in enabled))
  else:
    log.warning("   🛑 没有任何策略被启用 — 系统不会开任何新仓! 请检查 config.STRATEGIES")
  if disabled:
    log.info("   已禁用策略: " + ", ".join(disabled))

  unwired = get_unwired_strategies()
  if unwired:
    log.warning(
      f"   ⚠️ 这些策略在 config 里是 enabled=True, 但代码里没有接入检测函数, "
      f"实际永远不会开仓: {', '.join(unwired)}"
    )

  # 止损止盈/移动止损参数自检 —— 参数自相矛盾时"动态止损"会变成死代码
  risk_problems = check_risk_config()
  if risk_problems:
    for p in risk_problems:
      log.warning(f"   ⚠️ [风控参数] {p}")
  log.info(
    f"   止盈止损: 结构位(前{config.DYN_STRUCT_LOOKBACK}根已收盘K线高低点)"
    f"+ATR缓冲{config.DYN_STRUCT_BUFFER_ATR}×ATR | "
    f"ATR兜底 SL{config.DYN_SL_ATR_MULTIPLIER}×/TP{config.DYN_TP_ATR_MULTIPLIER}×"
  )
  log.info(
    f"   保本{config.BREAKEVEN_TRIGGER_ATR}×ATR → 跟踪{config.TRAIL_START_ATR}×ATR"
    f"(距离{config.TRAIL_DISTANCE_ATR}×ATR) | "
    f"ATR兜底口径约需胜率>{breakeven_winrate()*100:.0f}% (结构口径下每个信号不同)"
  )

  # 1. 实例化交易对象
  trader = GoldTrader()

  # 2. 启动 Telegram 机器人交互服务 (传入状态查询和控制回调)
  try:
    notifier.init_telegram_bot(
        status_provider_func=get_current_system_status,
        control_handler_func=handle_telegram_control,
    )
  except Exception as e:
    log.error(f"启动 Telegram Bot 失败: {e}")

  # 3. 启动时同步MT4持仓状态，避免重启后重复开仓
  try:
    trader._sync_positions_tracking()
    positions = trader.get_strategy_positions()
    if positions:
      log.info(f"📊 启动时检测到 {len(positions)} 笔持仓:")
      for p in positions:
        tk = str(p["ticket"])
        track = trader.tracking.get(tk, {})
        log.info(
            f"   #{tk} {track.get('strategy','?')} {track.get('direction','?')}"
            f" @ {p.get('open_price',0)}"
        )
    else:
      log.info("📊 启动时无持仓")
  except Exception as e:
    log.warning(f"启动同步失败: {e}")

  # 4. 发送 Telegram 系统启动通知
  try:
    notifier.notify_system_start()
  except Exception as e:
    log.debug(f"发送启动通知失败: {e}")

  signal_scanned_today = False
  last_date = None
  scan_count = 0
  daily_start_pnl = trader.total_pnl.get("total_pnl", 0)
  daily_trades = 0

  while True:
    try:
      now = datetime.now(LOCAL_TZ)
      today = now.date()

      # 新的一天重置 + 打印前一天绩效报告
      if today != last_date:
        if last_date is not None:
          # 前一天绩效报告
          current_pnl = trader.total_pnl.get("total_pnl", 0)
          day_pnl = round(current_pnl - daily_start_pnl, 2)
          total_trades = trader.total_pnl.get("trade_count", 0)
          emoji = "🟢" if day_pnl >= 0 else "🔴"
          log.info(f"\n{'='*60}")
          log.info(f"📊 每日绩效报告 — {last_date}")
          log.info(f"  {emoji} 当日盈亏: ${day_pnl:+.2f}")
          log.info(f"  💰 累计盈亏: ${current_pnl:+.2f}")
          log.info(f"  📊 总交易笔数: {total_trades}")
          log.info(
              f"  🛡️ 止损余量: ${config.MAX_TOTAL_LOSS + current_pnl:.2f}"
          )
          log.info(f"{'='*60}")
          try:
            notifier.notify_daily_report(
                current_pnl, day_pnl, total_trades
            )
          except Exception:
            pass

        signal_scanned_today = False
        last_date = today
        scan_count = 0
        daily_start_pnl = trader.total_pnl.get("total_pnl", 0)
        daily_trades = 0
        log.info(f"\n📅 {today} ({now.strftime('%A')})")
        log.info(f"  💰 今日起始盈亏: ${daily_start_pnl:+.2f}")

      is_open, status = is_market_open()

      if not is_open:
        log.info(f"💤 {status} | 等待5分钟...")
        time.sleep(300)
        continue

      scan_count += 1

      # 每10次(约10分钟)打印一次状态
      if scan_count % 10 == 1:
        log.info(f"\n🔄 扫描 #{scan_count} | {status}")

      # 检查出场
      try:
        trader.check_exits_only()
      except Exception as e:
        log.error(f"出场检查出错: {e}")

      # 每5分钟做一次完整信号扫描 (M15策略需要, 每10次循环×30秒≈5分钟)
      if scan_count == 1 or scan_count % 2 == 0:
        log.info(f"\n📊 完整信号扫描 (#{scan_count})")
        try:
          result = trader.scan_and_trade()

          # 检查是否触发停止复盘
          if result.get("status") == "STOP_REVIEW":
            log.warning("\n" + "=" * 60)
            log.warning("🚨 系统已停止 — 日内亏损超限")
            log.warning("请复盘后手动重启: python gold_runner.py")
            log.warning("=" * 60)
            break

          entries = result.get("entries", [])
          exits = result.get("exits", [])

          for e in entries:
            log.info(f"📈 买入: {e.get('reason', '')}")
          for e in exits:
            log.info(f"📉 平仓: {e.get('reason', '')}")

        except Exception as e:
          log.error(f"信号扫描出错: {e}")

      # 等待下一次扫描
      time.sleep(config.SCAN_INTERVAL_SECONDS)

    except KeyboardInterrupt:
      log.info("\n⏹️ 用户中断，停止运行")
      break
    except Exception as e:
      log.error(f"主循环异常: {e}")
      time.sleep(60)


if __name__ == "__main__":
  main()
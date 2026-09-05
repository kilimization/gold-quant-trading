"""
Telegram 交互与通知模块
================
1. 被动事件推送 (开仓/平仓/风控)
2. 主动命令响应 (发送 /status /report /pause /resume /start 等)
"""

import asyncio
import logging
import threading
from typing import Callable, Optional

from telegram import Update
from telegram.ext import Application,ApplicationBuilder,CommandHandler, ContextTypes
from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest

import config

log = logging.getLogger(__name__)

# 全局存储应用实例与系统状态提供者/控制句柄
_tg_app: Optional[Application] = None
_status_provider: Optional[Callable[[], dict]] = None
_control_handler: Optional[Callable[[str], tuple[bool, str]]] = None

# 1. 增加异常拦截函数
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """拦截并记录 Telegram Bot 的网络波动或运行异常，防止日志爆红"""
    log.warning(f"Telegram 网络波动/响应异常 (已自动恢复): {context.error}")
# ═══════════════════════════════════════════════════════════════
# 辅助解析工具函数
# ═══════════════════════════════════════════════════════════════


def _auth_check(update: Update) -> bool:
  """安全校验：仅允许 config 中配置的 CHAT_ID 操作"""
  if str(update.effective_chat.id) != str(config.TELEGRAM_CHAT_ID):
    log.warning(f"未经授权的用户试图调用机器人: {update.effective_chat.id}")
    return False
  return True


def _safe_float(val, default: float = 0.0) -> float:
  """安全提取 float，防止 dict 或 None 类型引发格式化报错"""
  if isinstance(val, dict):
    val = next(iter(val.values()), default) if val else default
  try:
    return float(val) if val is not None else default
  except (ValueError, TypeError):
    return default


# ═══════════════════════════════════════════════════════════════
# 1. 主动指令处理函数 (接收手机命令并回复)
# ═══════════════════════════════════════════════════════════════


async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """响应 /start 和 /help 指令：显示菜单帮手"""
  if not _auth_check(update):
    return

  menu_text = (
      "🤖 <b>黄金量化交易机器人控制台</b>\n\n"
      "<b>📊 查询指令：</b>\n"
      "• /status - 获取账户、实时持仓与系统运行状态\n"
      "• /report - 查看当日盈亏与累计绩效报告\n\n"
      "<b>🎮 控制指令：</b>\n"
      "• /pause - ⏸️ <b>一键暂停交易</b> (暂停新开仓)\n"
      "• /resume - ▶️ <b>一键恢复交易</b> (允许正常开仓)\n"
      "• /help - 显示当前帮助菜单"
  )
  await update.message.reply_html(menu_text)


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """响应 /status 指令：主动返回当前持仓和系统状态"""
  if not _auth_check(update):
    return

  if _status_provider is None:
    await update.message.reply_html("⚠️ 状态数据源未注册，无法获取实时状态。")
    return

  try:
    data = _status_provider() or {}
    #log.info(f"[TG Bot DEBUG] _status_provider 数据: {data}") #调试用显示传回了什么数据
    balance = _safe_float(data.get("balance"))
    equity = _safe_float(data.get("equity"))
    daily_pnl = _safe_float(data.get("daily_pnl"))
    daily_losses = int(_safe_float(data.get("daily_losses")))

    positions = data.get("positions", [])
    pos_str = ""
    if not positions or not isinstance(positions, list):
      pos_str = "暂无空闲持仓"
    else:
      for p in positions:
        if isinstance(p, dict):
          p_type = p.get("type", "UNKNOWN")
          p_symbol = p.get("symbol", getattr(config, "SYMBOL", "XAUUSD"))
          p_vol = _safe_float(p.get("volume"))
          p_profit = _safe_float(p.get("profit"))
          pos_str += (
              f"• [{p_type}] {p_symbol} | 手数: {p_vol} |"
              f" 浮盈: ${p_profit:+.2f}\n"
          )

    max_losses = getattr(config, "DAILY_MAX_LOSSES", 3)
    msg = (
        "⚙️ <b>系统实时运行状态</b>\n\n"
        f"<b>交易状态:</b> {data.get('system_status', '正常')}\n"
        f"<b>账户余额:</b> ${balance:.2f}\n"
        f"<b>净值/权益:</b> ${equity:.2f}\n"
        f"<b>当日盈亏:</b> ${daily_pnl:+.2f}\n"
        f"<b>当日连亏:</b> {daily_losses} / {max_losses} 笔\n\n"
        f"<b>当前持仓列表：</b>\n{pos_str}"
    )
    await update.message.reply_html(msg)

  except Exception as e:
    log.exception(f"处理 /status 指令异常: {e}")
    await update.message.reply_text(f"❌ 获取状态失败: {e}")


async def _cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """响应 /report 指令：主动获取绩效"""
  if not _auth_check(update):
    return

  if _status_provider is None:
    await update.message.reply_html("⚠️ 状态数据源未可供查询。")
    return

  try:
    data = _status_provider() or {}

    total_pnl = _safe_float(data.get("total_pnl"))
    daily_pnl = _safe_float(data.get("daily_pnl"))
    trade_count = int(_safe_float(data.get("trade_count")))

    emoji = "🟢" if daily_pnl >= 0 else "🔴"
    max_total_loss = getattr(config, "MAX_TOTAL_LOSS", 0.0)

    msg = (
        "📊 <b>实时绩效报告</b>\n\n"
        f"{emoji} 当日盈亏: ${daily_pnl:+.2f}\n"
        f"💰 累计盈亏: ${total_pnl:+.2f}\n"
        f"📊 总交易笔数: {trade_count}\n"
        f"🛡️ 止损余量: ${max_total_loss + total_pnl:.2f}"
    )
    await update.message.reply_html(msg)
  except Exception as e:
    log.exception(f"处理 /report 指令异常: {e}")
    await update.message.reply_text(f"❌ 获取绩效失败: {e}")


async def _cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """响应 /pause 指令：暂停交易"""
  if not _auth_check(update):
    return

  if _control_handler is not None:
    success, msg = _control_handler("pause")
    if success:
      await update.message.reply_html(
          f"⏸️ <b>交易系统已暂停交易！</b>\n\n{msg}\n<i>系统将继续监控市场，但<b>不会开立任何新仓位</b>。</i>"
      )
    else:
      await update.message.reply_text(f"❌ 暂停失败: {msg}")
  elif "trader" in globals() and globals()["trader"] is not None:
    trader = globals()["trader"]
    if hasattr(trader, "pause_trading"):
      trader.pause_trading()
    else:
      trader.is_paused = True
    await update.message.reply_html(
        "⏸️ <b>交易系统已暂停交易！</b>\n\n系统将继续监控市场，但<b>不会开立任何新仓位</b>。"
    )
    log.info("收到 Telegram 指令：系统交易已暂停。")
  else:
    await update.message.reply_text("❌ 交易对象未初始化，无法暂停。")


async def _cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """响应 /resume 指令：恢复交易"""
  if not _auth_check(update):
    return

  if _control_handler is not None:
    success, msg = _control_handler("resume")
    if success:
      await update.message.reply_html(
          f"▶️ <b>交易系统已恢复交易！</b>\n\n{msg}\n<i>系统已重新开启自动下单功能。</i>"
      )
    else:
      await update.message.reply_text(f"❌ 恢复失败: {msg}")
  elif "trader" in globals() and globals()["trader"] is not None:
    trader = globals()["trader"]
    if hasattr(trader, "resume_trading"):
      trader.resume_trading()
    else:
      trader.is_paused = False
    await update.message.reply_html(
        "▶️ <b>交易系统已恢复交易！</b>\n\n系统已重新开启自动下单功能。"
    )
    log.info("收到 Telegram 指令：系统交易已恢复。")
  else:
    await update.message.reply_text("❌ 交易对象未初始化，无法恢复。")


# ═══════════════════════════════════════════════════════════════
# 2. 监听服务启动与后台异步通信
# ═══════════════════════════════════════════════════════════════


def init_telegram_bot(
    status_provider_func: Callable[[], dict],
    control_handler_func: Optional[
        Callable[[str], tuple[bool, str]]
    ] = None,
):
  """初始化并启动 Telegram 机器人后台监听线程
  :param status_provider_func: 获取实时状态的回调函数
  :param control_handler_func: 可选，处理 pause/resume 等控制命令的回调函数
  """
# 1. 从 config 获取 Token
  token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
  proxy_url = getattr(config, "TELEGRAM_PROXY", "http://127.0.0.1:7890")  # 替换为你的代理端口

  builder = ApplicationBuilder().token(token)

    # 💡 使用 HTTPXRequest 设置代理
  if proxy_url:
        request = HTTPXRequest(
            proxy=proxy_url,
            connect_timeout=10.0,
            read_timeout=10.0
        )
        builder.request(request)
        builder.get_updates_request(request)

  app = builder.build()
  app.add_error_handler(error_handler)
  
  global _tg_app, _status_provider, _control_handler
  if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
    log.warning("Telegram Bot Token 或 Chat ID 未配置，跳过 Telegram Bot 初始化。")
    return

  _status_provider = status_provider_func
  _control_handler = control_handler_func

  def _run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    global _tg_app
    _tg_app = (
        Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    )

    # 注册指令路由
    _tg_app.add_handler(CommandHandler("start", _cmd_start))
    _tg_app.add_handler(CommandHandler("help", _cmd_start))
    _tg_app.add_handler(CommandHandler("status", _cmd_status))
    _tg_app.add_handler(CommandHandler("report", _cmd_report))
    _tg_app.add_handler(CommandHandler("pause", _cmd_pause))
    _tg_app.add_handler(CommandHandler("resume", _cmd_resume))

    log.info("Telegram 交互与控制服务启动成功...")
    _tg_app.run_polling(drop_pending_updates=True, stop_signals=None)

  t = threading.Thread(target=_run_bot, daemon=True, name="TelegramBotThread")
  t.start()


# ═══════════════════════════════════════════════════════════════
# 3. 原有被动推送函数
# ═══════════════════════════════════════════════════════════════


def send_telegram(message: str):
  """发送异步 Telegram 消息"""
  if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
    return

  if _tg_app and _tg_app.bot:
    try:
      loop = asyncio.get_event_loop()
      if loop.is_running():
        asyncio.run_coroutine_threadsafe(
            _tg_app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="HTML",
            ),
            loop,
        )
        return
    except Exception:
      pass

  import requests

  try:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        data={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        },
        timeout=10,
    )
  except Exception as e:
    log.debug(f"Telegram 发送异常: {e}")


def notify_open(
    strategy: str,
    direction: str,
    lots: float,
    price: float,
    sl: float,
    reason: str,
):
  emoji = "📈" if direction == "BUY" else "📉"
  send_telegram(
      f"{emoji} <b>开仓 {direction}</b>\n"
      f"策略: {strategy}\n"
      f"手数: {lots}  价格: ${price:.2f}\n"
      f"止损: ${sl:.2f}\n"
      f"原因: {reason}"
  )


def notify_close(ticket: int, strategy: str, profit: float, reason: str):
  emoji = "✅" if profit >= 0 else "❌"
  send_telegram(
      f"{emoji} <b>平仓 #{ticket}</b>\n"
      f"策略: {strategy}\n"
      f"盈亏: ${profit:+.2f}\n"
      f"原因: {reason}"
  )


def notify_stop_review(daily_pnl: float):
  send_telegram(
      f"🚨🚨🚨 <b>系统已停止</b> 🚨🚨🚨\n\n"
      f"日内亏损: ${daily_pnl:.2f}\n"
      f"已达日限亏 {config.DAILY_MAX_LOSSES} 笔\n\n"
      "⚠️ 明日自动恢复"
  )


def notify_daily_report(total_pnl: float, daily_pnl: float, trade_count: int):
  emoji = "🟢" if daily_pnl >= 0 else "🔴"
  send_telegram(
      f"📊 <b>每日绩效报告</b>\n\n"
      f"{emoji} 当日盈亏: ${daily_pnl:+.2f}\n"
      f"💰 累计盈亏: ${total_pnl:+.2f}\n"
      f"📊 总交易笔数: {trade_count}\n"
      f"🛡️ 止损余量: ${config.MAX_TOTAL_LOSS + total_pnl:.2f}"
  )


def notify_system_start():
  send_telegram(
      f"🥇 <b>黄金量化系统启动</b>\n\n"
      f"品种: {config.SYMBOL}\n"
      f"风险/笔: ${config.RISK_PER_TRADE}\n"
      f"日限亏: {config.DAILY_MAX_LOSSES}笔\n"
      f"总限亏: ${config.MAX_TOTAL_LOSS}"
  )


def notify_error(error_msg: str):
  send_telegram(f"⚠️ <b>系统异常</b>\n\n{error_msg}")
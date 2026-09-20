"""
Telegram 交互与通知模块
================
1. 被动事件推送 (开仓/平仓/风控)
2. 主动命令响应 (发送 /status /report /pause /resume /start 等)
"""

import asyncio
import logging
import httpx
import httpcore
import threading
from typing import Callable, Optional

from telegram import Update
from telegram.ext import Application,ApplicationBuilder,CommandHandler, ContextTypes
from telegram.ext import ApplicationBuilder
from telegram.request import HTTPXRequest
from telegram.error import NetworkError, TelegramError

import config
import logging

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._utils.networkloop").setLevel(logging.CRITICAL)
# 全局存储应用实例与系统状态提供者/控制句柄
_tg_app: Optional[Application] = None
_status_provider: Optional[Callable[[], dict]] = None
_control_handler: Optional[Callable[[str], tuple[bool, str]]] = None
_bot_loop: Optional[asyncio.AbstractEventLoop] = None  # Bot 轮询线程真正运行的事件循环
_tg_thread: Optional[threading.Thread] = None         # 轮询线程 (全局只允许启动一次)
_tg_thread_started = False                            # 轮询线程启动保护标志 (防止重复起轮询器)

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

  if _control_handler is None:
    # 原 globals()["trader"] 兜底分支在 notifier 中永远不存在，属死代码，已移除
    await update.message.reply_text("❌ 交易对象未初始化，无法暂停。")
    return

  try:
    success, msg = _control_handler("pause")
  except Exception as e:
    log.exception(f"执行 /pause 控制回调异常: {e}")
    await update.message.reply_text(f"❌ 暂停失败: 控制回调异常 ({e})")
    return

  if success:
    await update.message.reply_html(
        f"⏸️ <b>交易系统已暂停交易！</b>\n\n{msg}\n<i>系统将继续监控市场，但<b>不会开立任何新仓位</b>。</i>"
    )
    log.info("收到 Telegram 指令：系统交易已暂停。")
  else:
    await update.message.reply_text(f"❌ 暂停失败: {msg}")


async def _cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
  """响应 /resume 指令：恢复交易"""
  if not _auth_check(update):
    return

  if _control_handler is None:
    # 原 globals()["trader"] 兜底分支在 notifier 中永远不存在，属死代码，已移除
    await update.message.reply_text("❌ 交易对象未初始化，无法恢复。")
    return

  try:
    success, msg = _control_handler("resume")
  except Exception as e:
    log.exception(f"执行 /resume 控制回调异常: {e}")
    await update.message.reply_text(f"❌ 恢复失败: 控制回调异常 ({e})")
    return

  if success:
    await update.message.reply_html(
        f"▶️ <b>交易系统已恢复交易！</b>\n\n{msg}\n<i>系统已重新开启自动下单功能。</i>"
    )
    log.info("收到 Telegram 指令：系统交易已恢复。")
  else:
    await update.message.reply_text(f"❌ 恢复失败: {msg}")


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
  global _tg_app, _status_provider, _control_handler
  global _tg_thread, _tg_thread_started

  try:
    # 1. 先校验 Token / Chat ID —— 必须放在 build() 之前。
    #    空 token 时 build() 会抛 InvalidToken，旧代码的"跳过"分支因此永远走不到。
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not token or not chat_id:
      log.warning("Telegram Bot Token 或 Chat ID 未配置，跳过 Telegram Bot 初始化。")
      return

    # 回调登记 (保持全局状态可查)
    _status_provider = status_provider_func
    _control_handler = control_handler_func

    notify_method = str(getattr(config, "NOTIFY_METHOD", "telegram")).lower()
    if notify_method != "telegram":
      log.info(f"NOTIFY_METHOD={notify_method}，跳过 Telegram 轮询，通知走 console 输出。")
      return

    if _tg_thread_started:
      log.info("Telegram 轮询线程已启动，忽略重复初始化。")
      return

    proxy_url = getattr(config, "TELEGRAM_PROXY", "http://127.0.0.1:7899")

    # 2. 只构建一个 Application：代理 + 指令路由 + 错误处理器全部注册在它上面，
    #    轮询线程 (_run_bot) 轮询的就是这一个实例，不再重复 build。
    builder = ApplicationBuilder().token(token)
    if proxy_url:
      request = HTTPXRequest(
          proxy=proxy_url,
          connect_timeout=10.0,
          read_timeout=10.0,
      )
      builder.request(request)
      builder.get_updates_request(request)
    builder.post_init(_capture_bot_loop)

    app = builder.build()
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("help", _cmd_start))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CommandHandler("report", _cmd_report))
    app.add_handler(CommandHandler("pause", _cmd_pause))
    app.add_handler(CommandHandler("resume", _cmd_resume))
    app.add_error_handler(error_handler)
    _tg_app = app

    # 3. 轮询线程只在这里启动一次 (模块导入时不再启动任何网络线程)
    _tg_thread_started = True
    _tg_thread = threading.Thread(
        target=_run_bot, daemon=True, name="TelegramBotThread"
    )
    _tg_thread.start()
    log.info("Telegram 交互与控制服务线程已启动。")
  except Exception as e:
    log.error(f"初始化 Telegram Bot 失败 (交易主流程不受影响): {e}")


async def _capture_bot_loop(application: Application) -> None:
  """post_init 回调：记录真正运行 Bot 的事件循环，供 send_telegram 跨线程投递"""
  global _bot_loop
  try:
    _bot_loop = asyncio.get_running_loop()
  except Exception as e:
    log.warning(f"记录 Telegram 事件循环失败: {e}")


def _run_bot():
  """在独立线程中轮询 init_telegram_bot 构建好的那一个 Application"""
  global _bot_loop

  app = _tg_app
  if app is None:
    log.error("Telegram Application 未构建，轮询线程退出。")
    return

  proxy_url = getattr(config, "TELEGRAM_PROXY", "http://127.0.0.1:7899")
  loop = asyncio.new_event_loop()
  asyncio.set_event_loop(loop)

  # 捕获连接异常，避免刷屏
  try:
    log.info("Telegram 交互与控制服务启动中...")
    app.run_polling(
        drop_pending_updates=True,
        stop_signals=None,
        bootstrap_retries=3,  # 尝试连接3次
    )
  except (NetworkError, httpx.ConnectError, httpcore.ConnectError) as e:
    log.error(f"❌ Telegram Bot 连接失败: 无法连接 API，请检查代理配置 (代理: {proxy_url}) | 错误: {e}")
  except TelegramError as e:
    log.error(f"❌ Telegram API 异常: {e}")
  except Exception as e:
    log.error(f"❌ Telegram 服务发生未预期错误: {e}")
  finally:
    _bot_loop = None
    try:
      if not loop.is_closed():
        loop.close()
    except Exception:
      pass


# ═══════════════════════════════════════════════════════════════
# 3. 原有被动推送函数
# ═══════════════════════════════════════════════════════════════


def _log_send_result(future):
  """异步发送完成回调：只记日志，绝不向外抛异常"""
  try:
    future.result()
    log.info("Telegram 消息已发送。")
  except Exception as e:
    log.warning(f"Telegram 消息发送失败: {e}")


def send_telegram(message: str):
  """发送 Telegram 消息 (永不抛异常, 失败只记日志)

  1) NOTIFY_METHOD == "console": 直接打印到日志
  2) 轮询线程在线: 用 asyncio.run_coroutine_threadsafe 投递到 Bot 的事件循环 (不阻塞交易线程)
  3) 否则: requests 同步兜底 (带代理, 检查 HTTP 状态码)
  """
  try:
    notify_method = str(getattr(config, "NOTIFY_METHOD", "telegram")).lower()
    if notify_method == "console":
      log.info(f"[通知-console] {message}")
      return

    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    chat_id = getattr(config, "TELEGRAM_CHAT_ID", None)
    if not token or not chat_id:
      return

    # 1) 首选：投递到 Bot 正在运行的事件循环 (交易主线程本身没有事件循环)
    loop = _bot_loop
    if _tg_app is not None and getattr(_tg_app, "bot", None) and loop is not None:
      try:
        if loop.is_running() and not loop.is_closed():
          future = asyncio.run_coroutine_threadsafe(
              _tg_app.bot.send_message(
                  chat_id=chat_id,
                  text=message,
                  parse_mode="HTML",
              ),
              loop,
          )
          future.add_done_callback(_log_send_result)
          return
      except Exception as e:
        log.warning(f"Telegram 异步投递失败，改用 HTTP 兜底: {e}")

    # 2) 兜底：同步 HTTP。bot 需要代理才能连上 Telegram API，漏掉代理必然失败。
    import requests

    proxy_url = getattr(config, "TELEGRAM_PROXY", "http://127.0.0.1:7899")
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    try:
      url = f"https://api.telegram.org/bot{token}/sendMessage"
      resp = requests.post(
          url,
          data={
              "chat_id": chat_id,
              "text": message,
              "parse_mode": "HTML",
          },
          timeout=10,
          proxies=proxies,
      )
      if resp.status_code != 200:
        log.warning(f"Telegram 发送失败 (HTTP {resp.status_code}): {resp.text[:200]}")
      else:
        log.info("Telegram 消息已通过 HTTP 兜底发送。")
    except Exception as e:
      log.warning(f"Telegram 发送异常: {e}")
  except Exception as e:
    log.warning(f"send_telegram 未预期异常 (已忽略): {e}", exc_info=True)


def notify_open(
    strategy: str,
    direction: str,
    lots: float,
    price: float,
    sl: float,
    reason: str,
):
  """开仓通知 (sl 为美元"止损距离"，不是绝对价格；任何异常都不向外抛)"""
  try:
    emoji = "📈" if str(direction or "").upper() == "BUY" else "📉"
    send_telegram(
        f"{emoji} <b>开仓 {direction}</b>\n"
        f"策略: {strategy}\n"
        f"手数: {_safe_float(lots)}  价格: ${_safe_float(price):.2f}\n"
        f"止损距离: ${_safe_float(sl):.2f}\n"
        f"原因: {reason}"
    )
  except Exception as e:
    log.warning(f"发送开仓通知失败 (交易逻辑不受影响): {e}", exc_info=True)


def notify_close(ticket: int, strategy: str, profit: float, reason: str):
  try:
    pnl = _safe_float(profit)
    emoji = "✅" if pnl >= 0 else "❌"
    send_telegram(
        f"{emoji} <b>平仓 #{ticket}</b>\n"
        f"策略: {strategy}\n"
        f"盈亏: ${pnl:+.2f}\n"
        f"原因: {reason}"
    )
  except Exception as e:
    log.warning(f"发送平仓通知失败 (交易逻辑不受影响): {e}", exc_info=True)


def notify_stop_review(daily_pnl: float):
  try:
    send_telegram(
        f"🚨🚨🚨 <b>系统已停止</b> 🚨🚨🚨\n\n"
        f"日内亏损: ${_safe_float(daily_pnl):.2f}\n"
        f"已达日限亏 {getattr(config, 'DAILY_MAX_LOSSES', 3)} 笔\n\n"
        "⚠️ 明日自动恢复"
    )
  except Exception as e:
    log.warning(f"发送停止复核通知失败 (交易逻辑不受影响): {e}", exc_info=True)


def notify_daily_report(total_pnl: float, daily_pnl: float, trade_count: int):
  try:
    total = _safe_float(total_pnl)
    daily = _safe_float(daily_pnl)
    emoji = "🟢" if daily >= 0 else "🔴"
    send_telegram(
        f"📊 <b>每日绩效报告</b>\n\n"
        f"{emoji} 当日盈亏: ${daily:+.2f}\n"
        f"💰 累计盈亏: ${total:+.2f}\n"
        f"📊 总交易笔数: {trade_count}\n"
        f"🛡️ 止损余量: ${_safe_float(getattr(config, 'MAX_TOTAL_LOSS', 0.0)) + total:.2f}"
    )
  except Exception as e:
    log.warning(f"发送每日报告失败 (交易逻辑不受影响): {e}", exc_info=True)


def notify_system_start():
  try:
    send_telegram(
        f"🥇 <b>黄金量化系统启动</b>\n\n"
        f"品种: {getattr(config, 'SYMBOL', 'XAUUSD')}\n"
        f"风险/笔: ${_safe_float(getattr(config, 'RISK_PER_TRADE', 0.0))}\n"
        f"日限亏: {getattr(config, 'DAILY_MAX_LOSSES', 3)}笔\n"
        f"总限亏: ${_safe_float(getattr(config, 'MAX_TOTAL_LOSS', 0.0))}"
    )
  except Exception as e:
    log.warning(f"发送启动通知失败 (交易逻辑不受影响): {e}", exc_info=True)


def notify_error(error_msg: str):
  """异常通知 (当前无调用方，保留定义；保证自身永不抛异常)"""
  try:
    send_telegram(f"⚠️ <b>系统异常</b>\n\n{error_msg}")
  except Exception as e:
    log.warning(f"发送异常通知失败 (已忽略): {e}", exc_info=True)
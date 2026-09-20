"""
黄金量化交易系统 — 配置文件
============================
所有参数集中管理，修改这里即可
"""
from pathlib import Path

# ============================================================
# MT4 连接配置
# ============================================================
# MT4 数据文件夹路径 (在MT4中 File → Open Data Folder 获取)
# 例如: C:\Users\hlin2\AppData\Roaming\MetaQuotes\Terminal\XXXXXXXX
METATRADER_DIR_PATH = r"C:\Users\kilimy\AppData\Roaming\MetaQuotes\Terminal\AB75DD8A03E8CC693E1336EB0D50BA2D"

# MT4 文件桥接目录 (EA和Python通过这个目录通信)
BRIDGE_DIR = Path(METATRADER_DIR_PATH) / "MQL4" / "Files" / "DWX"

# ============================================================
# 交易账户参数
# ============================================================
SYMBOL = "XAUUSD"      # EMX Pro Limited 的黄金品种名称
CAPITAL = 1000            # 本金 (USD)
MAX_TOTAL_LOSS = 100     # 最大总亏损 (USD)，达到后停止交易
LOT_SIZE = 0.01           # 手数 (0.03手 = 3盎司)
# 每点价值: 0.03手 × $100/点/标准手 = $3/点 (价格每变动$1 = 盈亏$3)
POINT_VALUE_PER_LOT = 100  # 标准手每点价值 ($100/点)
MAX_POSITIONS = 3         # 最大同时持仓数
STOP_LOSS_PIPS = 5       # 默认止损距离 ($20 = 0.03手亏$60)
MAGIC_NUMBER = 20260325   # EA魔术号 (区分手动单和策略单)
SLIPPAGE = 3              # 最大滑点 (点)
DAILY_MAX_LOSS = 50     # 单日最大亏损金额 (已改用笔数控制，此项保留作极端保护)
DAILY_MAX_LOSSES = 5      # 单日最大亏损笔数 (达到后停止交易，回测Sharpe 0.84→2.84)
COOLDOWN_BARS = 1        # 止损后冷却期 (3根H1 K线 = 3小时)

# ── ATR自动调仓 ──
RISK_PER_TRADE = 25       # 每笔交易最大风险金额 (2.5%×$1000=$50)
AUTO_LOT_SIZING = True    # 是否启用ATR自动调仓 (True=根据ATR调整手数, False=固定LOT_SIZE)
MIN_LOT_SIZE = 0.01       # 最小手数
MAX_LOT_SIZE = 0.02       # 最大手数 (本金$2000, 保守控制)

# ── ORB策略参数 ──
# 注意: 策略开关只有唯一一个真值来源 → 下面的 STRATEGIES[name]["enabled"]
#       这里不再设置 ORB_ENABLED 之类的重复开关 (历史bug: 两个开关不一致,
#       改了一个另一个仍是True, 导致策略被禁用后照旧开单)
ORB_NY_OPEN_HOUR_UTC = 14         # 纽约开盘时间 UTC (14:30 = 纽约9:30, 用14近似)
# ⚠️ EA写入的K线时间戳是【经纪商服务器时间】, 不是UTC (MQL4的iTime/TimeCurrent都是服务器时间)。
#    本机实测(2026-09-20): EA心跳写的是 18:50:37, 而当时 UTC 是 15:50:58
#    → 服务器 = UTC+3, 所以这里必须填 3 (填0会让ORB提前3小时判定"纽约开盘")。
#    核对方法: EA写出的 heartbeat.json 里 timestamp 减去 当前UTC时间。
MT4_SERVER_UTC_OFFSET_HOURS = 3
ORB_RANGE_MINUTES = 15            # 开盘后前15分钟的高低点作为区间
ORB_EXPIRY_MINUTES = 120          # 突破窗口有效期 (2小时)
# 注: ORB 的止损止盈已统一改为 risk 模块的动态ATR口径, 下面两个旧参数不再生效
ORB_SL_MULTIPLIER = 0.75          # (已废弃) 原为 止损 = 0.75 × 区间宽度
ORB_TP_MULTIPLIER = 3.0           # (已废弃) 原为 止盈 = 3.0 × 区间宽度

# ============================================================
# M15 RSI 策略
# ============================================================
# ⚠️ 回测结论 (research/backtest_m15rsi.py, 2024-01 → 2025-01, 约1283笔):
#    毛利为正(+$282), 但点差成本 -$590, 净亏 -$309。
#    病因是【交易频率过高】: 一年约1300笔, 每笔成本$0.46。
#    敏感度测试(最小开仓间隔):
#        20分钟(原值) → 净 -$300
#        60分钟       → 净 -$269
#        180分钟      → 净   -$6   ← 基本打平
#    所以这个旋钮是M15 RSI能否盈利的关键。
#    当前已设为 180 分钟 (基本打平); 若仍持续为负, 就该考虑停用该策略。
M15_RSI_COOLDOWN_MINUTES = 180

# 趋势对齐过滤 (远端旧版 8846c92 原本就有, 本地重写时丢失):
#   True  -> 只在 SMA50 的顺势一侧做均值回归 (做多要求价格>SMA50, 做空要求价格<SMA50)
#   False -> 任意位置超卖就做多 / 超买就做空 (本地曾经的行为)
# 回测(research/ab_remote_vs_local.py, 2024-01->2025-01)对比:
#   False: 1181笔, 净 -$488, 每笔R -0.035, 回撤 -$674  <- 空头单独亏 -$706 (牛市逆势抄顶)
#   True :  434笔, 净   -$5, 每笔R -0.006, 回撤 -$173  <- 空头只亏 -$92
# 叠加 180 分钟间隔后: 净 +$16.5 (唯一转正的组合)
M15_RSI_TREND_FILTER = True

# ============================================================
# 动态止盈止损 (趋势型策略: keltner / macd / orb)
# ============================================================
# 口径: 【结构位为主 + ATR自适应为辅】
#   止损 = 前N根已收盘K线低点(多)/高点(空) 外侧 + ATR缓冲, 再用ATR上下限夹住
#   止盈 = 优先取结构目标(前N根K线高点/低点, 或ORB的开盘区间宽度);
#          结构目标不成立或盈亏比太差时, 退回 ATR 倍数
#   下单后由保本/移动止损接管 (见下方 TRAILING 段)
#
# 背景: 原实现用固定倍数(止损2.5×ATR / 止盈3.0×ATR)实测过高 —— 10笔趋势单里
#       **0笔**触及止盈 (设定止盈中位$51.5, 而24根H1内最大有利波动MFE中位仅$26.4)。

# ── 结构位 ──
DYN_STRUCT_LOOKBACK = 20       # 回看多少根【已收盘】K线找结构
DYN_STRUCT_WING = 2            # 摆动点判定半宽: 低点在其左右各2根内最低才算摆动低点
DYN_STRUCT_BUFFER_ATR = 0.3    # 结构位外再留 0.3×ATR 缓冲, 防插针扫损
DYN_TP_MIN_RR = 0.8            # 结构止盈至少要达到 0.8×止损距离, 否则改用ATR兜底

# ── ATR 自适应 (兼作兜底与上下限) ──
DYN_SL_ATR_MULTIPLIER = 2.5    # 结构不可用时的止损兜底 = 2.5×ATR
# 止盈兜底: 3.0×ATR。回测(最近180天/365天, 见 research/backtest_recent.py)显示
# 把止盈从3.0×ATR收紧到2.0×ATR 会让每笔R从 +0.212 掉到 +0.121 ——
# 止盈"很少被触及"并不等于它有害, 它只是不干扰趋势奔跑, 真正的出场靠时间止损。
DYN_TP_ATR_MULTIPLIER = 3.0    # 止盈 = 3.0×ATR (宽止盈, 基本不触发, 出场靠时间止损)
# 是否用"结构目标(前N根极值)"做止盈。默认 False —— 回测显示结构止盈通常比
# 3.0×ATR更近, 会把每笔R从 +0.212 压到 +0.123。结构位只用于【止损】。
DYN_TP_USE_STRUCTURE = False
DYN_SL_MIN_ATR = 0.5           # 止损至少 0.5×ATR (太近必被扫)
DYN_SL_MAX_ATR = 2.0           # 止损最多 2.0×ATR (硬上限)
DYN_TP_MAX_ATR = 3.0           # 止盈最多 3.0×ATR (硬上限)
DYN_SL_MIN = 8.0               # 止损绝对下限 ($)
DYN_SL_MAX = 60.0              # 止损绝对上限 ($)
DYN_TP_MIN = 4.0               # 止盈绝对下限 ($)
# 止盈绝对上限必须给得足够宽。旧口径的止盈 3.0×ATR 是【不设绝对上限】的(实测平均$62.9),
# 如果这里卡成40, 等于偷偷把止盈收紧了 —— 回测显示每笔R会从 +0.212 掉到 +0.119。
# 真正起约束作用的是 DYN_TP_MAX_ATR(3.0×ATR); 这里只做极端保护。
DYN_TP_MAX = 150.0

# ── 波动率自适应: ATR 相对自身均值的比值决定"行情能走多远" ──
DYN_VOL_LOOKBACK = 50          # 回看根数
DYN_VOL_LOW_RATIO = 0.8        # ATR < 0.8×均值 → 波动收缩
DYN_VOL_HIGH_RATIO = 1.25      # ATR > 1.25×均值 → 波动扩张
DYN_LOW_VOL_TP_FACTOR = 0.8    # 收缩时止盈再收紧 (走得快, 不贪)
DYN_HIGH_VOL_TP_FACTOR = 1.2   # 扩张时止盈放宽 (给出空间)

# ── 保本 / 移动止损 ──
# ⚠️ 这里的参数是回测出来的关键项, 不要凭感觉收紧:
#    最近的180天回测显示, 跟踪止损设得太紧会把优势吃掉2/3 ——
#     保本0.6×ATR/跟踪0.8×ATR(距离0.4) → 每笔R +0.071
#     保本2.0×ATR/跟踪3.0×ATR(距离2.0) → 每笔R +0.212  ← 当前设置
#     完全不用跟踪止损                    → 每笔R +0.214
#    即: 在H1黄金上, 0.4×ATR(≈$7)的跟踪距离会被正常噪音反复扫掉。
#    当前设置下跟踪止损只在极端有利行情里充当"灾难棘轮", 基本不干预趋势。
TRAILING_ENABLED = True
BREAKEVEN_TRIGGER_ATR = 2.0    # 浮盈达 2.0×ATR → 止损移到保本
BREAKEVEN_BUFFER = 0.30        # 保本时留出的缓冲 ($, 覆盖点差)
TRAIL_START_ATR = 3.0          # 浮盈达 3.0×ATR → 开始跟踪
TRAIL_DISTANCE_ATR = 2.0       # 跟踪距离 = 2.0×ATR
TRAIL_MIN_STEP = 0.5           # 止损至少移动$0.5才发MODIFY (避免刷单)

# ============================================================
# 策略参数
# ============================================================
# ★ 策略开关的唯一真值来源 ★
#   这是控制策略开/关的唯一位置。signals.scan_all_signals() 会逐个读取
#   这里的 enabled 字段; 代码里不再有第二套开关 (旧版的 ORB_ENABLED 已删除)。
#   每个策略都必须在本字典里有一条记录, 否则会被当作"未启用"而永远不交易。
STRATEGIES = {
    "keltner": {
        "enabled": True,
        "name": "Keltner通道突破",
        "timeframe": "H1",
        "stop_loss": 20,
        "take_profit": 35,
        "max_hold_bars": 15,
    },
    "macd": {
        "enabled": True,
        "name": "MACD+EMA100趋势",
        "timeframe": "H1",
        "stop_loss": 20,
        "take_profit": 50,
        "max_hold_bars": 20,
    },
    "orb": {
        "enabled": True,
        "name": "NY开盘区间突破",
        "timeframe": "H1",
        "max_hold_bars": 6,  # v5优化: 8→6根K线 (~6小时, Sharpe 1.31→1.51)
    },
    "m15_rsi": {
        "enabled": True,
        "name": "M15 RSI均值回归",
        "timeframe": "M15",
        "stop_loss": 10,
        "take_profit": 0,    # 用RSI极值出场, 不用固定止盈
        "max_hold_bars": 12,  # 12根M15 K线 = 3小时
    },
}

# ============================================================
# 扫描频率
# ============================================================
SCAN_INTERVAL_SECONDS = 30    # 每30秒扫描一次 (M15策略)
SIGNAL_CHECK_TIMEFRAME = "MULTI"  # 多时间框架: H1 + M15

# ============================================================
# 通知
# ============================================================
NOTIFY_METHOD = "telegram"     # "console" | "telegram"
TELEGRAM_BOT_TOKEN = "8819242532:AAFqK5iaFDfqffgTGHopsZMIM-rBbOAwQkw"
TELEGRAM_CHAT_ID = "2093450740"

# ============================================================
# 路径
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

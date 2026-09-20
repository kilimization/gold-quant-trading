//+------------------------------------------------------------------+
//|                                                  WhaleFlow_EA.mq4 |
//|                          巨鲸 / 聪明钱 图表标注 (数据来自 Python)  |
//+------------------------------------------------------------------+
//  数据来源: whale_collector.py 抓取 Binance 合约大单+顶级持仓、CFTC COT、
//            GLD 资金流，写入 MQL4\Files\DWX\whale_flow.json；
//            本 EA 只负责读文件 + 在图上标注，不联网、不下单。
//
//  图上会画出:
//    1) 吸筹/派发价位带  —— 巨鲸大单按价格聚类的净买卖区(绿=吸筹 红=派发)
//    2) 巨鲸大单标记      —— 单笔超过门槛的成交，落在对应K线的价格上
//    3) 聪明钱仪表盘      —— 顶级持仓多空比 vs 散户多空比、持仓量变化、资金费率
//    4) 黄金 COT          —— 管理基金净持仓 + COT 指数(拥挤度) 极值提示
//    5) 可选弹窗提醒      —— 出现超过门槛的新巨鲸单时 Alert
//
//  说明: 图表品种名需与 JSON 里的 BTCUSD / ETHUSD 对应(自动兼容 BTCUSD. / BTCUSDm 等后缀)，
//        黄金用 gold 段(品种名含 XAU 时)。
//+------------------------------------------------------------------+
#property copyright "Copyright 2026"
#property link      "https://www.mql5.com"
#property version   "1.00"
#property strict

//--- 输入参数
input string InpFileName        = "whale_flow.json"; // 数据文件名(位于 MQL4\Files\DWX\)
input string InpBridgeSubdir    = "DWX";             // 桥接子目录
input int    InpRefreshSeconds  = 3;                 // 刷新间隔(秒)
input bool   InpShowZones       = true;              // 显示吸筹/派发价位带
input bool   InpShowWhales      = true;              // 显示巨鲸大单标记
input bool   InpShowPanel       = true;              // 显示仪表盘
input int    InpZoneBars        = 30;                // 价位带向右延伸的K线数
input int    InpWhaleBarsBack   = 240;               // 巨鲸标记最多回溯多少根当前周期K线
input double InpAlertMinUSD     = 500000;            // 弹窗提醒的单笔金额门槛(USD, 0=不提醒)
input bool   InpAlertPopup      = false;             // 是否弹窗(Alert)，默认只打日志
input color  InpAccumColor      = clrLimeGreen;      // 吸筹颜色
input color  InpDistColor       = clrTomato;         // 派发颜色
input int    InpPanelX          = 14;                // 面板左上角X
input int    InpPanelY          = 24;                // 面板左上角Y

//--- 常量
#define WF_PREFIX      "WF_"
#define WF_MAX_ZONES   8
#define WF_MAX_WHALES  60
#define WF_ROWS        22
#define WF_PANEL_W     470
#define WF_PANEL_H     26
#define WF_ROW_H       16

//--- 状态
string   g_filePath      = "";
string   g_lastRaw       = "";
datetime g_lastLoad      = 0;
datetime g_lastGoodLoad  = 0;
int      g_loadErrors    = 0;
datetime g_zoneRightTime = 0;

//--- JSON 解析结果
string   g_symbol        = "";
double   g_refPrice      = 0;
double   g_walletPrice   = 0;
int      g_windowMin     = 0;
int      g_tradesUsed    = 0;
double   g_flowBuy       = 0, g_flowSell = 0, g_flowNet = 0, g_flowRatio = 0;

int      g_zoneCount     = 0;
double   g_zonePrice[WF_MAX_ZONES], g_zoneLow[WF_MAX_ZONES], g_zoneHigh[WF_MAX_ZONES];
double   g_zoneNet[WF_MAX_ZONES];
int      g_zoneStrength[WF_MAX_ZONES];
string   g_zoneKind[WF_MAX_ZONES];

//--- 配色: 文字颜色按图表背景自动取黑/白，避免"字和背景一个颜色"看不清
color    g_chartText  = clrWhite;   // 图上文字色(按图表背景自动改，见 RefreshColors)
color    g_panelText  = clrWhite;   // 面板普通文字色
color    g_accumText  = clrLime;    // 面板里的吸筹文字色
color    g_distText   = clrTomato;  // 面板里的派发文字色
color    g_hiText     = clrGold;    // 面板里的强调文字色

int      g_whaleCount    = 0;
long     g_whaleTs[WF_MAX_WHALES];
double   g_whalePrice[WF_MAX_WHALES];
double   g_whaleUsd[WF_MAX_WHALES];
string   g_whaleSide[WF_MAX_WHALES];
long     g_whaleMaxTs    = 0;

double   g_smartLongPct  = 0, g_smartRatio = 0, g_topAccountRatio = 0, g_retailRatio = 0;
double   g_oiUsd         = 0, g_oiChangePct = 0;
double   g_fundingPct    = 0, g_walletMark = 0;

string   g_cotDate       = "";
long     g_cotNet        = 0, g_cotNetChange = 0;
double   g_cotIndex      = -1;
double   g_cotAgeDays    = 0;
int      g_cotWeeks      = 0;
string   g_cotExtreme    = "";
double   g_gldPrice      = 0, g_gldChgPct = 0, g_gldVolRatio = 0;

//+------------------------------------------------------------------+
//| 小工具                                                            |
//+------------------------------------------------------------------+
string Num0(double v) { return DoubleToString(v, 0); }

string CompactNumUsd(double v)
{
   double a = MathAbs(v);
   string sign = (v < 0) ? "-" : "+";
   if(a >= 1e9) return StringFormat("%s$%.2fB", sign, a / 1e9);
   if(a >= 1e6) return StringFormat("%s$%.2fM", sign, a / 1e6);
   if(a >= 1e3) return StringFormat("%s$%.0fK", sign, a / 1e3);
   return StringFormat("%s$%.0f", sign, a);
}

string Money0(double v)
{
   // 千分位整数金额
   string s = StringFormat("%.0f", MathAbs(v));
   string out = "";
   int n = StringLen(s);
   for(int i = 0; i < n; i++)
   {
      if(i > 0 && ((n - i) % 3) == 0) out = out + ",";
      out = out + StringSubstr(s, i, 1);
   }
   return ((v < 0) ? "-" : "+") + out;
}

//+------------------------------------------------------------------+
//| 取某个子对象 {...} 的起止位置(不含花括号). 失败返回 false          |
//+------------------------------------------------------------------+
bool FindObjectRange(string s, string key, int from, int &b0, int &b1, string &inside)
{
   b0 = -1; b1 = -1; inside = "";
   string pat = "\"" + key + "\":{";
   int p = StringFind(s, pat, from);
   if(p < 0) return false;
   int start = p + StringLen(pat);      // '{' 之后
   int depth = 1;
   int i = start;
   int n = StringLen(s);
   while(i < n)
   {
      string ch = StringSubstr(s, i, 1);
      if(ch == "{") depth++;
      else if(ch == "}")
      {
         depth--;
         if(depth == 0)
         {
            b0 = start;
            b1 = i - 1;                   // '}' 之前
            inside = StringSubstr(s, start, i - start);
            return true;
         }
      }
      i++;
   }
   return false;
}

//+------------------------------------------------------------------+
//| 字符串查找(找不到返回 -1)                                         |
//+------------------------------------------------------------------+
int StrFind(string src, string needle, int start)
{
   int pos = StringFind(src, needle, start);
   return pos;
}

//+------------------------------------------------------------------+
//| 在 json 里取 "key" 之后的数字                                     |
//+------------------------------------------------------------------+
double JNum(string json, string key, double def, int from)
{
   string pat = "\"" + key + "\":";
   int p = StringFind(json, pat, from);
   if(p < 0) return def;
   p += StringLen(pat);
   int n = StringLen(json);
   string buf = "";
   while(p < n)
   {
      string ch = StringSubstr(json, p, 1);
      if(ch == "-" || ch == "+" || ch == "." || (ch >= "0" && ch <= "9") || ch == "e" || ch == "E")
      {
         buf = buf + ch;
         p++;
      }
      else break;
   }
   if(buf == "") return def;
   return StrToDouble(buf);
}

string JStr(string json, string key, string def, int from)
{
   string pat = "\"" + key + "\":\"";
   int p = StringFind(json, pat, from);
   if(p < 0) return def;
   p += StringLen(pat);
   int q = StringFind(json, "\"", p);
   if(q < 0) return def;
   return StringSubstr(json, p, q - p);
}

int JBool(string json, string key, int def, int from)
{
   string pat = "\"" + key + "\":";
   int p = StringFind(json, pat, from);
   if(p < 0) return def;
   p += StringLen(pat);
   string v = StringSubstr(json, p, 5);
   if(StringFind(v, "true") == 0) return 1;
   if(StringFind(v, "false") == 0) return 0;
   return def;
}

//+------------------------------------------------------------------+
//| 找到某 key 对应的数组，返回 '[' 的位置                            |
//+------------------------------------------------------------------+
int FindArray(string json, string key, int from)
{
   string pat = "\"" + key + "\":[";
   int p = StringFind(json, pat, from);
   if(p < 0) return -1;
   return p + StringLen(pat) - 1;   // 指向 '['
}

//+------------------------------------------------------------------+
//| 从 '[' 之后取第 n 个对象(0-based)，返回对象内含 { } 的字符串      |
//+------------------------------------------------------------------+
bool NthObject(string json, int arrPos, int index, string &obj)
{
   int p = arrPos + 1;              // 跳过 '['
   int depth = 0;
   int found = 0;
   int n = StringLen(json);
   int start = -1;
   while(p < n)
   {
      string ch = StringSubstr(json, p, 1);
      if(ch == "{")
      {
         if(depth == 0) start = p;
         depth++;
      }
      else if(ch == "}")
      {
         depth--;
         if(depth == 0 && start >= 0)
         {
            if(found == index)
            {
               obj = StringSubstr(json, start + 1, p - start - 1);
               return true;
            }
            found++;
            start = -1;
         }
      }
      else if(ch == "]" && depth == 0)
         return false;
      p++;
   }
   return false;
}

//+------------------------------------------------------------------+
//| 读整个文件                                                        |
//+------------------------------------------------------------------+
bool ReadDataFile(string &raw)
{
   raw = "";
   ResetLastError();
   int h = FileOpen(g_filePath, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE)
   {
      g_loadErrors++;
      if(g_loadErrors == 1 || (g_loadErrors % 60) == 0)
         Print("无法打开 ", g_filePath, " 错误码: ", GetLastError(),
               " (采集脚本 whale_collector.py 是否在运行?)");
      return false;
   }
   int size = (int)FileSize(h);
   if(size <= 0)
   {
      FileClose(h);
      return false;
   }
   uchar buf[];
   ArrayResize(buf, size);
   FileReadArray(h, buf, 0, size);
   FileClose(h);

   // 按 UTF-8 解码(JSON 里没有中文，直接用 CharArrayToString 亦可)
   raw = "";
   string part = "";
   for(int i = 0; i < size; i++)
   {
      part = part + CharToString(buf[i]);
      if(StringLen(part) >= 512)
      {
         raw = raw + part;
         part = "";
      }
   }
   raw = raw + part;
   return (StringLen(raw) > 10);
}

//+------------------------------------------------------------------+
//| 解析 JSON                                                         |
//+------------------------------------------------------------------+
void ResetParsed()
{
   g_symbol = "";
   g_refPrice = 0; g_walletPrice = 0; g_windowMin = 0; g_tradesUsed = 0;
   g_flowBuy = 0; g_flowSell = 0; g_flowNet = 0; g_flowRatio = 0;
   g_zoneCount = 0; g_whaleCount = 0;
   g_smartLongPct = 0; g_smartRatio = 0; g_topAccountRatio = 0; g_retailRatio = 0;
   g_oiUsd = 0; g_oiChangePct = 0; g_fundingPct = 0; g_walletMark = 0;
   g_cotDate = ""; g_cotNet = 0; g_cotNetChange = 0; g_cotIndex = -1;
   g_cotAgeDays = 0;
   g_cotWeeks = 0; g_cotExtreme = "";
   g_gldPrice = 0; g_gldChgPct = 0; g_gldVolRatio = 0;
}

// 图表品种与 JSON 里的品种名匹配(BTCUSD / BTCUSD. / BTCUSDm / #BTCUSD 等)
bool SymbolMatches(string jsonSymbol)
{
   if(jsonSymbol == "") return false;
   string a = _Symbol;
   StringToUpper(a);
   StringReplace(a, ".", "");
   StringReplace(a, "#", "");
   StringReplace(a, "_", "");
   StringReplace(a, "-", "");
   string b = jsonSymbol;
   StringToUpper(b);
   StringReplace(b, ".", "");
   StringReplace(b, "#", "");
   StringReplace(b, "_", "");
   StringReplace(b, "-", "");
   return (StringFind(a, b) >= 0);
}

bool IsGoldChart()
{
   string s = _Symbol;
   StringToUpper(s);
   return (StringFind(s, "XAU") >= 0);
}

bool ParseData(string raw)
{
   ResetParsed();

   // ---- crypto 数组 ----
   int cryptoArr = FindArray(raw, "crypto", 0);
   int goldPos   = StringFind(raw, "\"gold\":{", 0);
   if(cryptoArr >= 0)
   {
      int idx = 0;
      string obj = "";
      while(NthObject(raw, cryptoArr, idx, obj))
      {
         string sym = JStr(obj, "symbol", "", 0);
         if(SymbolMatches(sym))
         {
            g_symbol      = sym;
            g_refPrice    = JNum(obj, "last_price", 0, 0);
            g_windowMin   = (int)JNum(obj, "window_minutes", 0, 0);
            g_tradesUsed  = (int)JNum(obj, "trades_used", 0, 0);

            // 注意: zones 里也有 buy_usd/sell_usd/net_usd，所以必须限定在子对象内解析
            int b0 = -1, b1 = -1;
            string sub = "";

            if(FindObjectRange(obj, "flow_2h", 0, b0, b1, sub))
            {
               g_flowBuy   = JNum(sub, "buy_usd", 0, 0);
               g_flowSell  = JNum(sub, "sell_usd", 0, 0);
               g_flowNet   = JNum(sub, "net_usd", 0, 0);
               g_flowRatio = JNum(sub, "ratio", 0, 0);
            }
            if(FindObjectRange(obj, "smart", 0, b0, b1, sub))
            {
               g_smartLongPct = JNum(sub, "long_pct", 0, 0);
               g_smartRatio   = JNum(sub, "ratio", 0, 0);
            }
            if(FindObjectRange(obj, "top_account", 0, b0, b1, sub))
               g_topAccountRatio = JNum(sub, "ratio", 0, 0);
            if(FindObjectRange(obj, "retail", 0, b0, b1, sub))
               g_retailRatio = JNum(sub, "ratio", 0, 0);
            if(FindObjectRange(obj, "oi", 0, b0, b1, sub))
            {
               g_oiUsd       = JNum(sub, "oi_usd", 0, 0);
               g_oiChangePct = JNum(sub, "oi_change_pct", 0, 0);
            }
            if(FindObjectRange(obj, "funding", 0, b0, b1, sub))
            {
               g_walletMark = JNum(sub, "mark_price", 0, 0);
               g_fundingPct = JNum(sub, "funding_rate_pct", 0, 0);
            }

            // zones
            int zArr = FindArray(obj, "zones", 0);
            if(zArr >= 0)
            {
               int zi = 0;
               string zo = "";
               while(zi < WF_MAX_ZONES && NthObject(obj, zArr, zi, zo))
               {
                  g_zonePrice[zi]    = JNum(zo, "price", 0, 0);
                  g_zoneLow[zi]      = JNum(zo, "low", 0, 0);
                  g_zoneHigh[zi]     = JNum(zo, "high", 0, 0);
                  g_zoneNet[zi]      = JNum(zo, "net_usd", 0, 0);
                  g_zoneStrength[zi] = (int)JNum(zo, "strength", 0, 0);
                  g_zoneKind[zi]     = JStr(zo, "kind", "", 0);
                  zi++;
               }
               g_zoneCount = zi;
            }

            // whales
            int wArr = FindArray(obj, "whales", 0);
            if(wArr >= 0)
            {
               int wi = 0;
               string wo = "";
               while(wi < WF_MAX_WHALES && NthObject(obj, wArr, wi, wo))
               {
                  g_whaleTs[wi]    = (long)JNum(wo, "ts", 0, 0);
                  g_whalePrice[wi] = JNum(wo, "price", 0, 0);
                  g_whaleUsd[wi]   = JNum(wo, "usd", 0, 0);
                  g_whaleSide[wi]  = JStr(wo, "side", "", 0);
                  wi++;
               }
               g_whaleCount = wi;
            }
            break;
         }
         idx++;
         if(idx > 20) break;
      }
   }

   // ---- gold 段(图表是 XAU 时使用) ----
   if(IsGoldChart() && goldPos > 0)
   {
      int g0 = -1, g1 = -1;
      string gsub = "";
      if(FindObjectRange(raw, "cot", goldPos, g0, g1, gsub))
      {
         g_cotDate      = JStr(gsub, "report_date", "", 0);
         g_cotNet       = (long)JNum(gsub, "net", 0, 0);
         g_cotNetChange = (long)JNum(gsub, "net_change", 0, 0);
         g_cotIndex     = JNum(gsub, "cot_index", -1, 0);
         g_cotAgeDays   = JNum(gsub, "age_days", 0, 0);
         g_cotWeeks     = (int)JNum(gsub, "history_weeks", 0, 0);
         g_cotExtreme   = JStr(gsub, "extreme", "", 0);
      }
      if(FindObjectRange(raw, "gld", goldPos, g0, g1, gsub))
      {
         g_gldPrice    = JNum(gsub, "last", 0, 0);
         g_gldChgPct   = JNum(gsub, "chg_pct", 0, 0);
         g_gldVolRatio = JNum(gsub, "volume_vs_avg20", 0, 0);
      }
      g_refPrice = JNum(raw, "ref_price", 0, goldPos);
   }

   g_zoneRightTime = Time[0];
   return (g_symbol != "" || IsGoldChart());
}

//+------------------------------------------------------------------+
//| 对象管理                                                          |
//+------------------------------------------------------------------+
void DeleteOurObjects()
{
   int total = ObjectsTotal();
   for(int i = total - 1; i >= 0; i--)
   {
      string nm = ObjectName(i);
      if(StringFind(nm, WF_PREFIX) == 0) ObjectDelete(nm);
   }
}

#define WF_PANEL_TEXT_BG C'22,24,30'   // 面板底色(比文字亮/暗关系固定，保证对比)

//+------------------------------------------------------------------+
//| 颜色工具: 用相对亮度算对比度，自动挑"看得清"的字色               |
//+------------------------------------------------------------------+
double ColorLuma(color c)
{
   int r = (int)c & 0xFF;          // MT4 的 color 是 BGR 排列: 低字节=红
   int g = ((int)c >> 8) & 0xFF;
   int b = ((int)c >> 16) & 0xFF;
   return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0;
}

double ContrastRatio(double l1, double l2)
{
   double a = MathMax(l1, l2);
   double b = MathMin(l1, l2);
   return (a + 0.05) / (b + 0.05);
}

// 保证 clr 与背景 bg 的对比度不低于 minRatio；不够时按色相往亮处推
color MakeContrast(color clr, color bg, double minRatio)
{
   double lb = ColorLuma(bg);
   if(ContrastRatio(ColorLuma(clr), lb) >= minRatio) return clr;

   int r = (int)clr & 0xFF;
   int g = ((int)clr >> 8) & 0xFF;
   int b = ((int)clr >> 16) & 0xFF;
   int best = (int)clr;
   for(int step = 1; step <= 4; step++)
   {
      int nr = r + (255 - r) * step / 4;
      int ng = g + (255 - g) * step / 4;
      int nb = b + (255 - b) * step / 4;
      color cand = (color)(nr | (ng << 8) | (nb << 16));
      if(ContrastRatio(ColorLuma(cand), lb) >= minRatio) return cand;
      best = (int)cand;
   }
   // 往亮处推也不够(背景很亮) → 改用黑
   if(ContrastRatio(ColorLuma(clrBlack), lb) > ContrastRatio(ColorLuma((color)best), lb)) return clrBlack;
   return (color)best;
}

// 白/黑二选一，取在该背景上更清楚的那个
color ReadableOn(color bg)
{
   double lb = ColorLuma(bg);
   return (ContrastRatio(ColorLuma(clrWhite), lb) >= ContrastRatio(ColorLuma(clrBlack), lb))
          ? clrWhite : clrBlack;
}

//+------------------------------------------------------------------+
//| 根据图表背景刷新整套配色(每次重画前调用一次)                      |
//+------------------------------------------------------------------+
void RefreshColors()
{
   color chartBg = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);
   double lb = ColorLuma(chartBg);

   g_chartText  = (lb > 0.5) ? clrBlack : clrWhite;
   // 底衬取与背景相反的方向，保证底衬上的字一定看得清

   g_panelText = MakeContrast(clrWhite, WF_PANEL_TEXT_BG, 4.5);
   g_accumText = MakeContrast(InpAccumColor, WF_PANEL_TEXT_BG, 4.5);
   g_distText  = MakeContrast(InpDistColor,  WF_PANEL_TEXT_BG, 4.5);
   g_hiText    = MakeContrast(clrGold,       WF_PANEL_TEXT_BG, 4.5);
}

void SetLabel(string name, int x, int y, string text, color clr, int size)
{
   // 对比度不足时自动向亮处校正，避免"字体与背景颜色一致"看不清
   color use = MakeContrast(clr, WF_PANEL_TEXT_BG, 3.2);
   if(ObjectFind(name) < 0)
      ObjectCreate(name, OBJ_LABEL, 0, 0, 0);
   ObjectSet(name, OBJPROP_CORNER, 0);
   ObjectSet(name, OBJPROP_XDISTANCE, x);
   ObjectSet(name, OBJPROP_YDISTANCE, y);
   ObjectSetText(name, text, size);
   ObjectSet(name, OBJPROP_COLOR, use);
   ObjectSet(name, OBJPROP_FONTSIZE, size);
   ObjectSet(name, OBJPROP_SELECTABLE, false);
   ObjectSet(name, OBJPROP_HIDDEN, true);
}

void SetHLine(string name, double price, color clr, int style, int width)
{
   if(ObjectFind(name) < 0)
      ObjectCreate(name, OBJ_HLINE, 0, 0, price);
   ObjectSet(name, OBJPROP_PRICE1, price);
   ObjectSet(name, OBJPROP_COLOR, clr);
   ObjectSet(name, OBJPROP_STYLE, style);
   ObjectSet(name, OBJPROP_WIDTH, width);
   ObjectSet(name, OBJPROP_SELECTABLE, false);
   ObjectSet(name, OBJPROP_HIDDEN, true);
}

//+------------------------------------------------------------------+
//| 画价位带(吸筹/派发)                                               |
//+------------------------------------------------------------------+
void DrawZones()
{
   if(!InpShowZones) return;

   datetime t1 = Time[MathMin(InpZoneBars, Bars - 1)];
   datetime t2 = Time[0] + PeriodSeconds() * 2;

   // 价签错开显示: 价位区挨得近时，标签上下交替，避免叠在一起看不清
   bool flip = false;

   for(int i = 0; i < g_zoneCount; i++)
   {
      bool isAcc = (g_zoneKind[i] == "accumulate");
      color clr = isAcc ? InpAccumColor : InpDistColor;
      string nm = WF_PREFIX + "zone_" + IntegerToString(i);

      if(ObjectFind(nm) < 0)
         ObjectCreate(nm, OBJ_RECTANGLE, 0, t1, g_zoneHigh[i], t2, g_zoneLow[i]);
      ObjectSet(nm, OBJPROP_TIME1, t1);
      ObjectSet(nm, OBJPROP_PRICE1, g_zoneHigh[i]);
      ObjectSet(nm, OBJPROP_TIME2, t2);
      ObjectSet(nm, OBJPROP_PRICE2, g_zoneLow[i]);
      ObjectSet(nm, OBJPROP_COLOR, clr);
      ObjectSet(nm, OBJPROP_BACK, true);
      ObjectSet(nm, OBJPROP_SELECTABLE, false);
      ObjectSet(nm, OBJPROP_HIDDEN, true);
      ObjectSet(nm, OBJPROP_WIDTH, 1);

      string txt = StringFormat("%s %s ~ %s  净%s  强度%d",
                                (isAcc ? "吸筹" : "派发"),
                                DoubleToString(g_zoneLow[i], Digits),
                                DoubleToString(g_zoneHigh[i], Digits),
                                CompactNumUsd(g_zoneNet[i]),
                                g_zoneStrength[i]);

      // 标签位置: 一半贴在区间上沿，一半错开一点，减少互相遮挡
      double labelPrice = flip ? g_zoneLow[i] - 4 * Point : g_zoneHigh[i];
      flip = !flip;

      string tn = WF_PREFIX + "ztxt_" + IntegerToString(i);
      if(ObjectFind(tn) < 0)
         ObjectCreate(tn, OBJ_TEXT, 0, t2, labelPrice);
      ObjectSet(tn, OBJPROP_TIME1, t2);
      ObjectSet(tn, OBJPROP_PRICE1, labelPrice);
      ObjectSetText(tn, "  " + txt, 8);

      // 字色: 与图表背景对比度不足时改用背景的对比色，保证看得清
      // (吸筹/派发的方向由价位带和箭头颜色表示，标签只负责清晰可读)
      color chartBg1 = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);
      color txtClr = clr;
      if(ContrastRatio(ColorLuma(clr), ColorLuma(chartBg1)) < 3.0) txtClr = ReadableOn(chartBg1);
      ObjectSet(tn, OBJPROP_COLOR, txtClr);
      ObjectSet(tn, OBJPROP_FONTSIZE, 8);
      ObjectSet(tn, OBJPROP_SELECTABLE, false);
      ObjectSet(tn, OBJPROP_HIDDEN, true);
   }
}

//+------------------------------------------------------------------+
//| 画巨鲸大单标记                                                    |
//+------------------------------------------------------------------+
void DrawWhales()
{
   if(!InpShowWhales || g_whaleCount <= 0) return;

   int maxBack = MathMin(InpWhaleBarsBack, Bars - 1);

   for(int i = 0; i < g_whaleCount; i++)
   {
      datetime t = (datetime)g_whaleTs[i];
      // JSON 时间是 UTC，MT4 用服务器时间，加时差
      t = t + (TimeCurrent() - TimeLocal());
      int shift = iBarShift(_Symbol, 0, t, false);
      if(shift < 0 || shift > maxBack) continue;

      // 交易所价格 -> 图表价格：按当前价差平移(消除 CFD 基差)
      double price = g_whalePrice[i];
      if(g_refPrice > 0 && g_walletMark > 0)
         price = price * (MarketInfo(_Symbol, MODE_BID) / g_walletMark);

      bool isBuy = (g_whaleSide[i] == "buy");
      double anchor = isBuy ? Low[shift] - 6 * Point : High[shift] + 6 * Point;
      string nm = WF_PREFIX + "wh_" + IntegerToString(i);

      if(ObjectFind(nm) < 0)
         ObjectCreate(nm, OBJ_ARROW, 0, t, anchor);
      ObjectSet(nm, OBJPROP_TIME1, t);
      ObjectSet(nm, OBJPROP_PRICE1, anchor);
      ObjectSet(nm, OBJPROP_ARROWCODE, isBuy ? 233 : 234);   // 上/下箭头
      ObjectSet(nm, OBJPROP_COLOR, isBuy ? InpAccumColor : InpDistColor);
      ObjectSet(nm, OBJPROP_WIDTH, 2);
      ObjectSet(nm, OBJPROP_SELECTABLE, false);
      ObjectSet(nm, OBJPROP_HIDDEN, true);

      string tn = WF_PREFIX + "whtxt_" + IntegerToString(i);
      if(ObjectFind(tn) < 0)
         ObjectCreate(tn, OBJ_TEXT, 0, t, anchor);
      ObjectSet(tn, OBJPROP_TIME1, t);
      ObjectSet(tn, OBJPROP_PRICE1, anchor);
      ObjectSetText(tn, CompactNumUsd(g_whaleUsd[i]), 7);
      // 字色: 与图表背景对比度不足时改用背景的对比色，保证看得清
      color wClr = isBuy ? InpAccumColor : InpDistColor;
      color chartBg = (color)ChartGetInteger(0, CHART_COLOR_BACKGROUND);
      if(ContrastRatio(ColorLuma(wClr), ColorLuma(chartBg)) < 3.0) wClr = ReadableOn(chartBg);
      ObjectSet(tn, OBJPROP_COLOR, wClr);
      ObjectSet(tn, OBJPROP_FONTSIZE, 7);
      ObjectSet(tn, OBJPROP_SELECTABLE, false);
      ObjectSet(tn, OBJPROP_HIDDEN, true);
   }
}

//+------------------------------------------------------------------+
//| 仪表盘                                                            |
//+------------------------------------------------------------------+
void DrawPanel()
{
   if(!InpShowPanel) return;

   RefreshColors();   // 面板文字色跟随图表背景/对比度刷新

   int rows = IsGoldChart() ? 12 : 13;
   int h = 26 + rows * WF_ROW_H + 8;

   string bg = WF_PREFIX + "bg";
   if(ObjectFind(bg) < 0)
      ObjectCreate(bg, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSet(bg, OBJPROP_CORNER, 0);
   ObjectSet(bg, OBJPROP_XDISTANCE, InpPanelX);
   ObjectSet(bg, OBJPROP_YDISTANCE, InpPanelY);
   ObjectSet(bg, OBJPROP_XSIZE, WF_PANEL_W);
   ObjectSet(bg, OBJPROP_YSIZE, h);
   ObjectSet(bg, OBJPROP_BGCOLOR, WF_PANEL_TEXT_BG);
   ObjectSet(bg, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSet(bg, OBJPROP_COLOR, C'70,78,92');
   ObjectSet(bg, OBJPROP_BACK, false);
   ObjectSet(bg, OBJPROP_SELECTABLE, false);
   ObjectSet(bg, OBJPROP_HIDDEN, true);

   int x = InpPanelX + 10;
   int y = InpPanelY + 6;
   int r = 0;

   // 新鲜度
   int age = (int)(TimeLocal() - g_lastGoodLoad);
   bool fresh = (g_lastGoodLoad > 0 && age <= 900);
   string head = StringFormat("巨鲸/聪明钱  %s   数据:%s",
                              TimeToString(TimeCurrent(), TIME_MINUTES),
                              (g_lastGoodLoad > 0 ? TimeToString(g_lastGoodLoad, TIME_MINUTES) : "无"));
   SetLabel(WF_PREFIX + "r0", x, y, head, fresh ? g_panelText : clrOrange, 9);
   y += WF_ROW_H; r++;

   string status = fresh ? StringFormat("● 正常 (延迟 %d 秒)", age) : "● 数据过期/未启动采集脚本";
   SetLabel(WF_PREFIX + "r1", x, y, status, fresh ? g_accumText : clrRed, 8);
   y += WF_ROW_H; r++;

   if(IsGoldChart())
   {
      SetLabel(WF_PREFIX + "g0", x, y, StringFormat("COMEX 参考价: %s", DoubleToString(g_refPrice, 2)), g_panelText, 8);
      y += WF_ROW_H;
      SetLabel(WF_PREFIX + "g1", x, y, StringFormat("GLD ETF: %s  (%s%%)  量比 %.2f",
               DoubleToString(g_gldPrice, 2),
               DoubleToString(g_gldChgPct, 2),
               g_gldVolRatio), g_panelText, 8);
      y += WF_ROW_H;
      SetLabel(WF_PREFIX + "g2", x, y, "COT 管理基金持仓 (每周五更新)", g_hiText, 9);
      y += WF_ROW_H;
      SetLabel(WF_PREFIX + "g3", x, y, StringFormat("报告日: %s  (%.0f 天前)",
               g_cotDate, g_cotAgeDays), g_panelText, 8);
      y += WF_ROW_H;
      SetLabel(WF_PREFIX + "g4", x, y, StringFormat("净持仓: %s 手   周变化: %s",
               Money0((double)g_cotNet), Money0((double)g_cotNetChange)), g_panelText, 8);
      y += WF_ROW_H;
      color idxClr = g_panelText;
      string idxNote = "";
      if(g_cotExtreme == "crowded_long") { idxClr = g_distText; idxNote = " 多头极度拥挤"; }
      else if(g_cotExtreme == "crowded_short") { idxClr = g_accumText; idxNote = " 空头极度拥挤"; }
      SetLabel(WF_PREFIX + "g5", x, y, StringFormat("COT指数: %.1f / 100%s", g_cotIndex, idxNote), idxClr, 8);
      y += WF_ROW_H;
      SetLabel(WF_PREFIX + "g6", x, y, StringFormat("样本 %d 周(不足一年时指数仅供参考)", g_cotWeeks), g_panelText, 7);
      y += WF_ROW_H;
      SetLabel(WF_PREFIX + "g7", x, y, "COT 为周频机构持仓，图上不画价位，只做方向参考", g_panelText, 7);
      y += WF_ROW_H;
   }
   else
   {
      if(g_symbol == "")
      {
         SetLabel(WF_PREFIX + "n0", x, y, "该品种不在采集列表(BTC/ETH/黄金)", clrOrange, 9);
         y += WF_ROW_H;
      }
      else
      {
         SetLabel(WF_PREFIX + "c0", x, y, StringFormat("%s  交易所参考价: %s", g_symbol,
                  DoubleToString(g_refPrice, 2)), g_panelText, 8);
         y += WF_ROW_H;
         SetLabel(WF_PREFIX + "c1", x, y, StringFormat("窗口 %d 分钟 / %d 笔聚合成交",
                  g_windowMin, g_tradesUsed), g_panelText, 7);
         y += WF_ROW_H;
         SetLabel(WF_PREFIX + "c2", x, y, StringFormat("净主动流: %s   买卖比 %.2f",
                  CompactNumUsd(g_flowNet), g_flowRatio),
                  (g_flowNet >= 0 ? g_accumText : g_distText), 8);
         y += WF_ROW_H;
         color smartClr = (g_smartRatio >= 1) ? g_accumText : g_distText;
         SetLabel(WF_PREFIX + "c3", x, y, StringFormat("聪明钱(顶级持仓): 多 %.1f%%  多空比 %.2f",
                  g_smartLongPct, g_smartRatio), smartClr, 8);
         y += WF_ROW_H;
         SetLabel(WF_PREFIX + "c4", x, y, StringFormat("顶级账户多空比 %.2f   散户多空比 %.2f",
                  g_topAccountRatio, g_retailRatio), g_panelText, 8);
         y += WF_ROW_H;
         string divTxt = "";
         if(g_smartRatio > 1.2 && g_retailRatio < 1.0) divTxt = "  ← 大户偏多/散户偏空";
         else if(g_smartRatio < 1.0 && g_retailRatio > 1.2) divTxt = "  ← 大户偏空/散户偏多";
         SetLabel(WF_PREFIX + "c5", x, y, "持仓量变化(24h): " + DoubleToString(g_oiChangePct, 2) + "%" + divTxt,
                  (g_oiChangePct >= 0 ? g_accumText : g_distText), 8);
         y += WF_ROW_H;
         SetLabel(WF_PREFIX + "c6", x, y, StringFormat("资金费率: %.4f%%   持仓量 %s",
                  g_fundingPct, CompactNumUsd(g_oiUsd)), g_panelText, 8);
         y += WF_ROW_H;
         SetLabel(WF_PREFIX + "c7", x, y, StringFormat("巨鲸大单: %d 笔(窗口内)  价位区: %d 个",
                  g_whaleCount, g_zoneCount), g_panelText, 8);
         y += WF_ROW_H;
         // 列出强度最高的 3 个价位区
         for(int k = 0; k < MathMin(3, g_zoneCount); k++)
         {
            bool isAcc = (g_zoneKind[k] == "accumulate");
            SetLabel(WF_PREFIX + "z" + IntegerToString(k), x, y,
                     StringFormat("  %s %s~%s  净%s  强度%d",
                                  (isAcc ? "吸筹" : "派发"),
                                  DoubleToString(g_zoneLow[k], Digits),
                                  DoubleToString(g_zoneHigh[k], Digits),
                                  CompactNumUsd(g_zoneNet[k]), g_zoneStrength[k]),
                     isAcc ? InpAccumColor : InpDistColor, 8);
            y += WF_ROW_H;
         }
      }
   }
   ChartRedraw();
}

//+------------------------------------------------------------------+
//| 提醒(新出现的大额单)                                              |
//+------------------------------------------------------------------+
void CheckAlerts()
{
   if(InpAlertMinUSD <= 0 || g_whaleCount <= 0) return;

   string gvName = WF_PREFIX + "lastAlert_" + _Symbol;
   long lastTs = (long)GlobalVariableGet(gvName);
   long newest = lastTs;

   for(int i = 0; i < g_whaleCount; i++)
   {
      if(g_whaleTs[i] > lastTs && g_whaleUsd[i] >= InpAlertMinUSD)
      {
         if(g_whaleTs[i] > newest)
         {
            string msg = StringFormat("巨鲸大单 %s %s @ %s  (%s)",
                                      g_symbol, g_whaleSide[i],
                                      DoubleToString(g_whalePrice[i], 2),
                                      CompactNumUsd(g_whaleUsd[i]));
            Print(msg);
            if(InpAlertPopup) Alert(msg);
         }
         if(g_whaleTs[i] > newest) newest = g_whaleTs[i];
      }
      if(g_whaleTs[i] > newest) newest = g_whaleTs[i];
   }

   if(newest > lastTs && GlobalVariableSet(gvName, (double)newest) == 0)
      Print("GlobalVariableSet 失败: ", GetLastError());
   if(newest > g_whaleMaxTs) g_whaleMaxTs = newest;
}

//+------------------------------------------------------------------+
//| 主刷新                                                            |
//+------------------------------------------------------------------+
void Refresh()
{
   string raw = "";
   if(!ReadDataFile(raw))
   {
      if(g_lastGoodLoad == 0)
      {
         DeleteOurObjects();
         DrawPanel();
      }
      return;
   }

   g_lastLoad = TimeLocal();
   bool same = (raw == g_lastRaw);
   g_lastRaw = raw;

   if(!same)
   {
      if(ParseData(raw))
      {
         g_lastGoodLoad = TimeLocal();
         g_loadErrors = 0;
      }
      else
      {
         Print("JSON 解析失败(文件内容不完整或格式变化)");
         return;
      }
      CheckAlerts();
   }
   else if(g_lastGoodLoad == 0)
      g_lastGoodLoad = TimeLocal();

   DeleteOurObjects();
   RefreshColors();
   DrawZones();
   DrawWhales();
   DrawPanel();
}

//+------------------------------------------------------------------+
//| EA 生命周期                                                       |
//+------------------------------------------------------------------+
int OnInit()
{
   g_filePath = (StringLen(InpBridgeSubdir) > 0)
                ? InpBridgeSubdir + "\\" + InpFileName
                : InpFileName;

   IndicatorBuffers(0);
   DeleteOurObjects();

   // 首次加载
   string raw = "";
   if(ReadDataFile(raw))
   {
      if(ParseData(raw))
      {
         g_lastRaw = raw;
         g_lastGoodLoad = TimeLocal();
      }
   }
   DeleteOurObjects();
   RefreshColors();
   DrawZones();
   DrawWhales();
   DrawPanel();

   EventSetTimer(MathMax(1, InpRefreshSeconds));
   Print("WhaleFlow 启动 | 读取: MQL4\\Files\\", g_filePath,
         " | 图表品种: ", _Symbol, " | JSON品种: ", g_symbol);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   DeleteOurObjects();
   ChartRedraw();
   Print("WhaleFlow 停止 | 原因: ", reason);
}

void OnTick()
{
   // 主要刷新走 OnTimer；这里只处理面板时间显示
   if(InpShowPanel)
   {
      int age = (int)(TimeLocal() - g_lastGoodLoad);
      bool fresh = (g_lastGoodLoad > 0 && age <= 900);
      SetLabel(WF_PREFIX + "r1", InpPanelX + 10, InpPanelY + 6 + WF_ROW_H,
               fresh ? StringFormat("● 正常 (延迟 %d 秒)", age) : "● 数据过期/未启动采集脚本",
               fresh ? g_accumText : clrRed, 8);
   }
}

void OnTimer()
{
   Refresh();
}
//+------------------------------------------------------------------+

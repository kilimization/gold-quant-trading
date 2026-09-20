//+------------------------------------------------------------------+
//| Test_WhaleParse.mq4 — 解析自检脚本(只读文件+打印，不画图不下单)    |
//| 用法: 编译后拖到任意图表上，结果打印在"专家"日志里                 |
//|      同时会把同样的内容写入 MQL4\Files\whale_parse_result.txt，     |
//|      供 whale_health.py 体检脚本自动核对。                          |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs

input string InpFileName     = "whale_flow.json"; // 文件名
input string InpBridgeSubdir = "DWX";             // 子目录

// 复制自 WhaleFlow_EA.mq4 的解析层，用于单独验证解析是否正确
int Num0dummy = 0;

// 结果输出缓冲: 既打印到专家日志，也落盘一份给体检脚本
string g_log = "";

// 只接收一个已拼好的字符串；调用处用 + 拼接(MQL4 用户函数不支持变参)
void Log(string s)
{
   Print(s);
   g_log = g_log + s + "\r\n";
}

void SaveLog()
{
   int h = FileOpen("whale_parse_result.txt", FILE_WRITE | FILE_TXT);
   if(h == INVALID_HANDLE)
   {
      Print("!! 无法写入 whale_parse_result.txt 错误码: ", GetLastError());
      return;
   }
   FileWriteString(h, g_log);
   FileClose(h);
   Print("结果副本已写入 MQL4\\Files\\whale_parse_result.txt");
}

string JStrX(string json, string key, string def, int from)
{
   string pat = "\"" + key + "\":\"";
   int p = StringFind(json, pat, from);
   if(p < 0) return def;
   p += StringLen(pat);
   int q = StringFind(json, "\"", p);
   if(q < 0) return def;
   return StringSubstr(json, p, q - p);
}

double JNumX(string json, string key, double def, int from)
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

bool FindObjectRangeX(string s, string key, int from, int &b0, int &b1, string &inside)
{
   b0 = -1; b1 = -1; inside = "";
   string pat = "\"" + key + "\":{";
   int p = StringFind(s, pat, from);
   if(p < 0) return false;
   int start = p + StringLen(pat);
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
            b0 = start; b1 = i - 1;
            inside = StringSubstr(s, start, i - start);
            return true;
         }
      }
      i++;
   }
   return false;
}

int FindArrayX(string json, string key, int from)
{
   string pat = "\"" + key + "\":[";
   int p = StringFind(json, pat, from);
   if(p < 0) return -1;
   return p + StringLen(pat) - 1;
}

bool NthObjectX(string json, int arrPos, int index, string &obj)
{
   int p = arrPos + 1;
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

string NumFmt(double v) { return DoubleToString(v, 2); }

void OnStart()
{
   string path = InpBridgeSubdir + "\\" + InpFileName;
   Log("=== WhaleParse 自检开始 | 文件: "+ path+ " ===");

   ResetLastError();
   int h = FileOpen(path, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE)
   {
      Log("!! 打开失败 错误码: "+ IntegerToString(GetLastError())+ " (路径应为 MQL4\\Files\\"+ path+ ")");
      return;
   }
   int size = (int)FileSize(h);
   uchar buf[];
   ArrayResize(buf, size);
   FileReadArray(h, buf, 0, size);
   FileClose(h);

   string raw = "";
   for(int i = 0; i < size; i++)
      raw = raw + CharToString(buf[i]);
   Log("文件大小: "+ IntegerToString(size) + " 字节, 读入长度: "+ IntegerToString(StringLen(raw)));
   Log("头部: "+ StringSubstr(raw, 0, 90));

   // crypto 数组
   int arr = FindArrayX(raw, "crypto", 0);
   Log("crypto 数组位置: "+ IntegerToString(arr) );
   if(arr < 0) { Log("!! 未找到 crypto 数组"); return; }

   int idx = 0;
   string obj = "";
   while(NthObjectX(raw, arr, idx, obj) && idx < 8)
   {
      string sym = JStrX(obj, "symbol", "?", 0);
      double last = JNumX(obj, "last_price", 0, 0);
      int wmin = (int)JNumX(obj, "window_minutes", 0, 0);
      int used = (int)JNumX(obj, "trades_used", 0, 0);
      Log("--- crypto["+ IntegerToString(idx) + "] symbol="+ sym+ " last="+ NumFmt(last)+
            " window="+ IntegerToString(wmin) + "min trades="+ IntegerToString(used) );

      int b0 = -1, b1 = -1;
      string sub = "";
      if(FindObjectRangeX(obj, "flow_2h", 0, b0, b1, sub))
         Log("    flow_2h: buy="+ NumFmt(JNumX(sub, "buy_usd", 0, 0))+
               " sell="+ NumFmt(JNumX(sub, "sell_usd", 0, 0))+
               " net="+ NumFmt(JNumX(sub, "net_usd", 0, 0))+
               " ratio="+ NumFmt(JNumX(sub, "ratio", 0, 0)));
      else
         Log("    !! flow_2h 解析失败");

      if(FindObjectRangeX(obj, "smart", 0, b0, b1, sub))
         Log("    smart: long%="+ NumFmt(JNumX(sub, "long_pct", 0, 0))+
               " ratio="+ NumFmt(JNumX(sub, "ratio", 0, 0)));
      else
         Log("    !! smart 解析失败");

      if(FindObjectRangeX(obj, "top_account", 0, b0, b1, sub))
         Log("    top_account ratio="+ NumFmt(JNumX(sub, "ratio", 0, 0)));
      if(FindObjectRangeX(obj, "retail", 0, b0, b1, sub))
         Log("    retail ratio="+ NumFmt(JNumX(sub, "ratio", 0, 0)));
      if(FindObjectRangeX(obj, "oi", 0, b0, b1, sub))
         Log("    oi: usd="+ NumFmt(JNumX(sub, "oi_usd", 0, 0))+
               " chg%="+ NumFmt(JNumX(sub, "oi_change_pct", 0, 0)));
      if(FindObjectRangeX(obj, "funding", 0, b0, b1, sub))
         Log("    funding: mark="+ NumFmt(JNumX(sub, "mark_price", 0, 0))+
               " rate%="+ NumFmt(JNumX(sub, "funding_rate_pct", 0, 0)));

      int zArr = FindArrayX(obj, "zones", 0);
      int zc = 0;
      string zo = "";
      if(zArr >= 0)
         while(NthObjectX(obj, zArr, zc, zo))
         {
            Log("    zone["+ IntegerToString(zc) + "] "+ JStrX(zo, "kind", "?", 0)+
                  " low="+ NumFmt(JNumX(zo, "low", 0, 0))+
                  " high="+ NumFmt(JNumX(zo, "high", 0, 0))+
                  " net="+ NumFmt(JNumX(zo, "net_usd", 0, 0))+
                  " strength="+ NumFmt(JNumX(zo, "strength", 0, 0)));
            zc++;
         }

      int wArr = FindArrayX(obj, "whales", 0);
      int wc = 0;
      string wo = "";
      if(wArr >= 0)
         while(NthObjectX(obj, wArr, wc, wo))
         {
            if(wc < 4)
               Log("    whale["+ IntegerToString(wc) + "] ts="+ NumFmt(JNumX(wo, "ts", 0, 0))+
                     " price="+ NumFmt(JNumX(wo, "price", 0, 0))+
                     " usd="+ NumFmt(JNumX(wo, "usd", 0, 0))+
                     " side="+ JStrX(wo, "side", "?", 0));
            wc++;
         }
      Log("    统计: zones="+ IntegerToString(zc) + " whales="+ IntegerToString(wc) );
      idx++;
   }

   // gold 段
   int goldPos = StringFind(raw, "\"gold\":{", 0);
   if(goldPos > 0)
   {
      int c0 = -1, c1 = -1;
      string cot = "";
      if(FindObjectRangeX(raw, "cot", goldPos, c0, c1, cot))
         Log("gold.cot: date="+ JStrX(cot, "report_date", "?", 0)+
               " net="+ NumFmt(JNumX(cot, "net", 0, 0))+
               " chg="+ NumFmt(JNumX(cot, "net_change", 0, 0))+
               " index="+ NumFmt(JNumX(cot, "cot_index", 0, 0))+
               " weeks="+ NumFmt(JNumX(cot, "history_weeks", 0, 0))+
               " extreme="+ JStrX(cot, "extreme", "?", 0));
      string gld = "";
      if(FindObjectRangeX(raw, "gld", goldPos, c0, c1, gld))
         Log("gold.gld: last="+ NumFmt(JNumX(gld, "last", 0, 0))+
               " chg%="+ NumFmt(JNumX(gld, "chg_pct", 0, 0))+
               " volratio="+ NumFmt(JNumX(gld, "volume_vs_avg20", 0, 0)));
      Log("gold.ref_price="+ NumFmt(JNumX(raw, "ref_price", 0, goldPos)));
   }
   else
      Log("!! 未找到 gold 段");

   Log("=== 自检结束 (若上面字段都有值, EA 的画图数据源就是通的) ===");
   SaveLog();
}
//+------------------------------------------------------------------+

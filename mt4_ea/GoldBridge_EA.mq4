//+------------------------------------------------------------------+
//| GoldBridge_EA.mq4 — 黄金量化交易桥接EA                           |
//| 功能: 接收Python指令，执行交易，回传状态                           |
//| 通信方式: 文件桥接 (MQL4/Files/DWX/)                              |
//+------------------------------------------------------------------+
#property copyright "Gold Quant Trading System"
#property version   "1.00"
#property strict
#include <stdlib.mqh>  // 包含ErrorDescription函数

// 输入参数
extern int    TIMER_MS        = 500;     // 检查指令间隔(毫秒)
extern int    HEARTBEAT_SEC   = 5;       // 心跳间隔(秒)
extern string BRIDGE_SUBDIR   = "DWX";   // 桥接子目录

// 全局变量
string bridge_path;
datetime last_heartbeat;
datetime last_bar_write;      // K线数据上次写入时间
extern int    BAR_WRITE_SEC  = 30;     // K线数据写入间隔(秒)
extern int    BAR_COUNT      = 200;    // 写入的K线数量

//+------------------------------------------------------------------+
//| 初始化                                                            |
//+------------------------------------------------------------------+
int OnInit()
{
    bridge_path = BRIDGE_SUBDIR + "\\";
    
    // 创建桥接目录
    FolderCreate(bridge_path, 0);
    
    // 清掉上一次运行残留的指令文件。
    // 否则Python在EA离线时发出的指令会一直躺在磁盘上, EA一上线就把它当新指令执行,
    // 开出一张Python早已判定为"超时失败"的幽灵单。
    string stale_cmd = bridge_path + "commands.json";
    if(FileIsExist(stale_cmd, 0))
    {
        FileDelete(stale_cmd, 0);
        Print("[GoldBridge] 已清除上次运行残留的 commands.json (避免执行过期指令)");
    }
    
    // 同样清掉残留的标注指令: 否则EA一上线就会画出上一次会话遗留的止损/止盈线,
    // 用户看到的可能是一根几天前的过期线。Python在下一次信号/移动止损时自然会重发。
    string stale_ann = bridge_path + "annotate.json";
    if(FileIsExist(stale_ann, 0))
    {
        FileDelete(stale_ann, 0);
        Print("[GoldBridge] 已清除上次运行残留的 annotate.json (避免画出过期标注)");
    }
    
    // 启动定时器
    EventSetMillisecondTimer(TIMER_MS);
    
    // 写入初始心跳
    WriteHeartbeat();
    
    // 写入账户信息
    WriteAccountInfo();
    
    // 写入持仓信息
    WritePositions();
    
    Print("[GoldBridge] EA 初始化完成. 桥接目录: ", bridge_path);
    Print("[GoldBridge] 等待 Python 指令...");
    
    return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| 卸载                                                              |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
    EventKillTimer();
    Print("[GoldBridge] EA 已停止. 原因: ", reason);
}

//+------------------------------------------------------------------+
//| 定时器回调                                                        |
//+------------------------------------------------------------------+
void OnTimer()
{
    // 心跳
    if(TimeCurrent() - last_heartbeat >= HEARTBEAT_SEC)
    {
        WriteHeartbeat();
        WriteAccountInfo();
        WritePositions();
        last_heartbeat = TimeCurrent();
    }
    
    // K线数据 (每30秒写一次)
    if(TimeCurrent() - last_bar_write >= BAR_WRITE_SEC)
    {
        WriteBarData(PERIOD_H1,  "bars_h1.json");
        WriteBarData(PERIOD_M15, "bars_m15.json");
        WriteBarData(PERIOD_M5,  "bars_m5.json");   // M15 RSI 策略的M5形态校验需要
        last_bar_write = TimeCurrent();
    }
    
    // 检查Python指令
    CheckCommands();
    
    // 检查图表标注指令 (独立通道 annotate.json, 与下单通道互不干扰)
    CheckAnnotations();
}

//+------------------------------------------------------------------+
//| 检查并执行Python指令                                              |
//+------------------------------------------------------------------+
void CheckCommands()
{
    string filename = bridge_path + "commands.json";
    
    if(!FileIsExist(filename, 0))
        return;
    
    // 读取指令文件
    int handle = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE)
        return;
    
    string content = "";
    while(!FileIsEnding(handle))
        content += FileReadString(handle) + "\n";
    FileClose(handle);
    
    // ⚠️ 必须先确认指令完整, 再删除文件。
    // 旧代码在这里(解析之前)就 FileDelete, 如果正好读到 Python 写入一半/还没写完的
    // 文件, 指令就被直接丢弃 → Python 等不到响应而超时; 反过来若把残缺JSON当指令
    // 执行, 就会开出参数错误的单子。内容不完整时保留文件, 等下一次 timer 重试。
    if(StringLen(content) < 5 || StringFind(content, "\"action\"") == -1)
        return;
    
    // 解析JSON (简单解析)
    string action = ExtractJsonString(content, "action");
    
    if(action == "")
        return;   // 还没写完, 保留文件等下次重试
    
    // 到这里指令完整, 删除文件防止重复执行
    FileDelete(filename, 0);
    
    Print("[GoldBridge] 收到指令: ", StringSubstr(content, 0, 100));
    
    if(action == "OPEN")
        ExecuteOpen(content);
    else if(action == "CLOSE")
        ExecuteClose(content);
    else if(action == "MODIFY")
        ExecuteModify(content);
    else
        WriteResponse(false, "未知操作: " + action);
}

//+------------------------------------------------------------------+
//| 执行开仓                                                          |
//+------------------------------------------------------------------+
void ExecuteOpen(string json)
{
    string symbol   = ExtractJsonString(json, "symbol");
    string type_str = ExtractJsonString(json, "type");
    double lots     = ExtractJsonDouble(json, "lots");
    double price    = ExtractJsonDouble(json, "price");
    double sl       = ExtractJsonDouble(json, "sl");
    double tp       = ExtractJsonDouble(json, "tp");
    string comment  = ExtractJsonString(json, "comment");
    int    magic    = (int)ExtractJsonDouble(json, "magic");
    int    slippage = (int)ExtractJsonDouble(json, "slippage");
    
    if(symbol == "") symbol = Symbol();
    if(slippage == 0) slippage = 5;
    
    int cmd = -1;
    if(type_str == "BUY")        cmd = OP_BUY;
    else if(type_str == "SELL")  cmd = OP_SELL;
    else if(type_str == "BUYLIMIT")  cmd = OP_BUYLIMIT;
    else if(type_str == "SELLLIMIT") cmd = OP_SELLLIMIT;
    else if(type_str == "BUYSTOP")   cmd = OP_BUYSTOP;
    else if(type_str == "SELLSTOP")  cmd = OP_SELLSTOP;
    
    if(cmd == -1)
    {
        WriteResponse(false, "无效订单类型: " + type_str);
        return;
    }
    
    // 市价单用当前价
    if(price == 0)
    {
        if(cmd == OP_BUY)  price = MarketInfo(symbol, MODE_ASK);
        if(cmd == OP_SELL) price = MarketInfo(symbol, MODE_BID);
    }
    
    // 打印详细参数用于调试
    Print("[GoldBridge] 下单参数: symbol=", symbol, " cmd=", cmd, " lots=", lots, 
          " price=", price, " sl=", sl, " tp=", tp, " slippage=", slippage, " magic=", magic);
    Print("[GoldBridge] 当前报价: Bid=", MarketInfo(symbol, MODE_BID), " Ask=", MarketInfo(symbol, MODE_ASK),
          " StopLevel=", MarketInfo(symbol, MODE_STOPLEVEL), " Digits=", (int)MarketInfo(symbol, MODE_DIGITS));
    
    // 检查最小止损距离
    double stopLevel = MarketInfo(symbol, MODE_STOPLEVEL) * MarketInfo(symbol, MODE_POINT);
    if(cmd == OP_BUY && sl > 0 && (price - sl) < stopLevel)
    {
        Print("[GoldBridge] ⚠️ 止损距离太近, 调整: ", (price - sl), " < ", stopLevel);
        sl = price - stopLevel - MarketInfo(symbol, MODE_POINT);
    }
    if(cmd == OP_SELL && sl > 0 && (sl - price) < stopLevel)
    {
        Print("[GoldBridge] ⚠️ 止损距离太近, 调整: ", (sl - price), " < ", stopLevel);
        sl = price + stopLevel + MarketInfo(symbol, MODE_POINT);
    }
    
    // 发送订单
    int ticket = OrderSend(symbol, cmd, lots, price, slippage, sl, tp, comment, magic, 0, clrGreen);
    
    if(ticket > 0)
    {
        Print("[GoldBridge] ✅ 开仓成功: #", ticket, " ", type_str, " ", symbol, " ", lots, "手 @ ", price, " SL=", sl);
        WriteResponse(true, "开仓成功 #" + IntegerToString(ticket));
        WritePositions();
    }
    else
    {
        int err = GetLastError();
        Print("[GoldBridge] ❌ 开仓失败: Error ", err, 
              " (", ErrorDescription(err), ")",
              " price=", price, " sl=", sl, " lots=", lots);
        WriteResponse(false, "开仓失败 Error: " + IntegerToString(err) + " " + ErrorDescription(err));
    }
}

//+------------------------------------------------------------------+
//| 执行平仓                                                          |
//+------------------------------------------------------------------+
void ExecuteClose(string json)
{
    int ticket = (int)ExtractJsonDouble(json, "ticket");
    
    if(!OrderSelect(ticket, SELECT_BY_TICKET))
    {
        WriteResponse(false, "找不到订单 #" + IntegerToString(ticket));
        return;
    }
    
    double price;
    if(OrderType() == OP_BUY)
        price = MarketInfo(OrderSymbol(), MODE_BID);
    else
        price = MarketInfo(OrderSymbol(), MODE_ASK);
    
    bool result = OrderClose(ticket, OrderLots(), price, 5, clrRed);
    
    if(result)
    {
        Print("[GoldBridge] ✅ 平仓成功: #", ticket);
        WriteResponse(true, "平仓成功 #" + IntegerToString(ticket));
        WritePositions();
    }
    else
    {
        int err = GetLastError();
        Print("[GoldBridge] ❌ 平仓失败: Error ", err);
        WriteResponse(false, "平仓失败 Error: " + IntegerToString(err));
    }
}

//+------------------------------------------------------------------+
//| 执行修改                                                          |
//+------------------------------------------------------------------+
void ExecuteModify(string json)
{
    int    ticket = (int)ExtractJsonDouble(json, "ticket");
    double sl     = ExtractJsonDouble(json, "sl");
    double tp     = ExtractJsonDouble(json, "tp");
    
    if(!OrderSelect(ticket, SELECT_BY_TICKET))
    {
        WriteResponse(false, "找不到订单 #" + IntegerToString(ticket));
        return;
    }
    
    if(sl == 0) sl = OrderStopLoss();
    if(tp == 0) tp = OrderTakeProfit();
    
    bool result = OrderModify(ticket, OrderOpenPrice(), sl, tp, 0, clrBlue);
    
    if(result)
    {
        WriteResponse(true, "修改成功 #" + IntegerToString(ticket));
        WritePositions();
    }
    else
    {
        int err = GetLastError();
        WriteResponse(false, "修改失败 Error: " + IntegerToString(err));
    }
}

//+------------------------------------------------------------------+
//| 写心跳文件                                                        |
//+------------------------------------------------------------------+
void WriteHeartbeat()
{
    string filename = bridge_path + "heartbeat.json";
    int handle = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle != INVALID_HANDLE)
    {
        FileWriteString(handle, "{\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + 
                        "\",\"symbol\":\"" + Symbol() +
                        "\",\"bid\":" + DoubleToString(Bid, Digits) +
                        ",\"ask\":" + DoubleToString(Ask, Digits) + "}");
        FileClose(handle);
    }
}

//+------------------------------------------------------------------+
//| 写账户信息                                                        |
//+------------------------------------------------------------------+
void WriteAccountInfo()
{
    string filename = bridge_path + "account.json";
    int handle = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle != INVALID_HANDLE)
    {
        FileWriteString(handle, 
            "{\"balance\":" + DoubleToString(AccountBalance(), 2) +
            ",\"equity\":" + DoubleToString(AccountEquity(), 2) +
            ",\"margin\":" + DoubleToString(AccountMargin(), 2) +
            ",\"free_margin\":" + DoubleToString(AccountFreeMargin(), 2) +
            ",\"leverage\":" + IntegerToString(AccountLeverage()) +
            ",\"bid\":" + DoubleToString(Bid, Digits) +
            ",\"ask\":" + DoubleToString(Ask, Digits) +
            ",\"spread\":" + IntegerToString(MarketInfo(Symbol(), MODE_SPREAD)) +
            ",\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}");
        FileClose(handle);
    }
}

//+------------------------------------------------------------------+
//| 写持仓信息                                                        |
//+------------------------------------------------------------------+
void WritePositions()
{
    string filename = bridge_path + "positions.json";
    int handle = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE) return;
    
    string json = "{\"positions\":[";
    bool first = true;
    
    for(int i = 0; i < OrdersTotal(); i++)
    {
        if(!OrderSelect(i, SELECT_BY_POS, MODE_TRADES)) continue;
        
        if(!first) json += ",";
        first = false;
        
        double current_price;
        if(OrderType() == OP_BUY)
            current_price = MarketInfo(OrderSymbol(), MODE_BID);
        else
            current_price = MarketInfo(OrderSymbol(), MODE_ASK);
        
        json += "{\"ticket\":" + IntegerToString(OrderTicket()) +
                ",\"symbol\":\"" + OrderSymbol() + "\"" +
                ",\"type\":" + IntegerToString(OrderType()) +
                ",\"lots\":" + DoubleToString(OrderLots(), 2) +
                ",\"open_price\":" + DoubleToString(OrderOpenPrice(), Digits) +
                ",\"current_price\":" + DoubleToString(current_price, Digits) +
                ",\"sl\":" + DoubleToString(OrderStopLoss(), Digits) +
                ",\"tp\":" + DoubleToString(OrderTakeProfit(), Digits) +
                ",\"profit\":" + DoubleToString(OrderProfit(), 2) +
                ",\"magic\":" + IntegerToString(OrderMagicNumber()) +
                ",\"comment\":\"" + OrderComment() + "\"" +
                ",\"open_time\":\"" + TimeToString(OrderOpenTime(), TIME_DATE|TIME_SECONDS) + "\"}";
    }
    
    json += "],\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}";
    
    FileWriteString(handle, json);
    FileClose(handle);
}

//+------------------------------------------------------------------+
//| 写K线数据                                                          |
//+------------------------------------------------------------------+
void WriteBarData(int timeframe, string filename)
{
    string filepath = bridge_path + filename;
    int handle = FileOpen(filepath, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE) return;
    
    string sym = Symbol();
    int count = MathMin(BAR_COUNT, iBars(sym, timeframe));
    if(count < 10)
    {
        FileClose(handle);
        return;
    }
    
    // JSON数组格式: {"bars":[{"t":"...","o":...,"h":...,"l":...,"c":...,"v":...}, ...]}
    string json = "{\"symbol\":\"" + sym + 
                  "\",\"timeframe\":" + IntegerToString(timeframe) + 
                  ",\"count\":" + IntegerToString(count) + 
                  ",\"bars\":[";
    
    // 从最旧到最新 (count-1 = 最旧, 0 = 最新)
    for(int i = count - 1; i >= 0; i--)
    {
        if(i < count - 1) json += ",";
        
        datetime bar_time = iTime(sym, timeframe, i);
        double bar_open   = iOpen(sym, timeframe, i);
        double bar_high   = iHigh(sym, timeframe, i);
        double bar_low    = iLow(sym, timeframe, i);
        double bar_close  = iClose(sym, timeframe, i);
        long   bar_vol    = iVolume(sym, timeframe, i);
        
        json += "{\"t\":\"" + TimeToString(bar_time, TIME_DATE|TIME_SECONDS) + "\"" +
                ",\"o\":" + DoubleToString(bar_open, Digits) +
                ",\"h\":" + DoubleToString(bar_high, Digits) +
                ",\"l\":" + DoubleToString(bar_low, Digits) +
                ",\"c\":" + DoubleToString(bar_close, Digits) +
                ",\"v\":" + IntegerToString(bar_vol) + "}";
    }
    
    json += "]}";
    FileWriteString(handle, json);
    FileClose(handle);
}

//+------------------------------------------------------------------+
//| 写响应文件                                                        |
//+------------------------------------------------------------------+
void WriteResponse(bool success, string message)
{
    string filename = bridge_path + "response.json";
    int handle = FileOpen(filename, FILE_WRITE | FILE_TXT | FILE_ANSI);
    if(handle != INVALID_HANDLE)
    {
        string result = success ? "true" : "false";
        FileWriteString(handle, 
            "{\"success\":" + result +
            ",\"message\":\"" + message + "\"" +
            ",\"timestamp\":\"" + TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS) + "\"}");
        FileClose(handle);
    }
}

//+------------------------------------------------------------------+
//| 简单JSON解析                                                      |
//+------------------------------------------------------------------+
string ExtractJsonString(string json, string key)
{
    string search = "\"" + key + "\":\"";
    int start = StringFind(json, search);
    if(start == -1) return "";
    start += StringLen(search);
    int end = StringFind(json, "\"", start);
    if(end == -1) return "";
    return StringSubstr(json, start, end - start);
}

double ExtractJsonDouble(string json, string key)
{
    string search1 = "\"" + key + "\":";
    int start = StringFind(json, search1);
    if(start == -1) return 0;
    start += StringLen(search1);
    
    // 跳过引号(如果值是字符串格式的数字)
    if(StringGetChar(json, start) == '"') start++;
    
    string num = "";
    for(int i = start; i < StringLen(json); i++)
    {
        int ch = StringGetChar(json, i);
        if((ch >= '0' && ch <= '9') || ch == '.' || ch == '-')
            num += CharToString((uchar)ch);
        else
            break;
    }
    
    if(num == "") return 0;
    return StringToDouble(num);
}
//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| 图表标注 (止损/止盈虚线 + 信号箭头)                                |
//| 通信方式: 独立文件 annotate.json, 不使用 commands.json/response.json |
//| 原因: commands.json 是单槽请求/应答邮箱, 标注绝不能污染下单通道。   |
//+------------------------------------------------------------------+

//| 对象命名规则(按策略固定, 便于动态跟踪时原地更新)                    |
//|   箭头:    GQ_<strategy>_arrow                                    |
//|   止损线:  GQ_<strategy>_sl      标签: GQ_<strategy>_sl_txt       |
//|   止盈线:  GQ_<strategy>_tp      标签: GQ_<strategy>_tp_txt       |
string AnnotatePrefix(string strategy)
{
    if(StringLen(strategy) <= 0) strategy = "default";
    return "GQ_" + strategy + "_";
}

//+------------------------------------------------------------------+
//| 统一设置标注对象的公共属性: 不可选中、非背景、隐藏                 |
//| 说明: 只有 ObjectCreate 成功的对象才设置属性, 避免报错污染错误码。  |
//+------------------------------------------------------------------+
void ApplyAnnotateCommon(string name)
{
    ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
    ObjectSetInteger(0, name, OBJPROP_SELECTED,   false);
    ObjectSetInteger(0, name, OBJPROP_BACK,       false);
    ObjectSetInteger(0, name, OBJPROP_HIDDEN,     true);
}

//+------------------------------------------------------------------+
//| 创建一个对象并套用公共属性                                        |
//| ObjectCreate 失败时打印错误并继续, 不抛异常、不中断 timer            |
//+------------------------------------------------------------------+
bool SafeCreate(string name, int type, datetime t1, double p1, datetime t2, double p2)
{
    bool created = false;

    if(type == OBJ_TEXT)
        created = ObjectCreate(0, name, type, 0, t1, p1);          // 文本: 1个锚点
    else
        created = ObjectCreate(0, name, type, 0, t1, p1, t2, p2);  // 趋势线: 2个锚点

    if(!created)
    {
        int err = GetLastError();
        Print("[GoldBridge] ⚠️ 创建标注对象失败: ", name, " Error ", err, " (", ErrorDescription(err), ")");
        return false;
    }

    ApplyAnnotateCommon(name);
    return true;
}

//+------------------------------------------------------------------+
//| 画一条水平虚线 + 末端价格文本标签                                  |
//| labelAnchor 同时决定文本位置: ANCHOR_LEFT_LOWER(线在上方) 或        |
//|                              ANCHOR_LEFT_UPPER(线在下方)          |
//+------------------------------------------------------------------+
bool DrawPriceLine(string name, string priceText, datetime t1, datetime t2,
                   double price, color lineColor, int priceDigits, int labelAnchor)
{
    bool created = SafeCreate(name, OBJ_TREND, t1, price, t2, price);

    if(created)
    {
        ObjectSetInteger(0, name, OBJPROP_COLOR,     lineColor);
        ObjectSetInteger(0, name, OBJPROP_STYLE,     STYLE_DASH);
        ObjectSetInteger(0, name, OBJPROP_WIDTH,     1);
        ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
        ObjectSetInteger(0, name, OBJPROP_RAY,       false);
    }

    bool textOk = SafeCreate(priceText, OBJ_TEXT, t2, price, 0, 0);

    if(textOk)
    {
        ObjectSetString(0,  priceText, OBJPROP_TEXT,   DoubleToString(price, priceDigits));
        ObjectSetInteger(0, priceText, OBJPROP_COLOR,  lineColor);
        ObjectSetInteger(0, priceText, OBJPROP_FONTSIZE, 8);
        ObjectSetInteger(0, priceText, OBJPROP_ANCHOR, labelAnchor);
    }

    return (created && textOk);
}

//+------------------------------------------------------------------+
//| 画信号箭头 (BUY=上箭头, SELL=下箭头)                               |
//| 优先贴在信号K线的低点下方/高点上方, 取不到该K线时退回 entry 价位     |
//+------------------------------------------------------------------+
bool DrawSignalArrow(string name, string direction, datetime signalTime,
                     double entry, double sigHigh, double sigLow, bool barOk)
{
    double anchorPrice = entry;

    if(barOk)
    {
        double offset = 3 * Point;
        if(direction == "BUY")
            anchorPrice = sigLow - offset;    // 放在K线低点略下方
        else
            anchorPrice = sigHigh + offset;   // 放在K线高点略上方
    }

    bool created = SafeCreate(name, OBJ_ARROW, signalTime, anchorPrice, 0, 0);

    if(created)
    {
        // 233 = 上箭头, 234 = 下箭头; 多空都用红色 (与截图一致)
        ObjectSetInteger(0, name, OBJPROP_ARROWCODE, (direction == "BUY") ? 233 : 234);
        ObjectSetInteger(0, name, OBJPROP_COLOR,     clrRed);
        ObjectSetInteger(0, name, OBJPROP_WIDTH,     2);
    }

    return created;
}

//+------------------------------------------------------------------+
//| 检查并执行图表标注指令 (独立通道)                                  |
//| 读取工具目录下的 annotate.json, 与 commands.json 完全无关,         |
//| 因此标注请求不会占用/覆盖下单的 response.json 应答。                |
//+------------------------------------------------------------------+
void CheckAnnotations()
{
    string filename = bridge_path + "annotate.json";
    
    if(!FileIsExist(filename, 0))
        return;
    
    // 读取标注指令文件
    int handle = FileOpen(filename, FILE_READ | FILE_TXT | FILE_ANSI);
    if(handle == INVALID_HANDLE)
        return;
    
    string content = "";
    while(!FileIsEnding(handle))
        content += FileReadString(handle) + "\n";
    FileClose(handle);
    
    // 与 CheckCommands 同一条铁律: 先确认内容完整, 再删除文件。
    // 若读到 Python 写了一半的文件就删除, 这条标注指令会被永久丢弃;
    // 若把残缺 JSON 当指令执行, 会画出错误的线。内容不完整就保留文件,
    // 等下一次 timer (500ms 后) 重试。
    if(StringLen(content) < 5 || StringFind(content, "\"action\"") == -1)
        return;
    
    string action = ExtractJsonString(content, "action");
    
    if(action == "")
        return;   // 还没写完, 保留文件等下次重试
    
    // 到这里指令完整, 删除文件防止重复执行
    FileDelete(filename, 0);
    
    Print("[GoldBridge] 收到标注指令: ", StringSubstr(content, 0, 120));
    
    if(action == "ANNOTATE")
        ExecuteAnnotate(content);
    else if(action == "CLEAR_ANNOTATIONS")
        ExecuteClearAnnotations(content);
    else
        Print("[GoldBridge] ⚠️ 未知标注操作: ", action);
}

//+------------------------------------------------------------------+
//| 执行图表标注 (action == "ANNOTATE")                               |
//| 同一策略重复发送会先删除旧对象再重建 → 原地更新, 不产生重复对象      |
//| 本通道无应答握手: 结果只写 Experts 日志, 绝不调用 WriteResponse。   |
//+------------------------------------------------------------------+
void ExecuteAnnotate(string json)
{
    string strategy  = ExtractJsonString(json, "strategy");
    string symbol    = ExtractJsonString(json, "symbol");
    string direction = ExtractJsonString(json, "direction");
    double entry     = ExtractJsonDouble(json, "entry");
    double sl        = ExtractJsonDouble(json, "sl");
    double tp        = ExtractJsonDouble(json, "tp");
    string timeStr   = ExtractJsonString(json, "signal_time");
    string label     = ExtractJsonString(json, "label");
    int    ticket    = (int)ExtractJsonDouble(json, "ticket");
    int    magic     = (int)ExtractJsonDouble(json, "magic");
    int    hours     = (int)ExtractJsonDouble(json, "hours");

    // ---- 参数校验: 非法参数不画任何对象, 只打日志 ----
    if(entry <= 0 || sl <= 0)
    {
        Print("[GoldBridge] ❌ 标注参数无效: entry=", DoubleToString(entry, 2),
              " sl=", DoubleToString(sl, 2), " (必须为正数), 已忽略本指令");
        return;
    }

    if(hours <= 0)  hours = 12;   // 默认画12小时长度的线段
    if(strategy == "") strategy = "default";

    string prefix = AnnotatePrefix(strategy);
    string nameArrow  = prefix + "arrow";
    string nameSlLine = prefix + "sl";
    string nameSlText = prefix + "sl_txt";
    string nameTpLine = prefix + "tp";
    string nameTpText = prefix + "tp_txt";

    // 取标注精度: 优先用指令里的 symbol, 否则用当前图表
    string digitsSymbol = symbol;
    if(digitsSymbol == "") digitsSymbol = Symbol();
    int priceDigits = (int)MarketInfo(digitsSymbol, MODE_DIGITS);
    if(priceDigits <= 0) priceDigits = Digits;

    // 信号时间解析失败则退回当前K线时间
    datetime signalTime = StringToTime(timeStr);
    if(signalTime == 0)
        signalTime = Time[0];

    // 定位信号K线, 用于把箭头放在影线外侧
    int shift = iBarShift(Symbol(), 0, signalTime, false);
    bool barOk = false;
    double sigHigh = entry;
    double sigLow  = entry;

    if(shift >= 0)
    {
        int bars = iBars(Symbol(), 0);
        if(bars > 0 && shift < bars)
        {
            sigHigh = iHigh(Symbol(), 0, shift);
            sigLow  = iLow(Symbol(), 0, shift);
            barOk = true;
        }
    }

    // 线段终点: 信号时间 + N小时 (直接按秒推算, 周末/停牌也不会退化成1根K线)
    datetime lineEnd = signalTime + (datetime)(hours * 3600);
    if(lineEnd <= TimeCurrent())
        lineEnd = TimeCurrent() + 3600;      // 信号较早时保证线段可见

    // ---- 重画前先删除同名旧对象, 保证不产生重复对象 ----
    if(ObjectFind(0, nameArrow)  >= 0) ObjectDelete(0, nameArrow);
    if(ObjectFind(0, nameSlLine) >= 0) ObjectDelete(0, nameSlLine);
    if(ObjectFind(0, nameSlText) >= 0) ObjectDelete(0, nameSlText);
    if(ObjectFind(0, nameTpLine) >= 0) ObjectDelete(0, nameTpLine);
    if(ObjectFind(0, nameTpText) >= 0) ObjectDelete(0, nameTpText);

    // ---- 画箭头 ----
    bool arrowOk = DrawSignalArrow(nameArrow, direction, signalTime,
                                   entry, sigHigh, sigLow, barOk);

    // ---- 画止损线 (红) + 价格标签 ----
    bool slOk = DrawPriceLine(nameSlLine, nameSlText, signalTime, lineEnd,
                              sl, clrRed, priceDigits, ANCHOR_LEFT_LOWER);

    // ---- 画止盈线 (浅蓝, 黑底清晰) + 价格标签; tp<=0 表示无止盈, 不画 ----
    bool tpOk = true;
    bool hasTp = (tp > 0);
    if(hasTp)
        tpOk = DrawPriceLine(nameTpLine, nameTpText, signalTime, lineEnd,
                             tp, clrDodgerBlue, priceDigits, ANCHOR_LEFT_UPPER);

    ChartRedraw();

    // ---- 组装一行日志摘要 (无应答通道, 用户从 Experts 日志确认) ----
    string displayTime = TimeToString(signalTime, TIME_DATE|TIME_SECONDS);
    string msg = "标注完成 " + direction + " " + strategy +
                 " 箭头时间=" + displayTime +
                 " entry=" + DoubleToString(entry, priceDigits) +
                 " sl=" + DoubleToString(sl, priceDigits);

    if(hasTp)
        msg += " tp=" + DoubleToString(tp, priceDigits);
    else
        msg += " tp=none";

    // 统计实际创建成功的对象数量
    int created = 0;
    if(arrowOk) created++;
    if(slOk)    created++;
    if(tpOk)    created++;

    msg += " 对象数=" + IntegerToString(created) + "/" +
           IntegerToString(hasTp ? 5 : 3);

    if(hours != 12)
        msg += " hours=" + IntegerToString(hours);
    if(!barOk)
        msg += " ⚠️ 信号时间未匹配到K线, 箭头按entry定位";
    if(direction == "BUY" && sl >= entry)
        msg += " ⚠️ 异常: BUY的止损 >= 入场价";
    if(direction == "SELL" && sl <= entry)
        msg += " ⚠️ 异常: SELL的止损 <= 入场价";
    if(ticket > 0)
        msg += " ticket=" + IntegerToString(ticket);
    if(magic != 0)
        msg += " magic=" + IntegerToString(magic);
    if(label != "")
        msg += " label=" + label;   // 仅写入日志, 不画到图上

    Print("[GoldBridge] ✅ ", msg);
}

//+------------------------------------------------------------------+
//| 清除标注 (action == "CLEAR_ANNOTATIONS")                          |
//| 可带可选 strategy: 给了就只删 GQ_<strategy>_, 否则删所有 GQ_ 开头的 |
//| 说明: 只删名字以 GQ_ 开头的对象, 不影响用户自己的画线。             |
//| 与 ExecuteAnnotate 一样: 只打日志, 不走 response.json 应答。        |
//+------------------------------------------------------------------+
void ExecuteClearAnnotations(string json)
{
    string strategy = ExtractJsonString(json, "strategy");
    string prefix;
    int    prefixLen;
    int    deleted;
    int    total;
    int    i;
    string name;
    string msg;

    if(strategy == "")
        prefix = "GQ_";                        // 清除全部 GQ_ 标注
    else
        prefix = AnnotatePrefix(strategy);     // 只清除该策略的标注

    prefixLen = StringLen(prefix);
    deleted   = 0;
    total     = ObjectsTotal(0);               // 主图窗口对象数

    // 倒序删除, 避免删除时索引前移导致漏删
    for(i = total - 1; i >= 0; i--)
    {
        name = ObjectName(0, i);
        if(StringSubstr(name, 0, prefixLen) == prefix)
        {
            if(ObjectDelete(0, name))
                deleted++;
        }
    }

    ChartRedraw();

    msg = "已清除标注对象 " + IntegerToString(deleted) + " 个 (前缀: " + prefix + ")";
    Print("[GoldBridge] 🧹 ", msg);
}
//+------------------------------------------------------------------+
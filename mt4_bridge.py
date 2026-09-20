"""
MT4 文件桥接模块
================
通过文件系统与 MT4 EA 通信:
- Python 写指令文件 → EA 读取并执行
- EA 写状态文件 → Python 读取结果

通信目录: MT4数据文件夹/MQL4/Files/DWX/
"""
import json
import os
import time
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

import config

log = logging.getLogger(__name__)


class MT4Bridge:
    """MT4 文件桥接"""

    def __init__(self):
        self.bridge_dir = config.BRIDGE_DIR
        self.orders_file = self.bridge_dir / "orders.json"
        self.positions_file = self.bridge_dir / "positions.json"
        self.account_file = self.bridge_dir / "account.json"
        self.commands_file = self.bridge_dir / "commands.json"
        self.response_file = self.bridge_dir / "response.json"
        # 图表标注走【独立文件】: commands.json 是"单槽位+应答"通道,
        # 如果标注也挤进去, EA对标注的应答可能被 _wait_response() 误当成
        # 某笔真实订单的结果 → 真单被误判为失败。可视化绝不能碰订单通道。
        self.annotate_file = self.bridge_dir / "annotate.json"

        # 最近一次 buy/sell 实际使用的价格与止损止盈价位 (供图表标注使用)
        self.last_levels: Dict = {}

        # 确保目录存在
        self.bridge_dir.mkdir(parents=True, exist_ok=True)

    def _read_json(self, filepath: Path) -> Optional[Dict]:
        """读取JSON文件"""
        try:
            if filepath.exists():
                with open(filepath, 'r') as f:
                    content = f.read().strip()
                    if content:
                        return json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"读取 {filepath.name} 失败: {e}")
        return None

    def _write_json(self, filepath: Path, data: Dict):
        """
        原子写入JSON (紧凑格式, 无空格, 确保EA能正确解析)

        先写临时文件再 os.replace() 原子替换: EA每500ms轮询一次,
        非原子写入时EA可能读到只写了一半的文件(截断的JSON),
        轻则指令丢失、重则按残缺参数下单。
        """
        tmp_path = filepath.with_name(filepath.name + ".tmp")
        try:
            with open(tmp_path, 'w') as f:
                json.dump(data, f, separators=(',', ':'), default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
        except IOError as e:
            log.error(f"写入 {filepath.name} 失败: {e}")

    def get_account(self) -> Optional[Dict]:
        """获取账户信息"""
        return self._read_json(self.account_file)

    def get_positions(self) -> List[Dict]:
        """获取当前持仓"""
        data = self._read_json(self.positions_file)
        if data and 'positions' in data:
            return data['positions']
        return []

    def get_open_orders(self) -> List[Dict]:
        """获取挂单"""
        data = self._read_json(self.orders_file)
        if data and 'orders' in data:
            return data['orders']
        return []

    def send_order(self, symbol: str, order_type: str, lots: float,
                   price: float = 0, sl: float = 0, tp: float = 0,
                   comment: str = "", magic: int = 0) -> bool:
        """
        发送交易指令

        Args:
            symbol: 交易品种 (XAUUSD)
            order_type: 订单类型 (BUY, SELL, BUYLIMIT, SELLLIMIT, BUYSTOP, SELLSTOP)
            lots: 手数
            price: 价格 (市价单传0)
            sl: 止损价
            tp: 止盈价 (0=不设)
            comment: 订单备注
            magic: 魔术号
        """
        command = {
            "action": "OPEN",
            "symbol": symbol,
            "type": order_type,
            "lots": lots,
            "price": price,
            "sl": sl,
            "tp": tp,
            "comment": comment,
            "magic": magic or config.MAGIC_NUMBER,
            "slippage": config.SLIPPAGE,
            "timestamp": datetime.now().isoformat(),
        }

        self._clear_response()  # 清除旧响应
        self._write_json(self.commands_file, command)
        log.info(f"📤 发送指令: {order_type} {symbol} {lots}手 SL={sl} TP={tp}")

        # 等待EA响应
        return self._wait_response(timeout=10)

    def close_order(self, ticket: int) -> bool:
        """平仓指定订单"""
        command = {
            "action": "CLOSE",
            "ticket": ticket,
            "timestamp": datetime.now().isoformat(),
        }

        self._clear_response()  # 清除旧响应
        self._write_json(self.commands_file, command)
        log.info(f"📤 发送平仓指令: ticket={ticket}")

        return self._wait_response(timeout=10)

    def modify_order(self, ticket: int, sl: float = 0, tp: float = 0) -> bool:
        """修改订单止损止盈"""
        command = {
            "action": "MODIFY",
            "ticket": ticket,
            "sl": sl,
            "tp": tp,
            "timestamp": datetime.now().isoformat(),
        }

        self._clear_response()  # 清除旧响应
        self._write_json(self.commands_file, command)
        return self._wait_response(timeout=10)

    def _clear_response(self):
        """清除旧的响应文件（在发送指令前调用）"""
        try:
            if self.response_file.exists():
                self.response_file.unlink()
        except:
            pass

    def _wait_response(self, timeout: int = 10) -> bool:
        """等待EA执行结果"""
        start = time.time()
        while time.time() - start < timeout:
            resp = self._read_json(self.response_file)
            if resp and 'success' in resp:
                success = resp.get('success', False)
                message = resp.get('message', '')
                if success:
                    log.info(f"✅ 执行成功: {message}")
                else:
                    log.error(f"❌ 执行失败: {message}")
                # 清除响应文件
                try:
                    self.response_file.unlink()
                except:
                    pass
                return success
            time.sleep(0.5)

        log.warning(
            f"⏰ 等待EA响应超时 ({timeout}秒) — 指令可能已被EA延迟执行! "
            f"该单不会记入交易日志, 会在下一轮 _sync_positions_tracking() "
            f"按持仓comment识别为“未知来源”并补登记。"
        )
        # 撤销尚未被EA读取的指令: 否则EA稍后读到它就会开出一张
        # Python已经认定"失败"的幽灵单(用户看到"莫名多出来的仓位")
        try:
            if self.commands_file.exists():
                self.commands_file.unlink()
                log.warning("   ↳ 已撤销未执行的指令文件 (避免EA延迟执行产生幽灵单)")
        except OSError as e:
            log.error(f"   ↳ 撤销指令文件失败: {e}")
        return False

    def annotate(self, strategy: str, direction: str, entry: float, sl: float,
                 tp: float, signal_time: str = "", label: str = "",
                 ticket: int = 0, hours: int = 12) -> bool:
        """
        让EA在主图表上标注推荐止损/止盈 (箭头 + 虚线 + 价格标签)

        这是纯可视化: 走独立的 annotate.json, 不等应答、不抛异常,
        失败也绝不影响下单与持仓管理。

        Args:
            entry/sl/tp: 绝对价格 (不是距离!)
            signal_time: 服务器时间字符串 'YYYY.MM.DD HH:MM:SS' (箭头锚点),
                         留空则由EA退化为当前K线
            hours: 画出的水平线跨度(小时)
        """
        try:
            if not strategy or entry <= 0 or sl <= 0:
                return False
            command = {
                "action": "ANNOTATE",
                "symbol": config.SYMBOL,
                "strategy": str(strategy),
                "direction": str(direction),
                "entry": round(float(entry), 2),
                "sl": round(float(sl), 2),
                "tp": round(float(tp or 0), 2),
                "signal_time": str(signal_time or ""),
                "label": str(label or ""),
                "ticket": int(ticket or 0),
                "hours": int(hours),
                "magic": config.MAGIC_NUMBER,
                "timestamp": datetime.now().isoformat(),
            }
            self._write_json(self.annotate_file, command)
            return True
        except Exception as e:
            log.warning(f"发送图表标注失败 (不影响交易): {e}")
            return False

    def clear_annotations(self, strategy: str = "") -> bool:
        """清除图表上的标注 (不传 strategy 则清除全部)"""
        try:
            command = {
                "action": "CLEAR_ANNOTATIONS",
                "strategy": str(strategy or ""),
                "timestamp": datetime.now().isoformat(),
            }
            self._write_json(self.annotate_file, command)
            return True
        except Exception as e:
            log.warning(f"清除图表标注失败: {e}")
            return False

    def is_connected(self) -> bool:
        """检查EA是否在线 (通过心跳文件)"""
        heartbeat = self.bridge_dir / "heartbeat.json"
        data = self._read_json(heartbeat)
        if data:
            last_beat = data.get('timestamp', '')
            # 如果心跳在30秒内
            try:
                beat_time = datetime.fromisoformat(last_beat)
                if (datetime.now() - beat_time).total_seconds() < 30:
                    return True
            except:
                pass
        return False

    def buy(self, lots: float = None, sl_pips: float = None, tp_pips: float = 0, comment: str = "") -> bool:
        """市价买入 + 自动止损止盈"""
        lots = lots or config.LOT_SIZE
        sl_pips = sl_pips or config.STOP_LOSS_PIPS

        # 获取当前价格来计算止损止盈
        account = self.get_account()
        current_price = 0.0
        if account and 'bid' in account:
            current_price = account['bid']
            sl_price = round(current_price - sl_pips, 2)
            tp_price = round(current_price + tp_pips, 2) if tp_pips > 0 else 0
        else:
            sl_price = 0
            tp_price = 0

        # 记录实际使用的价位, 供图表标注 (纯展示, 不影响下单)
        self.last_levels = {
            'price': current_price, 'sl': sl_price, 'tp': tp_price,
            'sl_pips': sl_pips, 'tp_pips': tp_pips,
        }

        return self.send_order(
            symbol=config.SYMBOL,
            order_type="BUY",
            lots=lots,
            sl=sl_price,
            tp=tp_price,
            comment=comment,
            magic=config.MAGIC_NUMBER,
        )

    def sell(self, lots: float = None, sl_pips: float = None, tp_pips: float = 0, comment: str = "") -> bool:
        """市价卖出 + 自动止损止盈"""
        lots = lots or config.LOT_SIZE
        sl_pips = sl_pips or config.STOP_LOSS_PIPS

        account = self.get_account()
        current_price = 0.0
        if account and 'ask' in account:
            current_price = account['ask']
            sl_price = round(current_price + sl_pips, 2)
            tp_price = round(current_price - tp_pips, 2) if tp_pips > 0 else 0
        else:
            sl_price = 0
            tp_price = 0

        self.last_levels = {
            'price': current_price, 'sl': sl_price, 'tp': tp_price,
            'sl_pips': sl_pips, 'tp_pips': tp_pips,
        }

        return self.send_order(
            symbol=config.SYMBOL,
            order_type="SELL",
            lots=lots,
            sl=sl_price,
            tp=tp_price,
            comment=comment,
            magic=config.MAGIC_NUMBER,
        )

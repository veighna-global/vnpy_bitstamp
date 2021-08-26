import hashlib
import hmac
import sys
import time
import uuid
from copy import copy
from datetime import datetime, timedelta
from urllib.parse import urlencode
from typing import Dict, Any, List
import pytz

import requests

from vnpy_rest import RestClient, Request, Response
from vnpy_websocket import WebsocketClient
from asyncio import run_coroutine_threadsafe

from vnpy.trader.constant import (
    Direction,
    Exchange,
    OrderType,
    Product,
    Status,
    Interval
)
from vnpy.trader.gateway import BaseGateway, LocalOrderManager
from vnpy.trader.object import (
    TickData,
    OrderData,
    TradeData,
    AccountData,
    ContractData,
    BarData,
    OrderRequest,
    CancelRequest,
    SubscribeRequest,
    HistoryRequest
)

from vnpy.trader.event import EVENT_TIMER
from vnpy.event import Event, EventEngine

# UTC时区
UTC_TZ = pytz.utc

# 实盘REST API地址
REST_HOST: str = "https://www.bitstamp.net/api/v2"

# 实盘Websocket API地址
WEBSOCKET_HOST: str = "wss://ws.bitstamp.net"

# 买卖方向映射
DIRECTION_BITSTAMP2VT: Dict[str, Direction] = {
    "0": Direction.LONG,
    "1": Direction.SHORT,
}

# 数据频率映射
INTERVAL_VT2BITSTAMP: Dict[Interval, int] = {
    Interval.MINUTE: 60,
    Interval.HOUR: 3600,
    Interval.DAILY: 86400,
}

# 时间间隔映射
TIMEDELTA_MAP: Dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}

# 合约名称全局缓存字典
name_symbol_map: Dict[str, str] = {}

# 合约数据全局缓存字典
symbol_contract_map: Dict[str, ContractData] = {}


class BitstampGateway(BaseGateway):
    """
    vn.py用于对接Bitstamp交易所的交易接口。
    """

    default_setting: Dict[str, Any] = {
        "key": "",
        "secret": "",
        "代理地址": "",
        "代理端口": 0,
    }

    exchanges: Exchange = [Exchange.BITSTAMP]

    def __init__(self, event_engine: EventEngine, gateway_name: str = "BITSTAMP") -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.order_manager: LocalOrderManager = LocalOrderManager(self)

        self.rest_api: "BitstampRestApi" = BitstampRestApi(self)
        self.ws_api: "BitstampWebsocketApi" = BitstampWebsocketApi(self)

    def connect(self, setting: dict):
        """连接交易接口"""
        key: str = setting["key"]
        secret: str = setting["secret"]
        proxy_host: str = setting["代理地址"]
        proxy_port: int = setting["代理端口"]

        self.rest_api.connect(key, secret, proxy_host, proxy_port)
        self.ws_api.connect(proxy_host, proxy_port)

        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        self.ws_api.subscribe(req)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.rest_api.send_order(req)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.rest_api.cancel_order(req)

    def query_account(self) -> None:
        """查询资金"""
        pass

    def query_position(self) -> None:
        """查询持仓"""
        pass

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        history: List[BarData] = []
        limit: int = 1000
        step: int = INTERVAL_VT2BITSTAMP[req.interval]
        base: str = req.symbol[:3].upper()
        quote: str = req.symbol[3:].upper()
        start_time: int = int(datetime.timestamp(req.start))

        while True:
            # 如果收到了最后一批数据则终止循环
            end_time: int = start_time + INTERVAL_VT2BITSTAMP[req.interval] * limit
            if int(datetime.timestamp(req.end)) < end_time:
                break

            # 创建查询参数
            params: dict = {
                "exchange": "bitstamp",
                "base": base,
                "quote": quote,
                "start": start_time,
                "end": end_time,
                "scale": step
            }

            resp: Response = requests.get(
                "https://api.blockchain.info/price/bar-series",
                params
            )

            if resp.status_code // 100 != 2:
                msg: str = f"获取历史数据失败，状态码：{resp.status_code}，信息：{resp.text}"
                self.write_log(msg)
                break
            else:
                data: dict = resp.json()
                if not data:
                    msg: str = f"获取历史数据为空，开始时间：{req.start}"
                    self.write_log(msg)
                    break

                buf: List[BarData] = []
                for d in data:
                    bar: BarData = BarData(
                        symbol=req.symbol,
                        exchange=req.exchange,
                        datetime=generate_datetime(d["start"]),
                        interval=req.interval,
                        volume=d["volume"],
                        open_price=d["open"],
                        high_price=d["high"],
                        low_price=d["low"],
                        close_price=d["close"],
                        gateway_name=self.gateway_name
                    )
                    buf.append(bar)

                history.extend(buf)

                begin: datetime = buf[0].datetime
                end: datetime = buf[-1].datetime
                msg: str = f"获取历史数据成功，{req.symbol} - {req.interval.value}，{begin} - {end}"
                self.write_log(msg)

                # 更新开始时间
                start_time: int = int(datetime.timestamp(end)) + INTERVAL_VT2BITSTAMP[req.interval]

        return history

    def close(self) -> None:
        """关闭连接"""
        self.rest_api.stop()
        self.ws_api.stop()

    def process_timer_event(self, event: Event) -> None:
        """定时事件处理"""
        self.rest_api.query_account()


class BitstampRestApi(RestClient):
    """Bitstamp的REST接口"""

    def __init__(self, gateway: BitstampGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: BitstampGateway = gateway
        self.gateway_name: str = gateway.gateway_name
        self.order_manager: LocalOrderManager = gateway.order_manager

        self.key: str = ""
        self.secret: str  = ""

        self.order_count: int = 1000000
        self.connect_time: int = 0

    def connect(
        self,
        key: str,
        secret: str,
        proxy_host: str,
        proxy_port: int,
    ) -> None:
        """连接REST服务器"""
        self.key = key
        self.secret = secret.encode()

        self.connect_time = (
            int(datetime.now(UTC_TZ).strftime("%y%m%d%H%M%S")) * self.order_count
        )

        self.init(REST_HOST, proxy_host, proxy_port)
        self.start()

        self.gateway.write_log("REST API启动成功")

        self.query_contract()
        self.query_account()

    def sign(self, request: Request) -> Request:
        """生成Bitstamp签名"""
        if request.method == "GET":
            return request

        timestamp: str = str(int(round(time.time() * 1000)))
        nonce: str = str(uuid.uuid4())
        content_type: str = "application/x-www-form-urlencoded"

        # 传入offset字典，避免传入空data导致200报错
        if not request.data:
            request.data = {"offset": "1"}

        payload_str: str = urlencode(request.data)

        message: str = "BITSTAMP " + self.key + \
            request.method + \
            "www.bitstamp.net/api/v2" + \
            request.path + \
            "" + \
            content_type + \
            nonce + \
            timestamp + \
            "v2" + \
            payload_str
        message: bytes = message.encode("utf-8")

        signature: bytes = hmac.new(
            self.secret,
            msg=message,
            digestmod=hashlib.sha256
        ).hexdigest().upper()

        request.headers = {
            "X-Auth": "BITSTAMP " + self.key,
            "X-Auth-Signature": signature,
            "X-Auth-Nonce": nonce,
            "X-Auth-Timestamp": timestamp,
            "X-Auth-Version": "v2",
            "Content-Type": content_type
        }
        request.data = payload_str

        return request

    def query_order(self) -> None:
        """查询未成交委托"""
        self.add_request(
            method="POST",
            path="/open_orders/all/",
            callback=self.on_query_order
        )

    def query_account(self) -> None:
        """查询资金"""
        self.add_request(
            method="POST",
            path="/balance/",
            callback=self.on_query_account
        )

    def query_contract(self) -> None:
        """查询合约信息"""
        self.add_request(
            method="GET",
            path="/trading-pairs-info/",
            callback=self.on_query_contract,
        )

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        sys_orderid: str = self.order_manager.get_sys_orderid(req.orderid)

        data: dict = {"id": sys_orderid}

        self.add_request(
            method="POST",
            path="/cancel_order/",
            data=data,
            callback=self.on_cancel_order,
            extra=req
        )

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        local_orderid: str = self.order_manager.new_local_orderid()
        order: OrderData = req.create_order_data(
            local_orderid,
            self.gateway_name
        )
        order.datetime = datetime.now(UTC_TZ)

        data: dict = {
            "amount": req.volume,
            "price": req.price
        }

        if req.direction == Direction.LONG:
            if req.type == OrderType.LIMIT:
                path: str = f"/buy/{req.symbol}/"
            elif req.type == OrderType.MARKET:
                path: str = f"/buy/market/{req.symbol}/"
        else:
            if req.type == OrderType.LIMIT:
                path: str = f"/sell/{req.symbol}/"
            elif req.type == OrderType.MARKET:
                path: str = f"/sell/market/{req.symbol}/"

        self.add_request(
            method="POST",
            path=path,
            data=data,
            callback=self.on_send_order,
            extra=order,
        )
        self.order_manager.on_order(order)
        return order.vt_orderid

    def on_query_order(self, data: dict, request: Request) -> None:
        """未成交委托查询回报"""
        for d in data:
            sys_orderid: str = d["id"]
            local_orderid: str = self.order_manager.get_local_orderid(sys_orderid)

            direction: Direction = DIRECTION_BITSTAMP2VT[d["type"]]
            name: str = d["currency_pair"]
            symbol: str = name_symbol_map[name]

            order: OrderData = OrderData(
                orderid=local_orderid,
                symbol=symbol,
                exchange=Exchange.BITSTAMP,
                price=float(d["price"]),
                volume=float(d["amount"]),
                traded=float(0),
                direction=direction,
                status=Status.NOTTRADED,
                datetime=generate_datetime(d["datetime"]),
                gateway_name=self.gateway_name,
            )
            self.order_manager.on_order(order)

        self.gateway.write_log("委托信息查询成功")

    def on_query_account(self, data: dict, request: Request) -> None:
        """资金查询回报"""
        for key in data.keys():
            if "balance" not in key:
                continue
            currency: str = key.replace("_balance", "")

            account: AccountData = AccountData(
                accountid=currency,
                balance=float(data[currency + "_balance"]),
                frozen=float(data[currency + "_reserved"]),
                gateway_name=self.gateway_name
            )
            self.gateway.on_account(account)

    def on_query_contract(self, data: dict, request: Request) -> None:
        """合约信息查询回报"""
        for d in data:
            pricetick: float = 1 / pow(10, d["counter_decimals"])
            min_volume: float = 1 / pow(10, d["base_decimals"])

            contract: ContractData = ContractData(
                symbol=d["url_symbol"],
                exchange=Exchange.BITSTAMP,
                name=d["name"],
                product=Product.SPOT,
                size=1,
                pricetick=pricetick,
                min_volume=min_volume,
                history_data=True,
                gateway_name=self.gateway_name,
            )
            self.gateway.on_contract(contract)

            symbol_contract_map[contract.symbol] = contract
            name_symbol_map[contract.name] = contract.symbol

        self.gateway.write_log("合约信息查询成功")

        self.query_order()

    def on_cancel_order(self, data: dict, request: Request) -> None:
        """委托撤单回报"""
        error: str = data.get("error", "")
        if error:
            self.gateway.write_log(error)
            return

        cancel_request: CancelRequest = request.extra
        local_orderid: str = cancel_request.orderid
        order: OrderData = self.order_manager.get_order_with_local_orderid(local_orderid)

        if order.is_active:
            order.status = Status.CANCELLED
            self.order_manager.on_order(order)

        self.gateway.write_log(f"撤单成功：{order.orderid}")

    def on_cancel_order_error(self, data, request) -> None:
        """委托撤单回报函数报错回报"""
        error_msg: str = data["error"]
        self.gateway.write_log(f"撤单请求出错，信息：{error_msg}")

    def on_send_order(self, data: dict, request: Request) -> None:
        """委托下单回报"""
        order: OrderData = request.extra

        status: str = data.get("status", None)
        if status and status == "error":
            order.status = Status.REJECTED
            self.order_manager.on_order(order)

            msg: str = data["reason"]["__all__"][0]
            self.gateway.write_log(msg)
            return

        sys_orderid: str = data["id"]
        self.order_manager.update_orderid_map(order.orderid, sys_orderid)

        order.status = Status.NOTTRADED
        self.order_manager.on_order(order)

    def on_send_order_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """委托下单回报函数报错回报"""
        if not issubclass(exception_type, ConnectionError):
            self.on_error(exception_type, exception_value, tb, request)

    def on_failed(self, status_code: int, request: Request) -> None:
        """请求失败的回调"""
        data: dict = request.response.json()
        reason: str = data["reason"]
        code: int = data["code"]

        msg: str = f"{request.path} 请求失败，状态码：{status_code}，错误信息：{reason}，错误代码: {code}"
        self.gateway.write_log(msg)

    def on_error(
        self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """请求触发异常的回调"""
        msg: str = f"触发异常，状态码：{exception_type}，信息：{exception_value}"
        self.gateway.write_log(msg)

        super().on_error(exception_type, exception_value, tb, request)


class BitstampWebsocketApi(WebsocketClient):
    """Bitstamp的Websocket接口"""

    def __init__(self, gateway: BitstampGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: BitstampGateway = gateway
        self.gateway_name: str = gateway.gateway_name
        self.order_manager: LocalOrderManager = gateway.order_manager

        self.subscribed: Dict[str, SubscribeRequest] = {}
        self.ticks: Dict[str, TickData] = {}

    def connect(self, proxy_host: str, proxy_port: int) -> None:
        """连接Websocket"""
        self.init(WEBSOCKET_HOST, proxy_host, proxy_port)
        self.start()

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("Websocket API连接成功")

        for req in self.subscribed.values():
            self.subscribe(req)

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        if req.symbol not in symbol_contract_map:
            self.gateway.write_log(f"找不到该合约代码{req.symbol}")
            return

        # 缓存订阅记录
        self.subscribed[req.symbol] = req
        if not self._active:
            return

        # 创建TICK对象
        tick: TickData = TickData(
            symbol=req.symbol,
            name=symbol_contract_map[req.symbol].name,
            exchange=Exchange.BITSTAMP,
            datetime=datetime.now(UTC_TZ),
            gateway_name=self.gateway_name,
        )

        for prefix in [
            "order_book_",
            "live_trades_",
            "live_orders_"
        ]:
            channel: str = f"{prefix}{req.symbol}"
            d: dict = {
                "event": "bts:subscribe",
                "data": {
                    "channel": channel
                }
            }
            self.ticks[channel] = tick
            self.send_packet(d)

    def on_packet(self, packet: dict) -> None:
        """推送数据回报"""
        event: str = packet["event"]

        if event == "trade":
            self.on_market_trade(packet)
        elif event == "data":
            self.on_market_depth(packet)
        elif "order_" in event:
            self.on_market_order(packet)
        elif event == "bts:request_reconnect":
            coro = self._ws.close()
            run_coroutine_threadsafe(coro, self._loop)

    def on_market_trade(self, packet: dict) -> None:
        """成交信息推送"""
        channel: str = packet["channel"]
        data: dict = packet["data"]

        tick: TickData = self.ticks[channel]
        tick.last_price = data["price"]
        tick.last_volume = data["amount"]

        dt: datetime = datetime.fromtimestamp(int(data["timestamp"]))
        tick.datetime = UTC_TZ.localize(dt)
        tick.localtime = datetime.now()

        self.gateway.on_tick(copy(tick))

        # 委托状态检查
        buy_orderid: str = str(data["buy_order_id"])
        sell_orderid: str = str(data["sell_order_id"])

        for sys_orderid in [buy_orderid, sell_orderid]:
            order: OrderData = self.order_manager.get_order_with_sys_orderid(
                sys_orderid)

            if order:
                order.traded += data["amount"]

                if order.traded < order.volume:
                    order.status = Status.PARTTRADED
                else:
                    order.status = Status.ALLTRADED

                self.order_manager.on_order(copy(order))

                trade: TradeData = TradeData(
                    symbol=order.symbol,
                    exchange=order.exchange,
                    orderid=order.orderid,
                    tradeid=data["id"],
                    direction=order.direction,
                    price=data["price"],
                    volume=data["amount"],
                    datetime=tick.datetime,
                    gateway_name=self.gateway_name
                )
                self.gateway.on_trade(trade)

    def on_market_depth(self, packet: dict) -> None:
        """行情深度推送"""
        channel: str = packet["channel"]
        data: dict = packet["data"]

        tick: TickData = self.ticks[channel]

        dt: datetime = datetime.fromtimestamp(int(data["timestamp"]))
        tick.datetime = UTC_TZ.localize(dt)

        bids: list = data["bids"]
        asks: list = data["asks"]

        for n in range(5):
            ix = n + 1

            bid_price, bid_volume = bids[n]
            tick.__setattr__(f"bid_price_{ix}", float(bid_price))
            tick.__setattr__(f"bid_volume_{ix}", float(bid_volume))

            ask_price, ask_volume = asks[n]
            tick.__setattr__(f"ask_price_{ix}", float(ask_price))
            tick.__setattr__(f"ask_volume_{ix}", float(ask_volume))

        tick.localtime = datetime.now()
        self.gateway.on_tick(copy(tick))

    def on_market_order(self, packet:dict) -> None:
        """委托信息推送"""
        event: str = packet["event"]
        data: dict = packet["data"]

        if event != "order_deleted":
            return

        sys_orderid: str = str(data["id"])
        order: OrderData = self.order_manager.get_order_with_sys_orderid(sys_orderid)

        if order and order.is_active():
            order.status = Status.CANCELLED
            self.order_manager.on_order(copy(order))


def generate_datetime(timestamp: str) -> datetime:
    """生成时间"""
    dt: datetime = datetime.fromtimestamp(timestamp)
    dt: datetime = UTC_TZ.localize(dt)
    return dt

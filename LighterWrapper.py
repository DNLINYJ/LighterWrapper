# 自定义实现的 lighter DEX API 包装
# 因为官方 API SDK 写的真是太 TM 狗屎了
import lighter
import datetime
import asyncio
import math
import warnings
import aiohttp

from lighter.signer_client import CreateOrderTxReq

from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class APIConfig:
    base_url: str
    api_secret_key: Dict[int, str]  # {account_index: api_secret_key}
    account_index: int

class LighterWrapper:
    def __init__(self, config: APIConfig):
        self.config = config
        self.api_client = lighter.ApiClient(configuration=lighter.Configuration(host=self.config.base_url))

        self.signer_instance = lighter.SignerClient(
            url=self.config.base_url,
            account_index=self.config.account_index,
            api_private_keys=self.config.api_secret_key,
        )

        self._order_api = lighter.OrderApi(self.api_client)

        self.request_headers = {"Content-Type": "application/json"}
        self._http_session: Optional[aiohttp.ClientSession] = None
        self._http_timeout = aiohttp.ClientTimeout(total=10)

        self.books_metadatas_cache = dict()  # 订单簿元数据缓存 用于加速获取 market_id 和 换算精度
                                             # {"symbol": {...}, ...}
    
    def _resolution_to_seconds(self, resolution: str) -> int:
        mapping = {
            "1m": 60,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "4h": 14400,
            "1d": 86400,
            "1w": 604800,
        }
        return mapping.get(resolution, 60)  # 默认返回 1 分钟

    async def _resize_amount(self, symbol: str, amount: float) -> int:
        if symbol in list(self.books_metadatas_cache.keys()):
            size_decimals = self.books_metadatas_cache[symbol]["supported_size_decimals"]
        else:
            books_metadata = await self.get_order_books_metadata(symbol)
            size_decimals = books_metadata["order_books"][0]["supported_size_decimals"]
            self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存
            
        scaled = amount * (10 ** size_decimals)
        rounded = round(scaled)
        # 最小精度检测：如果不是可用精度的整数倍，给出 Warning 并抛弃小数点
        if not math.isclose(scaled, rounded, rel_tol=0.0, abs_tol=1e-6):
            min_step = 10 ** (-size_decimals)
            warnings.warn(
                f"amount 精度超限，最小步进 {min_step}，传入 {amount}；已截断至最小精度",
                RuntimeWarning,
            )
            return int(scaled)
        return int(rounded) # 根据精度缩放数量

    async def _resize_price(self, symbol: str, price: float) -> int:
        if symbol in list(self.books_metadatas_cache.keys()):
            price_decimals = self.books_metadatas_cache[symbol]["supported_price_decimals"]
        else:
            books_metadata = await self.get_order_books_metadata(symbol)
            price_decimals = books_metadata["order_books"][0]["supported_price_decimals"]
            self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存

        scaled = price * (10 ** price_decimals)
        rounded = round(scaled)
        # 最小精度检测：如果不是可用精度的整数倍，给出 Warning 并抛弃小数点
        if not math.isclose(scaled, rounded, rel_tol=0.0, abs_tol=1e-6):
            min_step = 10 ** (-price_decimals)
            warnings.warn(
                f"price 精度超限，最小步进 {min_step}，传入 {price}；已截断至最小精度",
                RuntimeWarning,
            )
            return int(scaled)
        return int(rounded) # 根据精度缩放价格

    async def _close(self):
        await self.api_client.close()
        await self.signer_instance.close()
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    def _get_auth_token(self) -> Optional[str]:
        """
        _get_auth_token: 生成只读接口所需的 auth token
        返回 None 表示生成失败（此时部分接口可能仍可访问）
        """
        try:
            auth, err = self.signer_instance.create_auth_token_with_expiry()
            if err:
                return None
            return auth
        except Exception:
            return None

    async def _get_http_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(timeout=self._http_timeout)
        return self._http_session

    async def _http_get_json(self, path: str) -> dict:
        session = await self._get_http_session()
        url = self.config.base_url + path
        async with session.get(url, headers=self.request_headers) as response:
            if response.status == 200:
                return await response.json()
            text = await response.text()
            raise Exception(f"Failed to fetch: {response.status} {text}")

    @staticmethod
    def _tuple_to_dict(res_tuple):
        """
        _tuple_to_dict: 将 SDK 返回的 tuple 里的模型对象转成 dict
        """
        if not isinstance(res_tuple, tuple):
            return res_tuple
        out = []
        for item in res_tuple:
            if item is None:
                out.append(None)
                continue
            if hasattr(item, "to_dict"):
                try:
                    out.append(item.to_dict())
                    continue
                except Exception:
                    pass
            if hasattr(item, "model_dump"):
                try:
                    out.append(item.model_dump())
                    continue
                except Exception:
                    pass
            out.append(item)
        return tuple(out)

    async def get_market_id(self, symbol: str) -> int:
        """
        get_market_id: 获取指定交易对的 market_id
        """
        # 先查缓存
        if symbol in list(self.books_metadatas_cache.keys()):
            return int(self.books_metadatas_cache[symbol]["market_id"])
        
        # 缓存没有就请求接口
        books = await self._order_api.order_books()
        for b in books.order_books:
            if getattr(b, "symbol", None) == symbol:
                return int(b.market_id)
        raise ValueError(f"找不到 {symbol} 对应的 market_id")
    
    async def bulid_market_id_symbol_map(self) -> dict:
        """
        bulid_market_id_symbol_map: 构建 market_id 和 symbol 的映射字典
        """
        market_map = dict()
        books = await self._order_api.order_books()
        for b in books.order_books:
            market_map[int(b.market_id)] = getattr(b, "symbol", None)

        self.books_metadatas_cache = {getattr(b, "symbol", None): b.to_dict() for b in books.order_books}
    
    async def get_symbol_price_decimals(self, symbol: str) -> int:
        """
        get_symbol_price_decimals: 获取指定交易对的价格精度
        """
        if symbol in list(self.books_metadatas_cache.keys()):
            return int(self.books_metadatas_cache[symbol]["supported_price_decimals"])
        books_metadata = await self.get_order_books_metadata(symbol)
        price_decimals = books_metadata["order_books"][0]["supported_price_decimals"]
        self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存
        return price_decimals

    async def get_symbol_size_decimals(self, symbol: str) -> int:
        """
        get_symbol_size_decimals: 获取指定交易对的数量精度
        """
        if symbol in list(self.books_metadatas_cache.keys()):
            return int(self.books_metadatas_cache[symbol]["supported_size_decimals"])
        books_metadata = await self.get_order_books_metadata(symbol)
        size_decimals = books_metadata["order_books"][0]["supported_size_decimals"]
        self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存
        return size_decimals
    
    async def get_account(self) -> dict:
        """
        get_account: 获取账户余额信息

        返回格式示例: {'code': 200, 'total': 1, 'accounts': [...]}

        其中 accounts 列表中包含每个账户的详细信息，包括余额和持仓等。
        """
        # 手动封装获取账户余额
        path = f"/api/v1/account?by=index&value={self.config.account_index}"
        return await self._http_get_json(path)

    async def fetch_ohlcv(self, symbol: str, resolution: str = "1m", limit: int = 200, count_back: int = 0) -> dict:
        """
        fetch_ohlcv: 获取 K 线数据

        参数:
            symbol: 交易对符号, 如 "BTC"
            resolution: K 线周期, 如 "1m", "5m", "1h", "1d"
            limit: 返回的 K 线数量
            count_back: 从最新 K 线向前偏移的数量

        返回格式示例: {'code': 200, 'r': '1h', 'c': [{'t': ..., 'o': ..., 'h': ..., 'l': ..., 'c': ..., 'v': ..., 'V': ..., 'i': ...}, ...]}

        其中 c 列表中包含每根 K 线的详细信息.
        """
        # 手动封装 K 线数据获取
        market_id = await self.get_market_id(symbol)
        end_timestamp = int(datetime.datetime.now().timestamp())
        start_timestamp = end_timestamp - limit * self._resolution_to_seconds(resolution)

        path = f"/api/v1/candles?market_id={market_id}&resolution={resolution}&start_timestamp={start_timestamp}&end_timestamp={end_timestamp}&count_back={count_back}"
        return await self._http_get_json(path)

    async def get_order_books_metadata(self, symbol: str) -> dict:
        """
        get_order_books_metadata: 获取指定交易对的订单簿元数据

        返回格式示例: {'code': 200, 'order_books': [{'market_id': ..., 'symbol': 'BTC', 'taker_fee': ..., 'supported_size_decimals': ..., ...}]}

        其中 order_books 列表中包含每个交易对的详细信息.
        """
        # 直接用 order_api 接口获取订单簿元数据
        market_id = await self.get_market_id(symbol)
        order_books = await self._order_api.order_books(market_id=market_id)
        return order_books.to_dict()

    async def get_positions_by_symbol(self, symbol: str) -> list:
        """
        get_positions_by_symbol: 获取指定交易对的持仓信息

        参数:
            symbol: 交易对符号, 如 "BTC"

        返回格式示例: [{'market_id': ..., 'symbol': 'BTC', 'position': ..., 'avg_entry_price': ..., ...}, ...]

        其中列表中包含所有该交易对的持仓信息.
        """
        # 直接用 get_account 接口获取持仓信息
        account_data = await self.get_account()
        positions = account_data["accounts"][0]["positions"]
        symbol_positions = [pos for pos in positions if pos.get("symbol") == symbol]
        return symbol_positions

    async def fetch_open_orders(self, symbol: str, auth: Optional[str] = None) -> dict:
        """
        fetch_open_orders: 获取指定交易对的未完成订单

        参数:
            symbol: 交易对符号, 如 "BTC"（必须）
            auth: 可选认证 token，不传则自动生成
        返回格式示例:
            {'code': 200, 'orders': [...]}
        """
        market_id = await self.get_market_id(symbol)
        if auth is None:
            auth = self._get_auth_token()

        res = await self._order_api.account_active_orders(
            account_index=self.config.account_index,
            market_id=market_id,
            auth=auth,
        )
        return res.to_dict()

    async def fetch_closed_orders(
        self,
        symbol: Optional[str] = None,
        limit: int = 100,
        ask_filter: Optional[int] = None,
        between_timestamps: Optional[str] = None,
        cursor: Optional[str] = None,
        auth: Optional[str] = None,
    ) -> dict:
        """
        fetch_closed_orders: 获取历史订单（已完成/已取消）

        参数:
            symbol: 交易对符号, 如 "BTC"；不传则返回全市场（如后端支持）
            limit: 返回数量（1-100）
            ask_filter: 0 买单 / 1 卖单（可选）
            between_timestamps: "start,end" 毫秒时间戳字符串（可选）
            cursor: 翻页游标（可选）
            auth: 可选认证 token，不传则自动生成
        返回格式示例:
            {'code': 200, 'orders': [...], 'cursor': ...}
        """
        market_id = await self.get_market_id(symbol) if symbol else None
        if auth is None:
            auth = self._get_auth_token()

        res = await self._order_api.account_inactive_orders(
            account_index=self.config.account_index,
            limit=limit,
            market_id=market_id,
            ask_filter=ask_filter,
            between_timestamps=between_timestamps,
            cursor=cursor,
            auth=auth,
        )
        return res.to_dict()

    async def cancel_all_orders(self, symbol: str) -> dict:
        """
        cancel_all_orders: 取消指定交易对的全部未完成订单

        参数:
            symbol: 交易对符号, 如 "BTC"
        返回格式示例:
            {'code': 200, 'symbol': 'BTC', 'total': 3, 'canceled': 3, 'failed': [], 'results': [...]}
        """
        market_id = await self.get_market_id(symbol)
        open_orders = await self.fetch_open_orders(symbol)
        orders = open_orders.get("orders") or []

        results = []
        failed = []
        for o in orders:
            order_index = o.get("order_index") or o.get("order_id")
            if order_index is None:
                failed.append({"order": o, "error": "missing order_index"})
                continue
            res_tuple = await self.signer_instance.cancel_order(
                market_index=market_id,
                order_index=int(order_index),
            )
            res_tuple = self._tuple_to_dict(res_tuple)
            _, _, err = res_tuple
            if err:
                failed.append({"order_index": order_index, "error": err})
            results.append({"order_index": order_index, "result": res_tuple})

        return {
            "code": 200,
            "symbol": symbol,
            "total": len(orders),
            "canceled": len(orders) - len(failed),
            "failed": failed,
            "results": results,
        }

    async def update_symbol_leverage(self, symbol: str, leverage: float, margin_mode: str) -> tuple:
        """
        update_symbol_leverage: 更新指定交易对的杠杆倍数

        参数:
            symbol: 交易对符号, 如 "BTC"
            leverage: 杠杆倍数, 如 3.0
            margin_mode: 杠杆模式, "isolated" 或 "cross"

        返回格式示例: Tuple[str, RespSendTx, Optional[str]] - (tx_info, response, error)
        """
        market_id = await self.get_market_id(symbol)

        if margin_mode.lower() == "isolated": # 逐仓
            mode = lighter.SignerClient.ISOLATED_MARGIN_MODE
        else: # 全仓
            mode = lighter.SignerClient.CROSS_MARGIN_MODE
        
        res_tuple = await self.signer_instance.update_leverage(
            market_index=market_id,
            leverage=leverage,
            mode=mode,
        )

        return self._tuple_to_dict(res_tuple)

    async def calulate_worst_acceptable_price(self, symbol: str, side: str) -> float:
        """
        calulate_worst_acceptable_price: 计算市价单的最差可接受价格
        
        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"

        返回格式示例: 2500.0
        """
        symbol_price_decimals = await self.get_symbol_price_decimals(symbol)
        last_price = await self.fetch_ohlcv(symbol=symbol, resolution="1m", limit=1)
        last_close = last_price['c'][-1]['c']
        if side.lower() == "buy":
            worst_price = last_close + (8 * (10 ** -symbol_price_decimals)) # 买单取略高于收盘价的价格
        else:
            worst_price = last_close - (8 * (10 ** -symbol_price_decimals)) # 卖单取略低于收盘价的价格

        return worst_price

    async def create_market_order_with_tp_sl(
            self,
            symbol: str,
            side: str,
            quantity: float,
            take_profit_price: float,
            stop_loss_price: float,
        ) -> tuple:
        """
        create_market_order_with_tp_sl: 创建市价单并设置止盈止损

        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"
            quantity: 交易数量 (交易币种) 如 0.1 ETH
            take_profit_price: 止盈价格
            stop_loss_price: 止损价格
            custom_order_index: 自定义订单索引, 默认为 0

        返回格式示例: 
            (CreateOrder, RespSendTx, None)     # 成功返回
            (None, None, error_string)          # 失败返回
        """
        market_id = await self.get_market_id(symbol)
        symbol_price_decimals = await self.get_symbol_price_decimals(symbol)

        if side.lower() == "buy": # 多单
            is_ask_ioc = 0
            is_ask_tp_ls = 1
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="buy")
            worst_tp_price = take_profit_price - (8 * (10 ** -symbol_price_decimals)) # 止盈价格略低于目标价
            worst_sl_price = stop_loss_price + (8 * (10 ** -symbol_price_decimals)) # 止损价格略高于目标价
        else:  # 空单
            is_ask_ioc = 1
            is_ask_tp_ls = 0
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="sell")
            worst_tp_price = take_profit_price + (8 * (10 ** -symbol_price_decimals)) # 止盈价格略高于目标价
            worst_sl_price = stop_loss_price - (8 * (10 ** -symbol_price_decimals)) # 止损价格略低于目标价

        # https://deepwiki.com/elliottech/lighter-python/6.3-grouped-and-conditional-orders
        # 设置 BaseAmount = 0 此订单会创建一个与持仓规模关联的订单
        # 创建 IOC 市价单
        resized_amount = await self._resize_amount(symbol, quantity)

        ioc_order = CreateOrderTxReq(
            MarketIndex=market_id, # 交易对 ID
            # 服务器对 grouped orders 的限制：在 create_grouped_orders 里每个 CreateOrderTxReq.ClientOrderIndex 必须是 nil(0)，不能自定义
            ClientOrderIndex = 0, # 不允许自定义订单索引
            BaseAmount = resized_amount,  # 数量
            # 市价单这里的 Price 仍然要填，用作“最差可接受价格”
            Price = await self._resize_price(symbol, worst_price),     # 限价
            IsAsk = is_ask_ioc,  # 买卖方向，0 买 1 卖
            Type = self.signer_instance.ORDER_TYPE_MARKET, # 市价单
            TimeInForce = self.signer_instance.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            ReduceOnly = 0, # 非减仓单
            TriggerPrice = 0,
            OrderExpiry = 0,
        )

        # 创建止盈单和止损单
        take_profit_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=0,
            Price = await self._resize_price(symbol, worst_tp_price),
            IsAsk=is_ask_tp_ls, # 和入场单方向相反
            Type=self.signer_instance.ORDER_TYPE_TAKE_PROFIT_LIMIT,
            TimeInForce=self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1, # 仅减仓
            TriggerPrice = await self._resize_price(symbol, take_profit_price),
            OrderExpiry=-1,
        )

        stop_loss_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=0,
            Price = await self._resize_price(symbol, worst_sl_price),
            IsAsk=is_ask_tp_ls,
            Type=self.signer_instance.ORDER_TYPE_STOP_LOSS_LIMIT,
            TimeInForce=self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1,
            TriggerPrice = await self._resize_price(symbol, stop_loss_price),
            OrderExpiry=-1,
        )

        transaction = await self.signer_instance.create_grouped_orders(
            grouping_type=lighter.SignerClient.GROUPING_TYPE_ONE_TRIGGERS_A_ONE_CANCELS_THE_OTHER,
            orders=[ioc_order, take_profit_order, stop_loss_order],
        )

        return self._tuple_to_dict(transaction)

    async def create_market_order(
            self,
            symbol: str,
            side: str,
            quantity: float,
            reduce_only: bool,
            custom_order_index: int = 0
        ) -> tuple:
        """
        create_market_order: 创建市价单

        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"
            quantity: 交易数量 (交易币种) 如 0.1 ETH
            reduce_only: 是否为仅减仓单
            custom_order_index: 自定义订单索引, 默认为 0

        返回格式示例: 
            (CreateOrder, RespSendTx, None)     # 成功返回
            (None, None, error_string)          # 失败返回
        """
        market_id = await self.get_market_id(symbol)

        if side.lower() == "buy":
            is_ask = 0
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="buy")
        else:
            is_ask = 1
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="sell")

        res_tuple = await self.signer_instance.create_market_order(
            market_index=market_id,
            client_order_index=custom_order_index if custom_order_index else 0,
            base_amount=await self._resize_amount(symbol, quantity),
            is_ask=is_ask,
            avg_execution_price=await self._resize_amount(symbol, worst_price),
            reduce_only=reduce_only,
        )

        return self._tuple_to_dict(res_tuple)
    
    async def create_limit_order_with_tp_sl(
            self,
            symbol: str,
            side: str,
            quantity: float,
            price: float,
            take_profit_price: float,
            stop_loss_price: float,
            order_expiry: int = -1
        ) -> tuple:
        """
        create_limit_order_with_tp_sl: 创建限价单并设置止盈止损

        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"
            quantity: 交易数量 (交易币种) 如 0.1 ETH
            price: 限价价格
            take_profit_price: 止盈价格
            stop_loss_price: 止损价格
            order_expiry: 订单过期时间, Unix 时间戳格式, 默认为 -1 (表示使用默认过期时间 28 天)

        返回格式示例: 
            (CreateOrder, RespSendTx, None)     # 成功返回
            (None, None, error_string)          # 失败返回
        """
        market_id = await self.get_market_id(symbol)
        symbol_price_decimals = await self.get_symbol_price_decimals(symbol)

        if side.lower() == "buy": # 多单
            is_ask_tp_ls = 1
            worst_tp_price = take_profit_price - (8 * (10 ** -symbol_price_decimals)) # 止盈价格略低于目标价
            worst_sl_price = stop_loss_price + (8 * (10 ** -symbol_price_decimals)) # 止损价格略高于目标价
        else:  # 空单
            is_ask_tp_ls = 0
            worst_tp_price = take_profit_price + (8 * (10 ** -symbol_price_decimals)) # 止盈价格略高于目标价
            worst_sl_price = stop_loss_price - (8 * (10 ** -symbol_price_decimals)) # 止损价格略低于目标价

        resized_amount = await self._resize_amount(symbol, quantity)
        limit_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=resized_amount,
            Price=await self._resize_price(symbol, price),
            IsAsk=0 if side.lower() == "buy" else 1,
            Type=self.signer_instance.ORDER_TYPE_LIMIT,
            TimeInForce=self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=0,
            TriggerPrice=0,
            OrderExpiry=order_expiry,
        )

        take_profit_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=0, # 设 0 让系统自己算
            Price = await self._resize_price(symbol, worst_tp_price),
            IsAsk=is_ask_tp_ls, # 和入场单方向相反
            Type=self.signer_instance.ORDER_TYPE_TAKE_PROFIT_LIMIT,
            TimeInForce=self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1, # 仅减仓
            TriggerPrice = await self._resize_price(symbol, take_profit_price),
            OrderExpiry=order_expiry,
        )

        stop_loss_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=0, 
            Price = await self._resize_price(symbol, worst_sl_price),
            IsAsk=is_ask_tp_ls,
            Type=self.signer_instance.ORDER_TYPE_STOP_LOSS_LIMIT,
            TimeInForce=self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            ReduceOnly=1,
            TriggerPrice = await self._resize_price(symbol, stop_loss_price),
            OrderExpiry=order_expiry,
        )

        transaction = await self.signer_instance.create_grouped_orders(
            grouping_type=self.signer_instance.GROUPING_TYPE_ONE_TRIGGERS_A_ONE_CANCELS_THE_OTHER,
            orders=[limit_order, take_profit_order, stop_loss_order],
        )

        return self._tuple_to_dict(transaction)

    async def create_limit_order(
            self,
            symbol: str,
            side: str,
            quantity: float,
            price: float,
            reduce_only: bool,
            custom_order_index: int = 0,
            order_expiry: int = -1
        ) -> tuple: 
        """
        create_limit_order: 创建限价单

        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"
            quantity: 交易数量 (交易币种) 如 0.1 ETH
            price: 限价价格
            reduce_only: 是否为仅减仓单
            custom_order_index: 自定义订单索引, 默认为 0
            order_expiry: 订单过期时间, Unix 时间戳格式, 默认为 -1 (表示使用默认过期时间 28 天)

        返回格式示例: 
            (CreateOrder, RespSendTx, None)     # 成功返回
            (None, None, error_string)          # 失败返回
        """
        market_id = await self.get_market_id(symbol)

        if side.lower() == "buy":
            is_ask = 0
        else:
            is_ask = 1

        res_tuple = await self.signer_instance.create_order(
            market_index=market_id,
            client_order_index=custom_order_index if custom_order_index else 0,
            base_amount=await self._resize_amount(symbol, quantity),
            price=await self._resize_price(symbol, price),
            is_ask=is_ask,
            order_type=self.signer_instance.ORDER_TYPE_LIMIT,
            time_in_force=self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
            reduce_only=reduce_only,
            order_expiry=order_expiry
        )

        return self._tuple_to_dict(res_tuple)

    async def cancel_order(self, symbol: str, order_index: int) -> tuple:
        """
        cancel_order: 取消指定订单

        参数:
            symbol: 交易对符号, 如 "BTC"
            order_index: 要取消的订单索引

        返回格式示例: 
            (RespCancelOrders, None)     # 成功返回
            (None, error_string)         # 失败返回
        """
        market_id = await self.get_market_id(symbol)
        res_tuple = await self.signer_instance.cancel_order(
            market_index=market_id,
            client_order_index=order_index,
        )
        return self._tuple_to_dict(res_tuple)
    
    async def close_all_positions_for_symbol(self, symbol: str) -> tuple:
        """ 
        close_all_positions_for_symbol: 市价单平仓指定交易对的所有持仓
        参数:
            symbol: 交易对符号, 如 "BTC"
        返回格式示例:
            (CreateOrder, RespSendTx, None)     # 成功返回
            (None, None, error_string)          # 失败返回
        """

        market_id = await self.get_market_id(symbol)
        positions = await self.get_positions_by_symbol(symbol)
        if not positions:
            return None, None, "no position"
        pos = float(positions[0]["position"])
        side = int(positions[0]["sign"]) # -1 空头 1 多头
        if pos == 0:
            return None, None, "position is zero"

        if side == 1:  # 多头平仓 -> 卖
            is_ask = 1 
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="sell")
        else:          # 空头平仓 -> 买
            is_ask = 0
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="buy")

        qty = abs(pos)

        base_amount_int = await self._resize_amount(symbol, float(qty))
        price_int = await self._resize_price(symbol, worst_price)

        # 3. 下 reduce_only 的市价单（MARKET + IOC）
        # 这里不需要分组单，直接单笔订单就行
        res_tuple = await self.signer_instance.create_order(
            market_index=market_id,
            client_order_index=0,
            base_amount=base_amount_int,
            price=price_int, # 市价单里是“最差可接受价格”
            is_ask=is_ask, # 多仓用卖单平，空仓用买单平
            order_type=self.signer_instance.ORDER_TYPE_MARKET,
            time_in_force=self.signer_instance.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            reduce_only=True, # 关键，防止反向开仓
            trigger_price=0,
            order_expiry=0, # MARKET + IOC -> 0
        )

        return self._tuple_to_dict(res_tuple)

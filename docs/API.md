# API 文档

本文档覆盖 `LighterWrapper` 的所有对外接口（不以下划线开头的方法）。  
所有 `async` 方法都需要在事件循环中 `await` 调用。

## 通用说明
- **鉴权**：多数只读接口支持 `auth`，不传时自动生成只读 token。
- **返回值类型**：多数接口返回 `dict`；下单/撤单类返回 `tuple`（SDK 原始对象 + 交易响应 + error）。
- **错误处理**：网络/SDK 错误会抛异常；交易失败一般在返回 tuple 的第三项体现 error 字符串。

## 数据结构

### APIConfig
**参数**
- `base_url: str`：API 主机，例如 `https://mainnet.zklighter.elliot.ai`
- `api_secret_key: Dict[int, str]`：`{account_index: private_key}`
- `account_index: int`：当前账户索引

**示例**
```python
config = APIConfig(
    base_url="https://mainnet.zklighter.elliot.ai",
    api_secret_key={709464: "YOUR_PRIVATE_KEY"},
    account_index=709464,
)
```

---

## 初始化与生命周期

### `LighterWrapper(config: APIConfig)`
**说明**：初始化 SDK 客户端、Signer、SQLite（虚拟单持久化）。

**示例**
```python
wrapper = LighterWrapper(config)
```

### `await _close()`
**说明**：关闭 SDK 连接、aiohttp session，并停止自动匹配 loop。

---

## 元数据 / 行情

### `await get_market_id(symbol: str) -> int`
**参数**
- `symbol: str`：交易对，如 `"BTC"`

**返回**
- `market_id: int`

**示例**
```python
market_id = await wrapper.get_market_id("BTC")
```

---

### `await build_books_metadata_cache() -> dict`
**说明**：热构建币种对到 market_id 的缓存.  

**示例**
```python
await wrapper.build_books_metadata_cache()
```

---

### `await get_symbol_price_decimals(symbol: str) -> int`
**参数**
- `symbol: str`

**返回**
- 价格精度（小数位数）

---

### `await get_symbol_size_decimals(symbol: str) -> int`
**参数**
- `symbol: str`

**返回**
- 数量精度（小数位数）

---

### `await get_order_books_metadata(symbol: str) -> dict`
**参数**
- `symbol: str`

**返回**
```json
{
  "code": 200,
  "order_books": [
    {
      "symbol": "BTC",
      "market_id": 1,
      "supported_size_decimals": 5,
      "supported_price_decimals": 1,
      "supported_quote_decimals": 6,
      "min_base_amount": "0.00020",
      "min_quote_amount": "10.000000"
    }
  ]
}
```

---

### `await fetch_ohlcv(symbol: str, resolution="1m", limit=200, count_back=0) -> dict`
**参数**
- `symbol: str`
- `resolution: str`：`1m/5m/15m/30m/1h/4h/1d/1w`
- `limit: int`：返回数量
- `count_back: int`：向前偏移数量

**返回**
```json
{
  "code": 200,
  "r": "1m",
  "c": [
    {"t": 1700000000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "V": 15, "i": 123}
  ]
}
```

---

### `await fetch_ticker(symbol: str, include_raw: bool = False) -> dict`
**参数**
- `symbol: str`
- `include_raw: bool`：是否包含完整原始结构

**返回**
```json
{"code": 200, "ticker": {"last_trade_price": 88000.0, "daily_price_high": 90000.0}}
```

---

### `await get_latest_price(symbol: str) -> Optional[float]`
**说明**：优先从 recent_trades 获取最新成交价，无数据则回退到 ticker 的 `last_trade_price`。

---

### `await fetch_order_book_depth(symbol: str, limit=50) -> dict`
**参数**
- `symbol: str`
- `limit: int`：1~250

**返回**
```json
{"code": 200, "asks": [...], "bids": [...], "total_asks": 10, "total_bids": 10}
```

---

### `get_price_cache(symbol: str) -> Optional[dict]`
**说明**：读取本地行情缓存（不会发请求）。

**返回示例**
```json
{"last_trade": 88000.0, "best_bid": 87999.9, "best_ask": 88000.1, "age_sec": 0.5, "stale": false}
```

---

### `await refresh_ticker_cache(symbol: str) -> Optional[dict]`
**说明**：拉取 ticker 并更新缓存。

---

### `await refresh_order_book_cache(symbol: str, limit: int = 1) -> Optional[dict]`
**说明**：拉取盘口并更新 best_bid / best_ask 缓存。

---

### `start_market_cache_loop(symbols: list, interval_sec=2.0, use_ticker=True, use_order_book=True, depth_limit=1) -> None`
**说明**：后台轮询行情缓存（建议用于热路径下单）。

---

### `await stop_market_cache_loop() -> None`
**说明**：停止行情缓存轮询。

---

## 账户 / 持仓

### `await get_account() -> dict`
**说明**：账户余额、持仓与资产信息。

**返回**
```json
{"code": 200, "accounts": [...]}
```

---

### `await get_positions_by_symbol(symbol: str) -> list`
**参数**
- `symbol: str`

**返回**
```json
[
  {"market_id": 1, "symbol": "BTC", "position": "0.00100", "avg_entry_price": "88000.0"}
]
```

---

## 订单列表

### `await fetch_open_orders(symbol: str, auth: Optional[str]=None) -> dict`
**参数**
- `symbol: str`
- `auth: Optional[str]`：只读 token（可选）

**返回**
```json
{"code": 200, "orders": [...]}
```

---

### `await fetch_closed_orders(symbol: Optional[str]=None, limit=100, ask_filter=None, between_timestamps=None, cursor=None, auth=None) -> dict`
**参数**
- `symbol: Optional[str]`
- `limit: int`：1~100
- `ask_filter: Optional[int]`：0 买 / 1 卖
- `between_timestamps: Optional[str]`：`"start,end"` 毫秒时间戳
- `cursor: Optional[str]`
- `auth: Optional[str]`

**返回**
```json
{"code": 200, "orders": [...], "next_cursor": "..."}
```

---

## 成交记录（fills）

### `await fetch_fills(symbol=None, market_id=None, order_index=None, limit=100, sort_by="timestamp", cursor=None, ask_filter=None, role="all", trade_type="all", aggregate=False, auth=None) -> dict`
**参数**
- `symbol: Optional[str]`
- `market_id: Optional[int]`
- `order_index: Optional[int]`
- `limit: int`：1~100
- `sort_by: str`：`block_height / timestamp / trade_id`
- `cursor: Optional[str]`
- `ask_filter: Optional[int]`：0 买 / 1 卖
- `role: str`：`all/maker/taker`
- `trade_type: str`：`all/trade/liquidation/deleverage/market-settlement`
- `aggregate: bool`
- `auth: Optional[str]`

**返回**
```json
{"code": 200, "trades": [...], "next_cursor": "..."}
```

---

### `await get_order_fills(symbol=None, order_index=None, limit=100, cursor=None, auth=None) -> dict`
**参数**
- `order_index: int`（必填）
- 其余同 `fetch_fills`

---

## 订单状态跟踪

### `await get_order_status(symbol=None, order_index=None, order_id=None, client_order_index=None, virtual_order_id=None, include_closed=True, closed_limit=100, include_fills=False, fills_limit=100) -> dict`
**说明**
- 先查 open，再查 closed
- 额外返回 `fill_status` 与 `filled_ratio`

**参数**
- `symbol: Optional[str]`：查真实订单时必填
- `order_index/order_id/client_order_index`：至少一个
- `virtual_order_id`：查询虚拟单状态
- `include_closed: bool`
- `closed_limit: int`
- `include_fills: bool`：是否带成交记录
- `fills_limit: int`

**返回示例**
```json
{
  "status": "open",
  "source": "open",
  "order": {...},
  "fill_status": "partial",
  "filled_ratio": 0.3,
  "fills": {...}
}
```

---

## 虚拟单与自动匹配

### `get_virtual_order(virtual_order_id: str) -> Optional[dict]`
**说明**：读取虚拟单信息。

---

### `list_virtual_orders() -> dict`
**说明**：返回所有虚拟单（内存缓存）。

---

### `await reconcile_virtual_orders(symbols=None, include_closed=True, closed_limit=100, include_entry=False, match_window_sec=300) -> dict`
**说明**：根据 side/reduce_only/价格/触发价/时间窗口匹配虚拟单与真实订单。

**返回**
```json
{"total": 3, "updated": 2, "matched": 3}
```

---

### `start_reconcile_loop(interval_sec=5.0, symbols=None, include_closed=True, closed_limit=100, include_entry=False, match_window_sec=300) -> None`
**说明**：启动自动匹配 loop。

---

### `await stop_reconcile_loop() -> None`
**说明**：停止自动匹配 loop。

---

## 下单 / 撤单 / 平仓

### `await create_limit_order(symbol, side, quantity, price, reduce_only, custom_order_index=0, order_expiry=-1) -> tuple`
**参数**
- `symbol: str`
- `side: "buy" | "sell"`
- `quantity: float`
- `price: float`
- `reduce_only: bool`
- `custom_order_index: int`：自定义索引（0 表示自动）
- `order_expiry: int`：订单过期时间戳（秒）；`-1` 使用默认

**返回**
```python
(CreateOrder, RespSendTx, error)
```

---

### `await create_market_order(symbol, side, quantity, reduce_only, custom_order_index=0) -> tuple`
**说明**：市价单内部使用“最差可接受价格”。

**返回**
```python
(CreateOrder, RespSendTx, error)
```

---

### `await create_limit_order_with_tp_sl(symbol, side, quantity, price, take_profit_price, stop_loss_price, order_expiry=-1) -> tuple`
**说明**：限价单 + TP/SL，内部将 `order_expiry` 解析为具体时间戳。

---

### `await create_limit_order_with_tp_sl_virtual(...) -> (virtual_id, tuple)`
**说明**：组合限价单 + TP/SL，同时生成虚拟单。

**返回**
```python
(virtual_order_id, (CreateGroupedOrders, RespSendTx, error))
```

---

### `await create_market_order_with_tp_sl(symbol, side, quantity, take_profit_price, stop_loss_price, tp_sl_market=True) -> (virtual_id, tuple)`
**说明**：市价单 + TP/SL，`tp_sl_market=True` 时 TP/SL 使用市价触发单，否则使用限价触发单。

---

### `await cancel_order(symbol, order_index) -> tuple`
**返回**
```python
(CancelOrder, RespSendTx, error)
```

---

### `await cancel_all_orders(symbol) -> dict`
**返回**
```json
{"code": 200, "symbol": "BTC", "total": 2, "canceled": 2, "failed": []}
```

---

### `await close_all_positions_for_symbol(symbol) -> tuple`
**说明**：市价平仓。若无持仓返回 `(None, None, "no position")`。

---

### `await update_symbol_leverage(symbol, leverage, margin_mode) -> tuple`
**参数**
- `margin_mode: "isolated" | "cross"`

---

### `await calulate_worst_acceptable_price(symbol, side, max_slippage=0.005) -> float`
**说明**：基于**行情缓存**计算最差可接受价格；使用 `max_slippage` 直接乘价计算；若无缓存，回退到 1m close（慢路径）。

---

## 常见调用示例

### 查询订单状态 + 成交记录
```python
status = await wrapper.get_order_status(
    symbol="BTC",
    order_index=123456,
    include_fills=True
)
```

### 启动自动匹配循环
```python
wrapper.start_reconcile_loop(interval_sec=5, symbols=["BTC"])
# ...
await wrapper.stop_reconcile_loop()
```

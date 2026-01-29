# 典型用例教程

本教程展示常见的量化后台/脚本场景用法（查询、下单、虚拟单追踪、成交记录与订单状态）。

## 1. 初始化与基础查询
```python
import asyncio
from LighterWrapper import APIConfig, LighterWrapper

async def main():
    config = APIConfig(
        base_url="https://mainnet.zklighter.elliot.ai",
        api_secret_key={YOUR_PRIVATE_KEY_INDEX: "YOUR_PRIVATE_KEY"},
        account_index=YOUR_ACCOUNT_INDEX,
    )

    wrapper = LighterWrapper(config)

    # 开启自动匹配 loop
    wrapper.start_reconcile_loop(
        interval_sec=3, # 每 3 秒同步一次虚拟订单
        symbols=["BTC"], # 同步的交易对列表
        include_closed=True,
        closed_limit=100,
    )

    # 热构建交易对 -> market_id 缓存
    await wrapper.build_books_metadata_cache()

    try:
        account = await wrapper.get_account()
        positions = await wrapper.get_positions_by_symbol("BTC")
        ohlcv = await wrapper.fetch_ohlcv("BTC", resolution="1m", limit=10)
        ticker = await wrapper.fetch_ticker("BTC")
        last_price = await wrapper.get_latest_price("BTC")
        depth = await wrapper.fetch_order_book_depth("BTC", limit=20)
        print(account, positions, ohlcv)
        print(ticker, last_price, depth)
    finally:
        await wrapper._close()

asyncio.run(main())
```

## 2. 下限价单 + 撤单
```python
limit_res = await wrapper.create_limit_order(
    symbol="BTC",
    side="buy",
    quantity=0.001,
    price=88000.0,
    reduce_only=False,
    custom_order_index=12345,
)

status = await wrapper.get_order_status(
    symbol="BTC",
    client_order_index=12345,
)

cancel_res = await wrapper.cancel_order("BTC", 12345)
```

## 3. 组合限价单（TP/SL）+ 虚拟单追踪
```python
virtual_id, res = await wrapper.create_limit_order_with_tp_sl_virtual(
    symbol="BTC",
    side="buy",
    quantity=0.001,
    price=88000.0,
    take_profit_price=93000.0,
    stop_loss_price=85000.0,
)

vo = wrapper.get_virtual_order(virtual_id)
reconcile = await wrapper.reconcile_virtual_orders(symbols=["BTC"])
```

## 4. 市价单 + TP/SL（带虚拟单号）
```python
virtual_id, res = await wrapper.create_market_order_with_tp_sl(
    symbol="BTC",
    side="buy",
    quantity=0.001,
    take_profit_price=93000.0,
    stop_loss_price=85000.0,
)
```

## 5. 成交记录与订单状态
```python
fills = await wrapper.fetch_fills(symbol="BTC", limit=20)

status = await wrapper.get_order_status(
    symbol="BTC",
    order_index=123456,
    include_fills=True,
)
```

## 6. 自动匹配循环
```python
wrapper.start_reconcile_loop(interval_sec=5, symbols=["BTC"])
# ...
await wrapper.stop_reconcile_loop()
```

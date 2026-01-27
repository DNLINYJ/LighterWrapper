# 典型用例教程

本教程展示常见的量化后台/脚本场景用法（查询、下单、虚拟单追踪、成交记录与订单状态）。

## 1. 初始化与基础查询
```python
import asyncio
from LighterWrapper import APIConfig, LighterWrapper

async def main():
    config = APIConfig(
        base_url="https://mainnet.zklighter.elliot.ai",
        api_secret_key={709464: "YOUR_PRIVATE_KEY"},
        account_index=709464,
    )

    wrapper = LighterWrapper(config)
    try:
        await wrapper.bulid_market_id_symbol_map()
        account = await wrapper.get_account()
        positions = await wrapper.get_positions_by_symbol("BTC")
        ohlcv = await wrapper.fetch_ohlcv("BTC", resolution="1m", limit=10)
        print(account, positions, ohlcv)
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

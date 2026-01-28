# LighterWrapper
Lighter DEX Python SDK 的轻量封装，面向量化交易与后台展示场景，提供下单、撤单、持仓、K线、虚拟单追踪、成交记录与订单状态跟踪等功能。

## 主要特性
- 统一的下单/撤单接口（限价、市价、组合单 TP/SL）
- 虚拟订单号（适配组合单无真实 order_id 的情况）
- 自动匹配虚拟单 ↔ 实际订单（可定时 loop）
- 成交记录（fills）与订单状态跟踪（部分成交/完成）
- ticker / 最新价 / 盘口深度
- 行情缓存与轮询（用于热路径下单）
- 轻量 SQLite 持久化（按 account_index 命名）
- 全异步调用（aiohttp + SDK async）

## 依赖与安装
```bash
pip install aiohttp
pip install git+https://github.com/elliottech/lighter-python.git
```

## 快速开始
```python
import asyncio
from LighterWrapper import APIConfig, LighterWrapper

async def main():
    config = APIConfig(
        base_url="https://mainnet.zklighter.elliot.ai",
        api_secret_key={api_index: "YOUR_PRIVATE_KEY"},
        account_index=account_index,
    )

    wrapper = LighterWrapper(config)
    try:
        await wrapper.build_books_metadata_cache()

        # 查询账户
        account = await wrapper.get_account()
        print(account)

        # 下限价单
        res = await wrapper.create_limit_order(
            symbol="BTC",
            side="buy",
            quantity=0.001,
            price=88000.0,
            reduce_only=False,
            custom_order_index=12345,
        )
        print(res)
    finally:
        await wrapper._close()

asyncio.run(main())
```

## 测试脚本
`test_lighter_wrapper.py` 覆盖大部分 API。为了避免误下单，默认不执行交易。

### 必选环境变量（二选一）
- `LIGHTER_ACCOUNT_INDEX` + `LIGHTER_API_SECRET_KEY`
- 或 `LIGHTER_API_SECRET_KEY_MAP='{"your_account_id":"your_key"}'`

### 可选环境变量
- `LIGHTER_BASE_URL`
- `LIGHTER_TEST_SYMBOL`（默认 BTC）
- `LIGHTER_TEST_QTY`（默认 0.001）
- `LIGHTER_LIMIT_PRICE_MULTIPLIER`（默认 0.95）
- `LIGHTER_TP_MULTIPLIER` / `LIGHTER_SL_MULTIPLIER`（默认 1.05 / 0.95）
- `RUN_TRADING_TESTS=1`（启用下单/撤单）
- `RUN_MARKET_TESTS=1`（启用市价 TP/SL）
- `RUN_CLOSE_POSITIONS=1`（启用平仓）
- `RUN_RECONCILE_LOOP=1`（启用自动匹配 loop）
- `RECONCILE_LOOP_INTERVAL`（默认 5 秒）

### 运行
```bash
python test_lighter_wrapper.py
```

## 设计说明
### 虚拟单与自动匹配
组合单无法自定义 `ClientOrderIndex`，因此引入虚拟订单号：
- 下单时生成 `virtual_order_id`
- 将订单信息持久化到 `{account_index}.db`
- `reconcile_virtual_orders` 根据 side / reduce_only / 价格 / 触发价 / 时间窗口做匹配

### 订单状态跟踪
`get_order_status` 会从 open/closed 订单列表中查找，并派生：
`fill_status`（unfilled/partial/filled/canceled）与 `filled_ratio`。

## 已知限制
- 匹配依赖启发式策略，极端情况下可能误匹配
- quote 精度未强校验（若 API 需要严格 quote 精度，可能报错）
- SQLite 为同步写入，极高频场景会有轻微阻塞

## 文档
- API 文档：`docs/API.md`
- 典型用例教程：`docs/USAGE.md`

## 安全提示
所有交易接口均直接发送交易到主网，请谨慎使用测试金额。

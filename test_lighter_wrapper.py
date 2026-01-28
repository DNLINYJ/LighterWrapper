import asyncio
import json
import os
import time
from typing import Optional

from LighterWrapper import APIConfig, LighterWrapper

def _load_config_from_env() -> Optional[APIConfig]:
    base_url = os.getenv("LIGHTER_BASE_URL", "https://mainnet.zklighter.elliot.ai")
    account_index_raw = os.getenv("LIGHTER_ACCOUNT_INDEX")
    api_secret_key = os.getenv("LIGHTER_API_SECRET_KEY")
    api_secret_key_map = os.getenv("LIGHTER_API_SECRET_KEY_MAP")

    if not api_secret_key_map and (not account_index_raw or not api_secret_key):
        print("请设置环境变量：")
        print("  LIGHTER_ACCOUNT_INDEX, LIGHTER_API_SECRET_KEY")
        print("或使用 JSON 映射：")
        print("  LIGHTER_API_SECRET_KEY_MAP='{\"your_account_id\":\"your_key\"}'")
        return None

    if api_secret_key_map:
        mapping = json.loads(api_secret_key_map)
        api_secret_key_dict = {int(k): v for k, v in mapping.items()}
        if account_index_raw:
            account_index = int(account_index_raw)
        else:
            account_index = next(iter(api_secret_key_dict.keys()))
    else:
        account_index = int(account_index_raw)
        api_secret_key_dict = {account_index: api_secret_key}

    return APIConfig(
        base_url=base_url,
        api_secret_key=api_secret_key_dict,
        account_index=account_index,
    )

def _section(title: str) -> None:
    print(f"\n=== {title} ===")


def _json_default(obj):
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if isinstance(obj, set):
        return list(obj)
    return str(obj)


def _print_json(label: str, data) -> None:
    print(f"{label}: {json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)}")


async def main() -> None:
    config = _load_config_from_env()
    if config is None:
        return

    run_trading = os.getenv("RUN_TRADING_TESTS", "0") == "1"
    run_market = os.getenv("RUN_MARKET_TESTS", "0") == "1"
    run_close_positions = os.getenv("RUN_CLOSE_POSITIONS", "0") == "1"
    run_reconcile_loop = os.getenv("RUN_RECONCILE_LOOP", "0") == "1"

    symbol = os.getenv("LIGHTER_TEST_SYMBOL", "BTC")
    test_qty = float(os.getenv("LIGHTER_TEST_QTY", "0.001"))
    limit_multiplier = float(os.getenv("LIGHTER_LIMIT_PRICE_MULTIPLIER", "0.99"))
    tp_multiplier = float(os.getenv("LIGHTER_TP_MULTIPLIER", "1.05"))
    sl_multiplier = float(os.getenv("LIGHTER_SL_MULTIPLIER", "0.95"))
    reconcile_interval = float(os.getenv("RECONCILE_LOOP_INTERVAL", "5"))

    wrapper = LighterWrapper(config)
    try:
        _section("基础初始化")
        await wrapper.build_books_metadata_cache()

        # 基础查询与元数据
        _section("市场元数据/行情")
        market_id = await wrapper.get_market_id(symbol)
        metadata = await wrapper.get_order_books_metadata(symbol)
        price_decimals = await wrapper.get_symbol_price_decimals(symbol)
        size_decimals = await wrapper.get_symbol_size_decimals(symbol)
        ohlcv = await wrapper.fetch_ohlcv(symbol=symbol, resolution="1m", limit=1)
        ticker = await wrapper.fetch_ticker(symbol)
        latest_price = await wrapper.get_latest_price(symbol)
        depth = await wrapper.fetch_order_book_depth(symbol, limit=20)
        last_close = ohlcv["c"][-1]["c"]

        print("market_id:", market_id)
        _print_json("metadata", metadata)
        print("price_decimals:", price_decimals)
        print("size_decimals:", size_decimals)
        _print_json("ohlcv(last)", ohlcv)
        _print_json("ticker", ticker)
        print("latest_price:", latest_price)
        _print_json("order_book_depth", depth)

        # 账户与订单
        _section("账户/持仓/订单")
        account = await wrapper.get_account()
        positions = await wrapper.get_positions_by_symbol(symbol)
        open_orders = await wrapper.fetch_open_orders(symbol)
        closed_orders = await wrapper.fetch_closed_orders(symbol=symbol, limit=5)

        _print_json("account", account)
        _print_json("positions", positions)
        _print_json("open_orders", open_orders)
        _print_json("closed_orders", closed_orders)

        fills = await wrapper.fetch_fills(symbol=symbol, limit=5)
        _print_json("fills", fills)

        if closed_orders.get("orders"):
            sample_order = closed_orders["orders"][0]
            sample_order_index = sample_order.get("order_index") or sample_order.get("orderId")
            if sample_order_index is not None:
                status_with_fills = await wrapper.get_order_status(
                    symbol=symbol,
                    order_index=int(sample_order_index),
                    include_fills=True,
                )
                _print_json("get_order_status (with fills)", status_with_fills)

        if run_reconcile_loop:
            _section("自动匹配 Loop")
            wrapper.start_reconcile_loop(
                interval_sec=reconcile_interval,
                symbols=[symbol],
                include_closed=True,
                closed_limit=100,
            )

        if not run_trading:
            print("RUN_TRADING_TESTS=1 未开启，仅执行查询类测试。")
            return

        # 1) 限价单下单 + 撤单
        _section("限价单下单 + 撤单")
        limit_price = last_close * limit_multiplier
        custom_order_index = int(time.time() * 1000) % (2**31)
        limit_res = await wrapper.create_limit_order(
            symbol=symbol,
            side="buy",
            quantity=test_qty,
            price=limit_price,
            reduce_only=False,
            custom_order_index=custom_order_index,
        )
        _print_json("create_limit_order", limit_res)
        status_res = await wrapper.get_order_status(
            symbol=symbol,
            client_order_index=custom_order_index,
        )
        _print_json("get_order_status", status_res)

        await asyncio.sleep(0.5)
        cancel_res = await wrapper.cancel_order(symbol, custom_order_index)
        _print_json("cancel_order", cancel_res)

        # 2) 组合限价单（带 TP/SL）+ 虚拟单号 + 自动匹配
        _section("组合限价单 TP/SL + 虚拟单 + 匹配")
        take_profit_price = last_close * tp_multiplier
        stop_loss_price = last_close * sl_multiplier

        virtual_id, grouped_res = await wrapper.create_limit_order_with_tp_sl_virtual(
            symbol=symbol,
            side="buy",
            quantity=test_qty,
            price=limit_price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
        )
        print("virtual_id:", virtual_id)
        _print_json("create_limit_order_with_tp_sl_virtual", grouped_res)
        _print_json("virtual_order", wrapper.get_virtual_order(virtual_id))
        _print_json(
            "get_order_status (virtual)",
            await wrapper.get_order_status(virtual_order_id=virtual_id),
        )

        await asyncio.sleep(1)
        reconcile_res = await wrapper.reconcile_virtual_orders(symbols=[symbol])
        _print_json("reconcile_virtual_orders", reconcile_res)
        _print_json("virtual_order_after_reconcile", wrapper.get_virtual_order(virtual_id))

        # 3) 批量撤单
        _section("批量撤单")
        cancel_all_res = await wrapper.cancel_all_orders(symbol)
        _print_json("cancel_all_orders", cancel_all_res)

        # 4) 市价单 + TP/SL（风险较高，需手动开启）
        if run_market:
            _section("市价单 TP/SL")
            market_virtual_id, market_res = await wrapper.create_market_order_with_tp_sl(
                symbol=symbol,
                side="buy",
                quantity=test_qty,
                take_profit_price=take_profit_price,
                stop_loss_price=stop_loss_price,
            )
            print("market_virtual_id:", market_virtual_id)
            _print_json("create_market_order_with_tp_sl", market_res)

            await asyncio.sleep(1)
            reconcile_market = await wrapper.reconcile_virtual_orders(symbols=[symbol])
            _print_json("reconcile_virtual_orders (market)", reconcile_market)
            _print_json("market_virtual_order", wrapper.get_virtual_order(market_virtual_id))

        # 5) 平仓（若无持仓会返回 no position）
        if run_close_positions:
            _section("平仓")
            close_res = await wrapper.close_all_positions_for_symbol(symbol)
            _print_json("close_all_positions_for_symbol", close_res)
    finally:
        if run_reconcile_loop:
            await wrapper.stop_reconcile_loop()
        await wrapper._close()


if __name__ == "__main__":
    asyncio.run(main())

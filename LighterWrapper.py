# 自定义实现的 lighter DEX API 包装
# 因为官方 API SDK 写的真是太 TM 狗屎了
import lighter
import datetime
import asyncio
import math
import warnings
import time
import uuid
import json
import sqlite3
import aiohttp

from lighter.signer_client import CreateOrderTxReq

from dataclasses import dataclass
from typing import Dict, Optional

@dataclass
class APIConfig:
    base_url: str
    api_secret_key: Dict[int, str]  # {api_secret_key_index: api_secret_key}
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
        self._virtual_orders: Dict[str, dict] = {}
        self._db_queue: asyncio.Queue = asyncio.Queue()
        self._db_writer_task: Optional[asyncio.Task] = None
        self._db_flush_batch_size = 50
        self._db_flush_interval_sec = 0.2
        self._db_writer_stop_timeout = 5.0
        self._db_path = f"{self.config.account_index}.db"
        self._init_virtual_orders_db()
        self._load_virtual_orders()
        self._reconcile_task: Optional[asyncio.Task] = None
        self._market_cache_task: Optional[asyncio.Task] = None
        self._price_cache: Dict[str, dict] = {}
        self._price_cache_max_age_sec = 10
        self._price_cache_fresh_slippage_ticks = 8
        self._price_cache_stale_slippage_ticks = 20
        self._warn_on_precision = False

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
        if symbol in self.books_metadatas_cache:
            size_decimals = self.books_metadatas_cache[symbol]["supported_size_decimals"]
        else:
            books_metadata = await self.get_order_books_metadata(symbol)
            size_decimals = books_metadata["order_books"][0]["supported_size_decimals"]
            self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存
            
        scaled = amount * (10 ** size_decimals)
        rounded = round(scaled)
        # 最小精度检测：如果不是可用精度的整数倍，给出 Warning 并抛弃小数点
        if not math.isclose(scaled, rounded, rel_tol=0.0, abs_tol=1e-6):
            if self._warn_on_precision:
                min_step = 10 ** (-size_decimals)
                warnings.warn(
                    f"amount 精度超限，最小步进 {min_step}，传入 {amount}；已截断至最小精度",
                    RuntimeWarning,
                )
            return int(scaled)
        return int(rounded) # 根据精度缩放数量

    async def _resize_price(self, symbol: str, price: float) -> int:
        if symbol in self.books_metadatas_cache:
            price_decimals = self.books_metadatas_cache[symbol]["supported_price_decimals"]
        else:
            books_metadata = await self.get_order_books_metadata(symbol)
            price_decimals = books_metadata["order_books"][0]["supported_price_decimals"]
            self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存

        scaled = price * (10 ** price_decimals)
        rounded = round(scaled)
        # 最小精度检测：如果不是可用精度的整数倍，给出 Warning 并抛弃小数点
        if not math.isclose(scaled, rounded, rel_tol=0.0, abs_tol=1e-6):
            if self._warn_on_precision:
                min_step = 10 ** (-price_decimals)
                warnings.warn(
                    f"price 精度超限，最小步进 {min_step}，传入 {price}；已截断至最小精度",
                    RuntimeWarning,
                )
            return int(scaled)
        return int(rounded) # 根据精度缩放价格

    async def _close(self):
        await self._cancel_task(self._reconcile_task, "reconcile_loop")
        await self._cancel_task(self._market_cache_task, "market_cache_loop")
        await self._stop_db_writer(fast_exit=True)
        await self._await_with_timeout(self.api_client.close(), "api_client.close")
        await self._await_with_timeout(self.signer_instance.close(), "signer_instance.close")
        if self._http_session and not self._http_session.closed:
            await self._await_with_timeout(self._http_session.close(), "http_session.close")

    async def _cancel_task(self, task: Optional[asyncio.Task], name: str, timeout_sec: float = 2.0) -> None:
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=timeout_sec)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                warnings.warn(f"{name} cancel timeout", RuntimeWarning)
            except Exception as e:
                warnings.warn(f"{name} cancel error: {e}", RuntimeWarning)

    async def _await_with_timeout(self, awaitable, name: str, timeout_sec: float = 2.0) -> None:
        try:
            await asyncio.wait_for(awaitable, timeout=timeout_sec)
        except asyncio.TimeoutError:
            warnings.warn(f"{name} timeout", RuntimeWarning)
        except Exception as e:
            warnings.warn(f"{name} error: {e}", RuntimeWarning)

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

    def _init_virtual_orders_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS virtual_orders (
                    virtual_order_id TEXT PRIMARY KEY,
                    account_index INTEGER NOT NULL,
                    status TEXT,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER,
                    data_json TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_virtual_orders_status ON virtual_orders(status)"
            )
        finally:
            conn.close()

    def _load_virtual_orders(self) -> None:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            cursor = conn.execute(
                "SELECT virtual_order_id, data_json FROM virtual_orders"
            )
            for virtual_order_id, data_json in cursor.fetchall():
                try:
                    self._virtual_orders[virtual_order_id] = json.loads(data_json)
                except Exception:
                    continue
        finally:
            conn.close()

    @staticmethod
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

    def _flush_virtual_orders(self, conn: sqlite3.Connection, items: list) -> None:
        rows = []
        for item in items:
            virtual_order_id = item["virtual_order_id"]
            data = item["data"]
            status = data.get("status")
            created_at_ms = data.get("created_at_ms")
            updated_at_ms = data.get("updated_at_ms")
            data_json = json.dumps(data, ensure_ascii=False, default=self._json_default)
            rows.append(
                (
                    virtual_order_id,
                    self.config.account_index,
                    status,
                    created_at_ms,
                    updated_at_ms,
                    data_json,
                )
            )
        conn.executemany(
            """
            INSERT INTO virtual_orders (
                virtual_order_id,
                account_index,
                status,
                created_at_ms,
                updated_at_ms,
                data_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(virtual_order_id) DO UPDATE SET
                status=excluded.status,
                updated_at_ms=excluded.updated_at_ms,
                data_json=excluded.data_json
            """,
            rows,
        )
        conn.commit()

    async def _db_writer_loop(self) -> None:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        pending = []
        last_flush = time.monotonic()
        drop_all = False
        try:
            while True:
                timeout = max(
                    0.0, self._db_flush_interval_sec - (time.monotonic() - last_flush)
                )
                try:
                    item = await asyncio.wait_for(self._db_queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    item = None

                if item:
                    if item.get("__stop__"):
                        drop_all = bool(item.get("drop_pending"))
                        while True:
                            try:
                                extra = self._db_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            if extra.get("__stop__"):
                                if extra.get("drop_pending"):
                                    drop_all = True
                                continue
                            if not drop_all:
                                pending.append(extra)
                        if pending and not drop_all:
                            await asyncio.to_thread(self._flush_virtual_orders, conn, pending)
                            pending.clear()
                        break
                    pending.append(item)

                if pending and (len(pending) >= self._db_flush_batch_size or item is None) and not drop_all:
                    await asyncio.to_thread(self._flush_virtual_orders, conn, pending)
                    pending.clear()
                    last_flush = time.monotonic()
        finally:
            if pending and not drop_all:
                await asyncio.to_thread(self._flush_virtual_orders, conn, pending)
            conn.close()

    def _ensure_db_writer(self) -> bool:
        if self._db_writer_task is None or self._db_writer_task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
            self._db_writer_task = loop.create_task(self._db_writer_loop())
        return True

    async def _stop_db_writer(
        self,
        timeout_sec: Optional[float] = None,
        warn_on_timeout: bool = False,
        fast_exit: bool = False,
    ) -> None:
        if timeout_sec is None:
            timeout_sec = self._db_writer_stop_timeout
        if self._db_writer_task and not self._db_writer_task.done():
            await self._db_queue.put({"__stop__": True, "drop_pending": fast_exit})
            try:
                wait_timeout = 0.2 if fast_exit else timeout_sec
                await asyncio.wait_for(self._db_writer_task, timeout=wait_timeout)
            except asyncio.TimeoutError:
                self._db_writer_task.cancel()
                if warn_on_timeout and not fast_exit:
                    warnings.warn("db_writer stop timeout", RuntimeWarning)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                warnings.warn(f"db_writer stop error: {e}", RuntimeWarning)

    def _persist_virtual_order_sync(self, virtual_order_id: str) -> None:
        data = self._virtual_orders.get(virtual_order_id)
        if data is None:
            return
        now_ms = int(time.time() * 1000)
        if data.get("created_at_ms") is None:
            data["created_at_ms"] = now_ms
        data["updated_at_ms"] = now_ms
        status = data.get("status")
        data_json = json.dumps(data, ensure_ascii=False, default=self._json_default)

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                INSERT INTO virtual_orders (
                    virtual_order_id,
                    account_index,
                    status,
                    created_at_ms,
                    updated_at_ms,
                    data_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(virtual_order_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at_ms=excluded.updated_at_ms,
                    data_json=excluded.data_json
                """,
                (
                    virtual_order_id,
                    self.config.account_index,
                    status,
                    data.get("created_at_ms"),
                    data.get("updated_at_ms"),
                    data_json,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _persist_virtual_order(self, virtual_order_id: str) -> None:
        data = self._virtual_orders.get(virtual_order_id)
        if data is None:
            return
        now_ms = int(time.time() * 1000)
        if data.get("created_at_ms") is None:
            data["created_at_ms"] = now_ms
        data["updated_at_ms"] = now_ms

        if not self._ensure_db_writer():
            self._persist_virtual_order_sync(virtual_order_id)
            return

        try:
            self._db_queue.put_nowait({"virtual_order_id": virtual_order_id, "data": dict(data)})
        except asyncio.QueueFull:
            self._persist_virtual_order_sync(virtual_order_id)

    def _generate_virtual_order_id(self) -> str:
        return f"v-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def get_virtual_order(self, virtual_order_id: str) -> Optional[dict]:
        return self._virtual_orders.get(virtual_order_id)

    def list_virtual_orders(self) -> dict:
        return dict(self._virtual_orders)

    @staticmethod
    def _get_order_field(order: dict, *keys):
        for key in keys:
            if key in order and order[key] is not None:
                return order[key]
        return None

    def _normalize_side(self, order: dict) -> Optional[str]:
        side = self._get_order_field(order, "side")
        if isinstance(side, str):
            s = side.lower()
            if s in ("buy", "sell"):
                return s
        if side is not None:
            try:
                return "sell" if int(side) == 1 else "buy"
            except Exception:
                pass
        is_ask = self._get_order_field(order, "is_ask", "IsAsk")
        if is_ask is not None:
            try:
                return "sell" if int(is_ask) == 1 else "buy"
            except Exception:
                pass
        return None

    def _normalize_reduce_only(self, order: dict) -> Optional[bool]:
        reduce_only = self._get_order_field(order, "reduce_only", "reduceOnly", "ReduceOnly")
        if reduce_only is None:
            return None
        try:
            return bool(int(reduce_only))
        except Exception:
            return bool(reduce_only)

    @staticmethod
    def _safe_float(value, default: Optional[float] = None) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return default

    def _update_price_cache(self, symbol: str, **kwargs) -> None:
        entry = self._price_cache.get(symbol, {})
        entry.update(kwargs)
        entry["ts"] = int(time.time() * 1000)
        self._price_cache[symbol] = entry

    def get_price_cache(self, symbol: str) -> Optional[dict]:
        entry = self._price_cache.get(symbol)
        if not entry:
            return None
        now_ms = int(time.time() * 1000)
        age_sec = (now_ms - entry.get("ts", now_ms)) / 1000.0
        return {**entry, "age_sec": age_sec, "stale": age_sec > self._price_cache_max_age_sec}

    async def refresh_ticker_cache(self, symbol: str) -> Optional[dict]:
        data = await self.fetch_ticker(symbol)
        ticker = data.get("ticker")
        if ticker:
            last_trade = self._safe_float(ticker.get("last_trade_price"))
            if last_trade is not None:
                self._update_price_cache(symbol, last_trade=last_trade, source="ticker")
        return self.get_price_cache(symbol)

    async def refresh_order_book_cache(self, symbol: str, limit: int = 1) -> Optional[dict]:
        data = await self.fetch_order_book_depth(symbol, limit=limit)
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = self._safe_float(bids[0].get("price")) if bids else None
        best_ask = self._safe_float(asks[0].get("price")) if asks else None
        if best_bid is not None or best_ask is not None:
            self._update_price_cache(
                symbol, best_bid=best_bid, best_ask=best_ask, source="order_book"
            )
        return self.get_price_cache(symbol)

    def _derive_fill_status(self, order: dict) -> dict:
        initial = self._safe_float(
            self._get_order_field(order, "initial_base_amount", "initialBaseAmount", "base_amount", "baseAmount")
        )
        remaining = self._safe_float(
            self._get_order_field(order, "remaining_base_amount", "remainingBaseAmount")
        )
        filled = self._safe_float(
            self._get_order_field(order, "filled_base_amount", "filledBaseAmount")
        )

        if filled is None and initial is not None and remaining is not None:
            filled = max(initial - remaining, 0.0)
        if remaining is None and initial is not None and filled is not None:
            remaining = max(initial - filled, 0.0)

        status = order.get("status")
        status_norm = status.lower() if isinstance(status, str) else None

        fill_status = "unknown"
        if filled is not None or remaining is not None or initial is not None:
            if remaining is not None and filled is not None:
                if remaining <= 0 and filled > 0:
                    fill_status = "filled"
                elif filled > 0 and remaining > 0:
                    fill_status = "partial"
                elif filled == 0:
                    fill_status = "unfilled"
            elif initial is not None and filled is not None:
                if filled >= initial and initial > 0:
                    fill_status = "filled"
                elif filled > 0:
                    fill_status = "partial"
                else:
                    fill_status = "unfilled"

        if status_norm in ("canceled", "cancelled", "canceled-reduce-only", "expired", "rejected"):
            if fill_status == "unfilled":
                fill_status = "canceled"

        filled_ratio = None
        if initial and initial > 0 and filled is not None:
            filled_ratio = min(max(filled / initial, 0.0), 1.0)

        return {
            "order_status": status,
            "fill_status": fill_status,
            "initial_base_amount": initial,
            "filled_base_amount": filled,
            "remaining_base_amount": remaining,
            "filled_ratio": filled_ratio,
        }

    @staticmethod
    def _order_matches_identifier(
        order: dict,
        order_index: Optional[int],
        order_id: Optional[int],
        client_order_index: Optional[int],
    ) -> bool:
        def _matches(keys, target):
            if target is None:
                return False
            for key in keys:
                if key in order and order[key] is not None:
                    if str(order[key]) == str(target):
                        return True
            return False

        if _matches(("order_index", "orderIndex", "order_id", "orderId", "id"), order_index):
            return True
        if _matches(("order_id", "orderId", "id", "order_index", "orderIndex"), order_id):
            return True
        if _matches(
            ("client_order_index", "clientOrderIndex", "client_order_id", "clientOrderId"),
            client_order_index,
        ):
            return True
        return False

    def _normalize_order_type(self, order: dict):
        order_type = self._get_order_field(order, "order_type", "orderType", "type", "Type")
        if isinstance(order_type, str):
            return order_type.lower()
        return order_type

    @staticmethod
    def _extract_timestamp_ms(order: dict) -> Optional[int]:
        ts = None
        for key in (
            "created_at",
            "createdAt",
            "created_time",
            "createdTime",
            "timestamp",
            "ts",
        ):
            if key in order and order[key] is not None:
                ts = order[key]
                break
        if ts is None:
            return None
        try:
            ts_int = int(ts)
        except Exception:
            return None
        if ts_int < 10**12:
            ts_int *= 1000
        return ts_int

    @staticmethod
    def _order_unique_key(order: dict) -> str:
        for key in ("order_index", "orderIndex", "order_id", "orderId", "id"):
            if key in order and order[key] is not None:
                return str(order[key])
        return f"noid-{hash(json.dumps(order, sort_keys=True, ensure_ascii=False))}"

    @staticmethod
    def _order_min_view(order: dict) -> dict:
        view = {}
        for key in (
            "order_index",
            "orderIndex",
            "order_id",
            "orderId",
            "id",
            "symbol",
            "price",
            "Price",
            "trigger_price",
            "TriggerPrice",
            "side",
            "is_ask",
            "reduce_only",
            "order_type",
            "type",
            "status",
        ):
            if key in order:
                view[key] = order[key]
        if "_source" in order:
            view["_source"] = order["_source"]
        return view

    @staticmethod
    def _order_type_matches(actual, expected_types) -> bool:
        if actual is None:
            return False
        if isinstance(actual, str):
            actual_str = actual.lower()
            for expected in expected_types:
                if isinstance(expected, str) and expected in actual_str:
                    return True
            return False
        try:
            actual_int = int(actual)
        except Exception:
            return False
        for expected in expected_types:
            try:
                if int(expected) == actual_int:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _price_matches(actual, expected_int: Optional[int], expected_float: Optional[float], step: float) -> bool:
        if actual is None:
            return True
        try:
            value = float(actual)
        except Exception:
            return True
        if expected_int is not None and abs(value - expected_int) <= 1:
            return True
        if expected_float is not None and abs(value - expected_float) <= step * 2:
            return True
        return False

    def _build_expected_suborders(self, virtual_order: dict, include_entry: bool) -> list:
        side = virtual_order.get("side")
        if side == "buy":
            opposite = "sell"
        else:
            opposite = "buy"

        expected = []

        if virtual_order.get("order_type") in ("market_with_tp_sl", "limit_with_tp_sl"):
            tp_sl_market = bool(virtual_order.get("tp_sl_market"))
            expected.append(
                {
                    "kind": "tp",
                    "side": opposite,
                    "reduce_only": True,
                    "order_types": {
                        self.signer_instance.ORDER_TYPE_TAKE_PROFIT
                        if tp_sl_market
                        else self.signer_instance.ORDER_TYPE_TAKE_PROFIT_LIMIT,
                        "take_profit",
                        "take_profit_limit" if not tp_sl_market else "take_profit_market",
                        "tp_limit" if not tp_sl_market else "tp_market",
                    },
                    "price_int": virtual_order.get("tp_price_int"),
                    "price": virtual_order.get("worst_tp_price"),
                    "trigger_int": virtual_order.get("tp_trigger_price_int"),
                    "trigger": virtual_order.get("take_profit_price"),
                }
            )
            expected.append(
                {
                    "kind": "sl",
                    "side": opposite,
                    "reduce_only": True,
                    "order_types": {
                        self.signer_instance.ORDER_TYPE_STOP_LOSS
                        if tp_sl_market
                        else self.signer_instance.ORDER_TYPE_STOP_LOSS_LIMIT,
                        "stop_loss",
                        "stop_loss_limit" if not tp_sl_market else "stop_loss_market",
                        "sl_limit" if not tp_sl_market else "sl_market",
                    },
                    "price_int": virtual_order.get("sl_price_int"),
                    "price": virtual_order.get("worst_sl_price"),
                    "trigger_int": virtual_order.get("sl_trigger_price_int"),
                    "trigger": virtual_order.get("stop_loss_price"),
                }
            )

            if include_entry:
                entry_types = {
                    self.signer_instance.ORDER_TYPE_MARKET,
                    "market",
                }
                if virtual_order.get("order_type") == "limit_with_tp_sl":
                    entry_types = {
                        self.signer_instance.ORDER_TYPE_LIMIT,
                        "limit",
                    }
                expected.append(
                    {
                        "kind": "entry",
                        "side": side,
                        "reduce_only": False,
                        "order_types": entry_types,
                        "price_int": virtual_order.get("entry_price_int"),
                        "price": virtual_order.get("price"),
                    }
                )

        return expected

    def _find_best_match(
        self,
        orders: list,
        used_keys: set,
        expected: dict,
        price_step: float,
        match_window_ms: int,
        created_at_ms: Optional[int],
    ) -> Optional[dict]:
        best = None
        best_score = None

        for order in orders:
            key = self._order_unique_key(order)
            if key in used_keys:
                continue

            actual_side = self._normalize_side(order)
            expected_side = expected.get("side")
            if actual_side is not None and expected_side is not None and actual_side != expected_side:
                continue

            actual_reduce_only = self._normalize_reduce_only(order)
            expected_reduce_only = expected.get("reduce_only")
            if actual_reduce_only is not None and expected_reduce_only is not None:
                if actual_reduce_only != expected_reduce_only:
                    continue

            actual_order_type = self._normalize_order_type(order)
            expected_types = expected.get("order_types")
            if actual_order_type is not None and expected_types is not None:
                if not self._order_type_matches(actual_order_type, expected_types):
                    continue

            if expected.get("price_int") is not None or expected.get("price") is not None:
                actual_price = self._get_order_field(order, "price", "Price", "limit_price", "limitPrice")
                if not self._price_matches(
                    actual_price,
                    expected.get("price_int"),
                    expected.get("price"),
                    price_step,
                ):
                    continue

            if expected.get("trigger_int") is not None or expected.get("trigger") is not None:
                actual_trigger = self._get_order_field(order, "trigger_price", "TriggerPrice", "triggerPrice")
                if not self._price_matches(
                    actual_trigger,
                    expected.get("trigger_int"),
                    expected.get("trigger"),
                    price_step,
                ):
                    continue

            if created_at_ms is not None:
                actual_ts = self._extract_timestamp_ms(order)
                if actual_ts is not None and abs(actual_ts - created_at_ms) > match_window_ms:
                    continue

            # Match score: smaller is better
            score = 0
            if actual_side is None:
                score += 1
            if actual_reduce_only is None:
                score += 1
            if actual_order_type is None:
                score += 1

            actual_price = self._get_order_field(order, "price", "Price", "limit_price", "limitPrice")
            if actual_price is not None and expected.get("price") is not None:
                try:
                    score += abs(float(actual_price) - float(expected.get("price"))) / max(price_step, 1e-9)
                except Exception:
                    score += 1

            actual_trigger = self._get_order_field(order, "trigger_price", "TriggerPrice", "triggerPrice")
            if actual_trigger is not None and expected.get("trigger") is not None:
                try:
                    score += abs(float(actual_trigger) - float(expected.get("trigger"))) / max(price_step, 1e-9)
                except Exception:
                    score += 1

            if best is None or score < best_score:
                best = order
                best_score = score

        if best is None:
            return None

        best_view = self._order_min_view(best)
        best_view["_match_key"] = self._order_unique_key(best)
        return best_view

    async def reconcile_virtual_orders(
        self,
        symbols: Optional[list] = None,
        include_closed: bool = True,
        closed_limit: int = 100,
        include_entry: bool = False,
        match_window_sec: int = 300,
    ) -> dict:
        """
        reconcile_virtual_orders: 虚拟单与实际订单自动匹配

        参数:
            symbols: 仅匹配指定交易对列表
            include_closed: 是否包含已完成/已取消订单
            closed_limit: 已完成订单拉取数量
            include_entry: 是否尝试匹配入场单
            match_window_sec: 订单创建时间匹配窗口（秒）
        """
        active_orders = {
            vid: vo
            for vid, vo in self._virtual_orders.items()
            if vo.get("status") in ("created", "submitted", "matched_partial")
        }
        if not active_orders:
            return {"total": 0, "updated": 0, "matched": 0}

        if symbols is None:
            symbols = sorted(
                {vo.get("symbol") for vo in active_orders.values() if vo.get("symbol")}
            )

        actual_orders_by_symbol = {}
        for symbol in symbols:
            open_res = await self.fetch_open_orders(symbol)
            orders = open_res.get("orders") or []
            for order in orders:
                order["_source"] = "open"

            if include_closed:
                closed_res = await self.fetch_closed_orders(symbol=symbol, limit=closed_limit)
                closed_orders = closed_res.get("orders") or []
                for order in closed_orders:
                    order["_source"] = "closed"
                orders.extend(closed_orders)

            actual_orders_by_symbol[symbol] = orders

        price_steps = {}
        for symbol in symbols:
            decimals = await self.get_symbol_price_decimals(symbol)
            price_steps[symbol] = 10 ** (-decimals)

        used_keys = {symbol: set() for symbol in symbols}

        updated = 0
        matched_total = 0
        match_window_ms = match_window_sec * 1000

        for virtual_order_id, virtual_order in active_orders.items():
            symbol = virtual_order.get("symbol")
            if symbol not in actual_orders_by_symbol:
                continue

            expected_orders = self._build_expected_suborders(
                virtual_order, include_entry=include_entry
            )
            if not expected_orders:
                continue

            matched = []
            for expected in expected_orders:
                match = self._find_best_match(
                    actual_orders_by_symbol[symbol],
                    used_keys[symbol],
                    expected,
                    price_steps[symbol],
                    match_window_ms,
                    virtual_order.get("created_at_ms"),
                )
                if match:
                    used_keys[symbol].add(match["_match_key"])
                    matched.append(match)

            if matched:
                virtual_order["actual_orders"] = matched
                expected_count = virtual_order.get("expected_order_count", len(expected_orders))
                if len(matched) >= expected_count:
                    virtual_order["status"] = "matched"
                else:
                    virtual_order["status"] = "matched_partial"
                self._persist_virtual_order(virtual_order_id)
                updated += 1
                matched_total += len(matched)

        return {"total": len(active_orders), "updated": updated, "matched": matched_total}

    async def _reconcile_loop(
        self,
        interval_sec: float,
        symbols: Optional[list],
        include_closed: bool,
        closed_limit: int,
        include_entry: bool,
        match_window_sec: int,
    ) -> None:
        while True:
            try:
                await self.reconcile_virtual_orders(
                    symbols=symbols,
                    include_closed=include_closed,
                    closed_limit=closed_limit,
                    include_entry=include_entry,
                    match_window_sec=match_window_sec,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                warnings.warn(f"reconcile_virtual_orders loop error: {e}", RuntimeWarning)
            await asyncio.sleep(interval_sec)

    def start_reconcile_loop(
        self,
        interval_sec: float = 5.0,
        symbols: Optional[list] = None,
        include_closed: bool = True,
        closed_limit: int = 100,
        include_entry: bool = False,
        match_window_sec: int = 300,
    ) -> None:
        """
        start_reconcile_loop: 启动轻量定时协程自动匹配虚拟单
        """
        if self._reconcile_task and not self._reconcile_task.done():
            return
        self._reconcile_task = asyncio.create_task(
            self._reconcile_loop(
                interval_sec=interval_sec,
                symbols=symbols,
                include_closed=include_closed,
                closed_limit=closed_limit,
                include_entry=include_entry,
                match_window_sec=match_window_sec,
            )
        )

    async def stop_reconcile_loop(self) -> None:
        """
        stop_reconcile_loop: 停止自动匹配协程
        """
        if self._reconcile_task and not self._reconcile_task.done():
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass

    async def _market_cache_loop(
        self,
        symbols: list,
        interval_sec: float,
        use_ticker: bool,
        use_order_book: bool,
        depth_limit: int,
    ) -> None:
        while True:
            try:
                for symbol in symbols:
                    if use_ticker:
                        await self.refresh_ticker_cache(symbol)
                    if use_order_book:
                        await self.refresh_order_book_cache(symbol, limit=depth_limit)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                warnings.warn(f"market cache loop error: {e}", RuntimeWarning)
            await asyncio.sleep(interval_sec)

    def start_market_cache_loop(
        self,
        symbols: list,
        interval_sec: float = 2.0,
        use_ticker: bool = True,
        use_order_book: bool = True,
        depth_limit: int = 1,
    ) -> None:
        """
        start_market_cache_loop: 启动行情缓存轮询
        """
        if self._market_cache_task and not self._market_cache_task.done():
            return
        self._market_cache_task = asyncio.create_task(
            self._market_cache_loop(
                symbols=symbols,
                interval_sec=interval_sec,
                use_ticker=use_ticker,
                use_order_book=use_order_book,
                depth_limit=depth_limit,
            )
        )

    async def stop_market_cache_loop(self) -> None:
        """
        stop_market_cache_loop: 停止行情缓存轮询
        """
        if self._market_cache_task and not self._market_cache_task.done():
            self._market_cache_task.cancel()
            try:
                await self._market_cache_task
            except asyncio.CancelledError:
                pass

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
        if symbol in self.books_metadatas_cache:
            return int(self.books_metadatas_cache[symbol]["market_id"])
        
        # 缓存没有就请求接口
        books = await self._order_api.order_books()
        for b in books.order_books:
            if getattr(b, "symbol", None) == symbol:
                # 更新缓存
                self.books_metadatas_cache[symbol] = b.to_dict()
                return int(b.market_id)
        raise ValueError(f"找不到 {symbol} 对应的 market_id")
    
    async def build_books_metadata_cache(self) -> None:
        """
        build_books_metadata_cache: 构建 symbol -> market_id 的映射字典
        """
        books = await self._order_api.order_books()
        self.books_metadatas_cache = {
            getattr(b, "symbol", None): b.to_dict()
            for b in books.order_books
            if getattr(b, "symbol", None) is not None
        }
    
    async def get_symbol_price_decimals(self, symbol: str) -> int:
        """
        get_symbol_price_decimals: 获取指定交易对的价格精度
        """
        if symbol in self.books_metadatas_cache:
            return int(self.books_metadatas_cache[symbol]["supported_price_decimals"])
        books_metadata = await self.get_order_books_metadata(symbol)
        price_decimals = books_metadata["order_books"][0]["supported_price_decimals"]
        self.books_metadatas_cache[symbol] = books_metadata["order_books"][0] # 添加缓存
        return price_decimals

    async def get_symbol_size_decimals(self, symbol: str) -> int:
        """
        get_symbol_size_decimals: 获取指定交易对的数量精度
        """
        if symbol in self.books_metadatas_cache:
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

    async def fetch_ticker(self, symbol: str, include_raw: bool = False) -> dict:
        """
        fetch_ticker: 获取 ticker（含最新价与 24h 统计）

        参数:
            symbol: 交易对符号, 如 "BTC"
            include_raw: 是否返回完整原始结构
        返回格式示例:
            {"code": 200, "ticker": {...}} 或包含 raw
        """
        market_id = await self.get_market_id(symbol)
        res = await self._order_api.order_book_details(market_id=market_id)
        data = res.to_dict()

        details = data.get("order_book_details") or data.get("spot_order_book_details") or []
        ticker = None
        for item in details:
            if item.get("market_id") == market_id or item.get("symbol") == symbol:
                ticker = item
                break
        if ticker is None and details:
            ticker = details[0]

        out = {"code": data.get("code", 200), "ticker": ticker}
        if ticker:
            last_trade = self._safe_float(ticker.get("last_trade_price"))
            if last_trade is not None:
                self._update_price_cache(symbol, last_trade=last_trade, source="ticker")
        if include_raw:
            out["raw"] = data
        return out

    async def get_latest_price(self, symbol: str) -> Optional[float]:
        """
        get_latest_price: 获取最新成交价（优先 recent_trades, 无数据则用 ticker 的 last_trade_price）
        """
        market_id = await self.get_market_id(symbol)
        res = await self._order_api.recent_trades(market_id=market_id, limit=1)
        data = res.to_dict()
        trades = data.get("trades") or []
        if trades:
            price = trades[0].get("price")
            price_val = self._safe_float(price)
            if price_val is not None:
                self._update_price_cache(symbol, last_trade=price_val, source="recent_trades")
            return price_val

        ticker = await self.fetch_ticker(symbol)
        if ticker.get("ticker"):
            return self._safe_float(ticker["ticker"].get("last_trade_price"))
        return None

    async def fetch_order_book_depth(self, symbol: str, limit: int = 50) -> dict:
        """
        fetch_order_book_depth: 获取盘口深度（bids/asks）

        参数:
            symbol: 交易对符号, 如 "BTC"
            limit: 深度档数（1-250）
        返回格式示例:
            {"code": 200, "asks": [...], "bids": [...], "total_asks": 10, "total_bids": 10}
        """
        market_id = await self.get_market_id(symbol)
        res = await self._order_api.order_book_orders(market_id=market_id, limit=limit)
        data = res.to_dict()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        best_bid = self._safe_float(bids[0].get("price")) if bids else None
        best_ask = self._safe_float(asks[0].get("price")) if asks else None
        if best_bid is not None or best_ask is not None:
            self._update_price_cache(symbol, best_bid=best_bid, best_ask=best_ask, source="order_book")
        return data

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

    async def fetch_fills(
        self,
        symbol: Optional[str] = None,
        market_id: Optional[int] = None,
        order_index: Optional[int] = None,
        limit: int = 100,
        sort_by: str = "timestamp",
        cursor: Optional[str] = None,
        ask_filter: Optional[int] = None,
        role: str = "all",
        trade_type: str = "all",
        aggregate: bool = False,
        auth: Optional[str] = None,
    ) -> dict:
        """
        fetch_fills: 获取成交记录（fills）

        参数:
            symbol: 交易对符号, 如 "BTC"（可选）
            market_id: 交易对 ID（可选，symbol 存在时自动换算）
            order_index: 订单索引（可选）
            limit: 返回数量（1-100）
            sort_by: block_height / timestamp / trade_id
            cursor: 翻页游标（可选）
            ask_filter: 0 买单 / 1 卖单（可选）
            role: all / maker / taker
            trade_type: all / trade / liquidation / deleverage / market-settlement
            aggregate: 是否聚合
            auth: 可选认证 token，不传则自动生成
        """
        if market_id is None and symbol:
            market_id = await self.get_market_id(symbol)
        if auth is None:
            auth = self._get_auth_token()

        res = await self._order_api.trades(
            sort_by=sort_by,
            limit=limit,
            auth=auth,
            market_id=market_id,
            account_index=self.config.account_index,
            order_index=order_index,
            sort_dir="desc",
            cursor=cursor,
            ask_filter=ask_filter,
            role=role,
            type=trade_type,
            aggregate=aggregate,
        )
        return res.to_dict()

    async def get_order_fills(
        self,
        symbol: Optional[str] = None,
        order_index: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        auth: Optional[str] = None,
    ) -> dict:
        """
        get_order_fills: 获取指定订单的成交记录
        """
        if order_index is None:
            raise ValueError("order_index is required for order fills lookup")
        return await self.fetch_fills(
            symbol=symbol,
            order_index=order_index,
            limit=limit,
            cursor=cursor,
            auth=auth,
        )

    async def get_order_status(
        self,
        symbol: Optional[str] = None,
        order_index: Optional[int] = None,
        order_id: Optional[int] = None,
        client_order_index: Optional[int] = None,
        virtual_order_id: Optional[str] = None,
        include_closed: bool = True,
        closed_limit: int = 100,
        include_fills: bool = False,
        fills_limit: int = 100,
    ) -> dict:
        """
        get_order_status: 查询订单状态（支持 virtual_order_id / order_id / order_index / client_order_index）
        """
        if virtual_order_id:
            vo = self._virtual_orders.get(virtual_order_id)
            if vo is None:
                return {"status": "not_found", "virtual_order_id": virtual_order_id}
            return {
                "status": vo.get("status"),
                "virtual_order_id": virtual_order_id,
                "virtual_order": vo,
            }

        if symbol is None:
            raise ValueError("symbol is required for order status lookup")
        if order_index is None and order_id is None and client_order_index is None:
            raise ValueError("order_index/order_id/client_order_index at least one is required")

        open_res = await self.fetch_open_orders(symbol)
        open_orders = open_res.get("orders") or []
        for order in open_orders:
            if self._order_matches_identifier(order, order_index, order_id, client_order_index):
                status_info = self._derive_fill_status(order)
                result = {"status": "open", "source": "open", "order": order, **status_info}
                if include_fills:
                    oi = order_index or self._get_order_field(order, "order_index", "orderIndex")
                    if oi is not None:
                        fills = await self.get_order_fills(
                            symbol=symbol,
                            order_index=int(oi),
                            limit=fills_limit,
                        )
                        result["fills"] = fills
                return result

        if include_closed:
            closed_res = await self.fetch_closed_orders(symbol=symbol, limit=closed_limit)
            closed_orders = closed_res.get("orders") or []
            for order in closed_orders:
                if self._order_matches_identifier(order, order_index, order_id, client_order_index):
                    status = order.get("status") or "closed"
                    status_info = self._derive_fill_status(order)
                    result = {"status": status, "source": "closed", "order": order, **status_info}
                    if include_fills:
                        oi = order_index or self._get_order_field(order, "order_index", "orderIndex")
                        if oi is not None:
                            fills = await self.get_order_fills(
                                symbol=symbol,
                                order_index=int(oi),
                                limit=fills_limit,
                            )
                            result["fills"] = fills
                    return result

        return {"status": "not_found"}

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

    async def update_symbol_leverage(self, symbol: str, leverage: int, margin_mode: str) -> tuple:
        """
        update_symbol_leverage: 更新指定交易对的杠杆倍数

        参数:
            symbol: 交易对符号, 如 "BTC"
            leverage: 杠杆倍数
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
            margin_mode=mode,
        )

        return self._tuple_to_dict(res_tuple)

    async def calulate_worst_acceptable_price(self, symbol: str, side: str, max_slippage: float = 0.001) -> float:
        """
        calulate_worst_acceptable_price: 计算市价单的最差可接受价格
        
        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"
            max_slippage: 最大滑点比例 (默认 0.001 即 0.1%)

        返回格式示例: 2500.0
        """
        symbol_price_decimals = await self.get_symbol_price_decimals(symbol)

        cached = self.get_price_cache(symbol)
        if cached is None:
            try:
                await self.refresh_order_book_cache(symbol, limit=1)
            except Exception:
                pass
            try:
                await self.refresh_ticker_cache(symbol)
            except Exception:
                pass
            cached = self.get_price_cache(symbol)

        ref_price = None
        is_stale = False
        if cached:
            is_stale = bool(cached.get("stale"))
            best_bid = cached.get("best_bid")
            best_ask = cached.get("best_ask")
            last_trade = cached.get("last_trade")
            if side.lower() == "buy":
                ref_price = best_ask or last_trade or best_bid
            else:
                ref_price = best_bid or last_trade or best_ask

        if ref_price is None:
            warnings.warn(
                "price cache unavailable, fallback to last close (slow path)",
                RuntimeWarning,
            )
            last_price = await self.fetch_ohlcv(symbol=symbol, resolution="1m", limit=1)
            ref_price = last_price["c"][-1]["c"]
            is_stale = True

        if side.lower() == "buy":
            worst_price = ref_price * (1 + max_slippage)
        else:
            worst_price = ref_price * (1 - max_slippage)

        # 价格四舍五入到指定精度
        worst_price = round(worst_price, symbol_price_decimals)

        return worst_price

    async def create_market_order_with_tp_sl(
            self,
            symbol: str,
            side: str,
            quantity: float,
            take_profit_price: float,
            stop_loss_price: float,
            tp_sl_market: bool = True,
            max_slippage: float = 0.001,
        ) -> tuple:
        """
        create_market_order_with_tp_sl: 创建市价单并设置止盈止损（带虚拟订单号）

        参数:
            symbol: 交易对符号, 如 "BTC"
            side: "buy" 或 "sell"
            quantity: 交易数量 (交易币种) 如 0.1 ETH
            take_profit_price: 止盈价格
            stop_loss_price: 止损价格
            tp_sl_market: 是否使用市价 TP/SL
            max_slippage: TP/SL 最差价格偏移比例 (默认 0.001 即 0.1%)

        返回格式示例: 
            (virtual_order_id, (CreateOrder, RespSendTx, None))     # 成功返回
            (virtual_order_id, (None, None, error_string))          # 失败返回
        """
        market_id = await self.get_market_id(symbol)

        if side.lower() == "buy": # 多单
            is_ask_ioc = 0
            is_ask_tp_ls = 1
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="buy", max_slippage=max_slippage)
            worst_tp_price = take_profit_price * (1 - max_slippage)  # 止盈价格略低于目标价
            worst_sl_price = stop_loss_price * (1 - max_slippage) 
        else:  # 空单
            is_ask_ioc = 1
            is_ask_tp_ls = 0
            worst_price = await self.calulate_worst_acceptable_price(symbol, side="sell", max_slippage=max_slippage)
            worst_tp_price = take_profit_price * (1 + max_slippage)  # 止盈价格略高于目标价
            worst_sl_price = stop_loss_price * (1 + max_slippage)

        # https://deepwiki.com/elliottech/lighter-python/6.3-grouped-and-conditional-orders
        # 设置 BaseAmount = 0 此订单会创建一个与持仓规模关联的订单
        # 创建 IOC 市价单
        resized_amount = await self._resize_amount(symbol, quantity)
        worst_price_int = await self._resize_price(symbol, worst_price)
        tp_price_int = await self._resize_price(symbol, worst_tp_price)
        sl_price_int = await self._resize_price(symbol, worst_sl_price)
        tp_trigger_int = await self._resize_price(symbol, take_profit_price)
        sl_trigger_int = await self._resize_price(symbol, stop_loss_price)

        ioc_order = CreateOrderTxReq(
            MarketIndex=market_id, # 交易对 ID
            # 服务器对 grouped orders 的限制：在 create_grouped_orders 里每个 CreateOrderTxReq.ClientOrderIndex 必须是 nil(0)，不能自定义
            ClientOrderIndex = 0, # 不允许自定义订单索引
            BaseAmount = resized_amount,  # 数量
            # 市价单这里的 Price 仍然要填，用作“最差可接受价格”
            Price = worst_price_int,     # 限价
            IsAsk = is_ask_ioc,  # 买卖方向，0 买 1 卖
            Type = self.signer_instance.ORDER_TYPE_MARKET, # 市价单
            TimeInForce = self.signer_instance.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            ReduceOnly = 0, # 非减仓单
            TriggerPrice = 0,
            OrderExpiry = 0,
        )

        # 创建止盈单和止损单
        tp_order_type = (
            self.signer_instance.ORDER_TYPE_TAKE_PROFIT
            if tp_sl_market
            else self.signer_instance.ORDER_TYPE_TAKE_PROFIT_LIMIT
        )
        tp_tif = (
            self.signer_instance.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            if tp_sl_market
            else self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        )
        sl_order_type = (
            self.signer_instance.ORDER_TYPE_STOP_LOSS
            if tp_sl_market
            else self.signer_instance.ORDER_TYPE_STOP_LOSS_LIMIT
        )
        sl_tif = (
            self.signer_instance.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            if tp_sl_market
            else self.signer_instance.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        )

        take_profit_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=0,
            Price = tp_price_int,
            IsAsk=is_ask_tp_ls, # 和入场单方向相反
            Type=tp_order_type,
            TimeInForce=tp_tif,
            ReduceOnly=1, # 仅减仓
            TriggerPrice = tp_trigger_int,
            OrderExpiry=-1,
        )

        stop_loss_order = CreateOrderTxReq(
            MarketIndex=market_id,
            ClientOrderIndex=0,
            BaseAmount=0,
            Price = sl_price_int,
            IsAsk=is_ask_tp_ls,
            Type=sl_order_type,
            TimeInForce=sl_tif,
            ReduceOnly=1,
            TriggerPrice = sl_trigger_int,
            OrderExpiry=-1,
        )

        transaction = await self.signer_instance.create_grouped_orders(
            grouping_type=lighter.SignerClient.GROUPING_TYPE_ONE_TRIGGERS_A_ONE_CANCELS_THE_OTHER,
            orders=[ioc_order, take_profit_order, stop_loss_order],
        )

        # 先下单 再记录虚拟订单
        virtual_order_id = self._generate_virtual_order_id()
        self._virtual_orders[virtual_order_id] = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "worst_tp_price": worst_tp_price,
            "worst_sl_price": worst_sl_price,
            "tp_price_int": tp_price_int,
            "sl_price_int": sl_price_int,
            "tp_trigger_price_int": tp_trigger_int,
            "sl_trigger_price_int": sl_trigger_int,
            "order_type": "market_with_tp_sl",
            "tp_sl_market": tp_sl_market,
            "market_id": market_id,
            "expected_order_count": 2,
            "status": "created",
        }
        self._persist_virtual_order(virtual_order_id)

        res = self._tuple_to_dict(transaction)
        err = None
        if isinstance(res, tuple) and len(res) >= 3:
            err = res[2]
        if err:
            self._virtual_orders[virtual_order_id]["status"] = "error"
            self._virtual_orders[virtual_order_id]["error"] = err
        else:
            self._virtual_orders[virtual_order_id]["status"] = "submitted"

        self._virtual_orders[virtual_order_id]["result"] = res
        self._persist_virtual_order(virtual_order_id)

        return virtual_order_id, res

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
            avg_execution_price=await self._resize_price(symbol, worst_price),
            reduce_only=reduce_only,
        )

        return self._tuple_to_dict(res_tuple)

    async def create_limit_order_with_tp_sl_virtual(
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
        create_limit_order_with_tp_sl_virtual: 创建限价单并设置止盈止损（带虚拟订单号）

        返回格式示例:
            (virtual_order_id, (CreateOrder, RespSendTx, None))
        """
        # 先下单 再记录虚拟订单
        res = await self.create_limit_order_with_tp_sl(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            order_expiry=order_expiry,
        )

        symbol_price_decimals = await self.get_symbol_price_decimals(symbol)

        if side.lower() == "buy": # 多单
            worst_tp_price = take_profit_price - (8 * (10 ** -symbol_price_decimals))
            worst_sl_price = stop_loss_price + (8 * (10 ** -symbol_price_decimals))
        else:  # 空单
            worst_tp_price = take_profit_price + (8 * (10 ** -symbol_price_decimals))
            worst_sl_price = stop_loss_price - (8 * (10 ** -symbol_price_decimals))

        entry_price_int = await self._resize_price(symbol, price)
        tp_price_int = await self._resize_price(symbol, worst_tp_price)
        sl_price_int = await self._resize_price(symbol, worst_sl_price)
        tp_trigger_int = await self._resize_price(symbol, take_profit_price)
        sl_trigger_int = await self._resize_price(symbol, stop_loss_price)

        virtual_order_id = self._generate_virtual_order_id()
        self._virtual_orders[virtual_order_id] = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "take_profit_price": take_profit_price,
            "stop_loss_price": stop_loss_price,
            "worst_tp_price": worst_tp_price,
            "worst_sl_price": worst_sl_price,
            "entry_price_int": entry_price_int,
            "tp_price_int": tp_price_int,
            "sl_price_int": sl_price_int,
            "tp_trigger_price_int": tp_trigger_int,
            "sl_trigger_price_int": sl_trigger_int,
            "order_expiry": order_expiry,
            "order_type": "limit_with_tp_sl",
            # fix: create_limit_order_with_tp_sl_virtual 仍然 expected_order_count = 3（entry + tp + sl），而 reconcile 默认 include_entry=False，所以它会长期 matched_partial
            "expected_order_count": 2, 
            "status": "created",
        }
        self._persist_virtual_order(virtual_order_id)

        err = None
        if isinstance(res, tuple) and len(res) >= 3:
            err = res[2]
        if err:
            self._virtual_orders[virtual_order_id]["status"] = "error"
            self._virtual_orders[virtual_order_id]["error"] = err
        else:
            self._virtual_orders[virtual_order_id]["status"] = "submitted"

        self._virtual_orders[virtual_order_id]["result"] = res
        self._persist_virtual_order(virtual_order_id)
        return virtual_order_id, res

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
            order_index=order_index,
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

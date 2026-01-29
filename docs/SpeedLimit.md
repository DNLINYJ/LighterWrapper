Lighter DEX 的 API 限速在官方文档里是按 **账号等级 +（IP 地址 & L1 钱包地址）**一起做限流的，而且 **REST 和 WebSocket 都会被限**（WebSocket 发交易也和 REST 共用同一套限额）。 ([Lighter API Documentation][1])

### 1) 主交易 REST API（`https://mainnet.zklighter.elliot.ai/`）

* **Standard 账号：60 次/分钟（按“请求数”计）** ([Lighter API Documentation][1])
* **Premium 账号：24000 “weighted requests”/分钟**（每个接口有不同 weight，消耗不同配额） ([Lighter API Documentation][1])

常见接口的 **weight（Per User）**（举例）：

* `/api/v1/sendTx` / `sendTxBatch` / `nextNonce`：**6**
* `/api/v1/trades` / `recentTrades`：**600**
* 其他未列出：**300** ([Lighter API Documentation][1])

> 换算方法：你每分钟可用的“次数”≈ 配额 / weight。比如 Premium 下 `sendTx` 大约 24000/6 ≈ 4000 次/分钟；而 `trades` 大约 24000/600 = 40 次/分钟。 ([Lighter API Documentation][1])

### 2) Explorer REST API（`https://explorer.elliot.ai/`）

* **Standard & Premium 一样：90 weighted requests/分钟**
* `/api/search` weight=3；部分 accounts 接口 weight=2；其他未列出 weight=1 ([Lighter API Documentation][1])

### 3) WebSocket 额外限制（按 IP）

* 连接数 **100**；每连接订阅 **100**；总订阅 **1000**
* 每分钟新建连接 **60**；每分钟消息 **200**（`sendTx/sendBatchTx` 不算在这 200 里，走 REST 同款限额）
* 最大并发未完成消息 **50**；唯一账号数 **10**；连接 **24 小时**会被自动断开 ([Lighter API Documentation][1])

### 4) 超限表现

* REST 会返回 **HTTP 429**；WebSocket 可能会被断开，建议做 backoff/retry。 ([Lighter API Documentation][1])

[1]: https://apidocs.lighter.xyz/docs/rate-limits "Rate Limits"

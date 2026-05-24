# SMS Gateway HTTP API

所有接口仅接受 HTTPS。鉴权通过 API Key（在 Web 控制台「API Keys」页面生成）：

```
Authorization: Bearer smsgw_<random>
```

或：

```
X-API-Key: smsgw_<random>
```

权限标签：`sms:read`（查询）、`sms:send`（发送）。错误响应统一形式：

```json
{ "detail": { "error": { "code": "rate_limited", "message": "too many requests" } } }
```

## GET /api/v1/health
无需鉴权。返回服务与模组连接状态。

```json
{ "status": "ok", "modem": "connected", "modem_port": "/dev/ttyUSB2", "uptime_seconds": 1234 }
```

## POST /api/v1/sms/send
权限：`sms:send`。请求体：

```json
{ "to": "+8613800138000", "content": "你好，世界" }
```

响应（200）：

```json
{ "message_id": 42, "status": "sent", "segments": 1 }
```

错误：
- `401 missing_api_key` / `invalid_api_key`
- `403 insufficient_scope`
- `429 rate_limited`（默认每个 Key 每分钟 30 次；含 `Retry-After` 头）
- `502 modem_error`（模组层错误，`message` 含原始 +CMS/+CME 信息）

## GET /api/v1/sms/inbox
权限：`sms:read`。Query 参数：
- `since=<epoch_seconds>`（可选）：返回该时间点之后接收到的短信（按时间升序）
- `limit=<n>`（可选，1..500，默认 50）

```json
{ "items": [ { "id": 1, "sender": "+86...", "content": "...", "received_at": 1700000000, "stored_at": 1700000001, "decode_error": false } ], "count": 1 }
```

## GET /api/v1/sms/messages/{id}
权限：`sms:read`。返回单条详情，含 `raw_pdu`。

## 速率限制

- 当前仅对 `/api/v1/sms/send` 限流（每 Key 每分钟），其它接口无限流。
- 命中限流后响应 `429`，`Retry-After` 指示需要等待的秒数。

## 安全建议

- 仅通过 HTTPS 调用；如使用反向代理，请确保 `X-Forwarded-Proto=https`。
- API Key 视同密码：泄露后立即在 Web 控制台撤销。
- 为不同集成方各自申请独立标签的 Key，便于审计与撤销。

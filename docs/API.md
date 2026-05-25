# SMS Gateway HTTP API

更完整的接入、错误码和 SDK 示例见 [API_SDK.md](API_SDK.md)。

所有业务 API 使用 API Key 鉴权。可以在 Web 控制台的 API Keys 页面生成密钥。

认证头任选一种：

```http
Authorization: Bearer smsgw_<random>
X-API-Key: smsgw_<random>
```

权限标签：

- `sms:read`：读取设备和短信
- `sms:send`：发送短信

管理员在后台创建 API Key 时，还可以设置：

- 可使用 SIM：全部可用 SIM，或仅指定设备编号
- 发送次数上限：留空为无限使用；达到上限后发送接口返回 `quota_exhausted`

## GET /api/v1/health

无需鉴权。返回服务与设备状态。

```json
{
  "status": "ok",
  "modem": "connected",
  "modem_port": "/dev/ttyUSB2,/dev/ttyUSB7",
  "devices_total": 2,
  "devices_online": 2,
  "uptime_seconds": 1234
}
```

## GET /api/v1/devices

需要 `sms:read` 权限。返回所有已发现/已绑定设备。新设备默认显示为 `编号001`、`编号002`；管理员可在 Web 后台维护 `label` 和 `sim_number_manual`。
如果 API Key 只授权了部分 SIM，本接口只返回被授权的设备。

```json
{
  "items": [
    {
      "id": 1,
      "number": 1,
      "code": "001",
      "display_name": "编号001",
      "label": null,
      "sim_number": "+8613800138000",
      "sim_number_auto": "+8613800138000",
      "sim_number_manual": null,
      "iccid": "8986...",
      "imsi": "460...",
      "imei": "86...",
      "model": "A7670G",
      "manufacturer": "SIMCom",
      "at_port": "/dev/ttyUSB2",
      "usb_path": "/sys/devices/...",
      "status": "online",
      "last_error": null
    }
  ],
  "count": 1
}
```

`sim_number_auto` 通过 `AT+CNUM` 尽力读取。很多 SIM 卡不会返回本机号码，此时 `sim_number` 会使用后台人工备注；未备注则为空，但设备名仍显示默认编号。

## POST /api/v1/sms/send

需要 `sms:send` 权限。

请求：

```json
{ "device_id": 1, "to": "+8613800138000", "content": "你好，世界" }
```

响应：

```json
{ "message_id": 42, "device_id": 1, "status": "sent", "segments": 1 }
```

`device_id` 可省略；省略时服务器自动选择该 API Key 被授权的第一台在线设备。每个设备拥有独立串口连接和发送锁，不会和其他设备抢同一个 AT 端口。

常见错误：

- `401 missing_api_key` / `invalid_api_key`
- `403 insufficient_scope`
- `403 device_not_allowed`
- `429 rate_limited`，响应头包含 `Retry-After`
- `429 quota_exhausted`
- `502 modem_error`

## GET /api/v1/sms/inbox

需要 `sms:read` 权限。

Query 参数：

- `since=<epoch_seconds>`：可选，返回该时间之后收到的短信
- `limit=<n>`：可选，范围 `1..500`，默认 50
- `device_id=<id>`：可选，仅返回指定设备的短信

如果 API Key 只授权了部分 SIM，未传 `device_id` 时只返回授权 SIM 的短信；传入未授权设备会返回 `device_not_allowed`。

```json
{
  "items": [
    {
      "id": 1,
      "device_id": 1,
      "device": { "id": 1, "code": "001", "display_name": "编号001", "sim_number": "+8613..." },
      "sender": "+86...",
      "content": "...",
      "received_at": 1700000000,
      "stored_at": 1700000001,
      "decode_error": false
    }
  ],
  "count": 1
}
```

## GET /api/v1/sms/messages/{id}

需要 `sms:read` 权限。返回单条短信详情，包含 `raw_pdu` 和设备信息。

## GET /api/v1/sms/outbox

需要 `sms:read` 权限。返回发送记录，支持 `limit` 与 `device_id` 参数，同样受 API Key 的 SIM 授权范围限制。

## Python SDK

SDK 位于 `sdk/python/smsgw_client.py`，示例见 `sdk/README.md`。

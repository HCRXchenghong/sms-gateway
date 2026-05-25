# SMS Gateway API 与 SDK 文档

本文档用于二次开发和客户端接入。系统提供 HTTP API、独立 API 客户端页面，以及一个零依赖 Python SDK。

## 基础信息

- 默认本地地址：`http://127.0.0.1:8080`
- 生产建议地址：`https://your-domain.example`
- API 前缀：`/api/v1`
- 数据格式：JSON
- 时间格式：Unix epoch seconds
- 短信保留：默认 7 天自动清理
- 设备隔离：每个 4G 模块独立 AT 串口和发送锁，避免多设备冲突
- SIM 隔离：短信记录写入当前 SIM 快照；同一设备换 SIM 后，默认只显示当前 SIM 的记录

## API Key 策略

管理员在后台 `API` 页面创建 Key。完整 Key 只显示一次，请保存到密码管理器。

Key 可配置：

- 权限：`sms:read`、`sms:send`
- 可用设备：全部设备，或指定设备 ID 列表
- 发送额度：留空为无限；填写数字后按该 Key 累计发送次数
- 失效日期：留空为永不过期；到期后 API 自动拒绝
- 删除：删除后客户端立即无法继续使用

鉴权头二选一：

```http
Authorization: Bearer smsgw_xxx
X-API-Key: smsgw_xxx
```

## 端点总览

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `GET` | `/api/v1/health` | 无 | 服务和设备健康检查 |
| `GET` | `/api/v1/devices` | `sms:read` | 当前 Key 可访问的设备列表 |
| `POST` | `/api/v1/sms/send` | `sms:send` | 发送短信 |
| `GET` | `/api/v1/sms/inbox` | `sms:read` | 收件箱 |
| `GET` | `/api/v1/sms/outbox` | `sms:read` | 发件箱 |
| `GET` | `/api/v1/sms/messages/{id}` | `sms:read` | 单条短信详情 |

## 健康检查

```bash
curl http://127.0.0.1:8080/api/v1/health
```

响应：

```json
{
  "status": "ok",
  "modem": "connected",
  "modem_port": "COM15",
  "devices_total": 1,
  "devices_online": 1,
  "uptime_seconds": 12345
}
```

## 获取设备

```bash
curl -H "Authorization: Bearer smsgw_xxx" \
  http://127.0.0.1:8080/api/v1/devices
```

响应字段重点：

- `id`：发送短信时使用的设备 ID
- `code`：后台显示编号，如 `001`
- `display_name`：管理员备注名；未备注时为编号
- `sim_key`：当前 SIM 身份，优先 ICCID，其次 IMSI
- `sim_number`：自动识别或管理员手动备注的号码
- `iccid` / `imsi` / `imei`：硬件和 SIM 识别信息
- `status`：`online`、`connecting`、`offline`

## 发送短信

```bash
curl -X POST http://127.0.0.1:8080/api/v1/sms/send \
  -H "Authorization: Bearer smsgw_xxx" \
  -H "Content-Type: application/json" \
  -d '{"device_id":1,"to":"13800138000","content":"测试短信"}'
```

`device_id` 可省略。省略时，服务端会选择该 Key 被授权的第一台在线设备。

响应：

```json
{
  "message_id": 42,
  "device_id": 1,
  "status": "sent",
  "segments": 1
}
```

中文短信会自动走 PDU/UCS2 编码；长短信会拆分并返回 `segments`。

## 查询收件箱

```bash
curl -H "Authorization: Bearer smsgw_xxx" \
  "http://127.0.0.1:8080/api/v1/sms/inbox?limit=50&device_id=1"
```

参数：

- `limit`：`1..500`，默认 `50`
- `since`：只返回该 epoch 秒之后收到的短信
- `device_id`：只看某个设备当前 SIM 的短信

响应：

```json
{
  "items": [
    {
      "id": 1,
      "device_id": 1,
      "sim_key": "iccid:8960...",
      "sim_number": null,
      "sender": "+8613800138000",
      "content": "测试",
      "received_at": 1779700000,
      "stored_at": 1779700001,
      "decode_error": false
    }
  ],
  "count": 1
}
```

## 查询发件箱

```bash
curl -H "Authorization: Bearer smsgw_xxx" \
  "http://127.0.0.1:8080/api/v1/sms/outbox?limit=50&device_id=1"
```

响应包含 `status`、`segments`、`queued_at`、`sent_at` 和失败原因 `error`。

## 错误码

常见错误：

- `401 missing_api_key`：没有传 Key
- `401 invalid_api_key`：Key 不存在、已删除、已失效或已撤销
- `403 insufficient_scope`：权限不足
- `403 device_not_allowed`：Key 不允许使用该设备
- `429 rate_limited`：超过每分钟频率限制，响应头含 `Retry-After`
- `429 quota_exhausted`：Key 发送额度已用完
- `502 modem_error`：设备离线、AT 超时或模块返回错误

## Python SDK

SDK 文件：`sdk/python/smsgw_client.py`

安装依赖：无需额外依赖，使用 Python 标准库。

```python
from sdk.python.smsgw_client import SmsGatewayClient, SmsGatewayError

client = SmsGatewayClient(
    base_url="http://127.0.0.1:8080",
    api_key="smsgw_xxx",
)

try:
    health = client.health()
    devices = client.list_devices()

    device_id = devices["items"][0]["id"]
    sent = client.send_sms(
        to="13800138000",
        content="测试短信",
        device_id=device_id,
    )

    inbox = client.list_inbox(limit=20, device_id=device_id)
    outbox = client.list_outbox(limit=20, device_id=device_id)
    detail = client.get_message(inbox["items"][0]["id"]) if inbox["items"] else None
except SmsGatewayError as exc:
    print(exc.status, exc.payload)
```

## JavaScript 调用示例

```js
async function sendSms({ baseUrl, apiKey, to, content, deviceId }) {
  const response = await fetch(`${baseUrl}/api/v1/sms/send`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      "Accept": "application/json"
    },
    body: JSON.stringify({
      to,
      content,
      device_id: deviceId || null
    })
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data?.detail?.error?.message || data?.detail || "send failed");
  }
  return data;
}
```

## 独立 API 客户端

目录：`api-client/`

用途：给 API 使用方单独打开，不包含后台管理能力。用户输入 API 地址和 Key 后，只能发送短信、查看收件箱和发件箱。适合交付给客户或业务系统操作人员。

## 二次开发建议

- 新增 API 时优先放在 `app/routes_api.py`
- 后台管理接口放在 `app/routes_web.py`
- 设备和短信数据访问放在 `app/repo.py`
- 串口和 AT 命令逻辑放在 `app/modem.py`
- 多设备发现和防冲突逻辑放在 `app/modem_manager.py`
- PDU 编解码放在 `app/pdu.py`
- 前端后台页面在 `app/templates/app.html` 和 `app/static/app.js`
- API Key 不要明文保存，数据库只保存 hash 和前缀
- 不要提交 `_realdata/`、`_devdata/`、`.venv/`、`*.db`、`*.log`、`secret.key`

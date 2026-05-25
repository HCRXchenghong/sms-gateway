# SMS Gateway SDK

## Python

The Python SDK is dependency-free and uses the standard library.

```python
from sdk.python.smsgw_client import SmsGatewayClient

client = SmsGatewayClient("https://sms.example.com:8443", "smsgw_xxx")

devices = client.list_devices()
device_id = devices["items"][0]["id"]

client.send_sms("+8613800138000", "hello", device_id=device_id)
messages = client.list_inbox(limit=20, device_id=device_id)
sent = client.list_outbox(limit=20, device_id=device_id)
```

If `device_id` is omitted when sending, the server selects the first online device allowed by the API key. Device lists, inbox, outbox, and sending are all limited by the SIM scope configured for that key.

See the full API and SDK guide at `docs/API_SDK.md`.

"""Small dependency-free Python SDK for SMS Gateway."""
from __future__ import annotations

import json
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class SmsGatewayError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"SMS Gateway API error {status}: {payload}")


class SmsGatewayClient:
    """HTTP client for SMS Gateway API v1.

    Example:
        client = SmsGatewayClient("https://sms.example.com:8443", "smsgw_xxx")
        devices = client.list_devices()
        client.send_sms("+8613800138000", "hello", device_id=devices["items"][0]["id"])
    """

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/health", auth=False)

    def list_devices(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/devices")

    def send_sms(self, to: str, content: str, device_id: Optional[int] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"to": to, "content": content}
        if device_id is not None:
            body["device_id"] = device_id
        return self._request("POST", "/api/v1/sms/send", body)

    def list_inbox(
        self,
        since: Optional[int] = None,
        limit: int = 50,
        device_id: Optional[int] = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit}
        if since is not None:
            query["since"] = since
        if device_id is not None:
            query["device_id"] = device_id
        return self._request("GET", "/api/v1/sms/inbox?" + urlencode(query))

    def list_outbox(self, limit: int = 50, device_id: Optional[int] = None) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit}
        if device_id is not None:
            query["device_id"] = device_id
        return self._request("GET", "/api/v1/sms/outbox?" + urlencode(query))

    def get_message(self, message_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/sms/messages/{message_id}")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", errors="replace")
            raise SmsGatewayError(e.code, payload) from e

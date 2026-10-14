"""Shared conversation CRUD against a running HTTP/WebSocket server."""
import sqlite3
import time
from urllib.parse import quote

import pytest

from tests.live_server import LiveServer
from tests.test_live_integration import WsCollector


@pytest.fixture()
def gateway():
    server = LiveServer().start()
    server.create_admin()
    token = server.login('liveadmin', 'LivePass123')
    try:
        yield server, token
    finally:
        server.stop()


def req(gateway, method, path, body=None, expect=200):
    server, token = gateway
    return server.request(method, path, token=token, body=body, expect=expect)[1]


def conversations(gateway):
    return {c['peer_key']: c for c in req(gateway, 'GET', '/app/conversations')['items']}


def receive(gateway, peer, device=1):
    req(gateway, 'POST', f'/app/sms/mock/{device}/incoming', {'sender': peer, 'content': '短信管理测试'})
    deadline = time.time() + 3
    while time.time() < deadline:
        if conversations(gateway):
            return
        time.sleep(.05)


def test_read_unread_bulk_delete_and_websocket(gateway):
    server, token = gateway
    with WsCollector(server.ws_url, token) as ws:
        receive(gateway, '+8613800000101')
        receive(gateway, '13800000101', device=2)
        assert conversations(gateway)['13800000101']['unread'] == 2
        path = '/app/conversations/13800000101'
        req(gateway, 'GET', path + '/messages?mark_read=false')
        assert conversations(gateway)['13800000101']['unread'] == 2
        req(gateway, 'GET', path + '/messages')
        assert conversations(gateway)['13800000101']['unread'] == 0
        req(gateway, 'POST', path + '/unread')
        assert conversations(gateway)['13800000101']['unread'] > 0
        # Passive refresh must not clear an explicit unread marker.
        req(gateway, 'GET', path + '/messages?mark_read=false')
        assert conversations(gateway)['13800000101']['unread'] > 0
        receive(gateway, '13800000102')
        result = req(gateway, 'POST', '/app/conversations/bulk_delete',
                     {'peers': ['008613800000101', '13800000102']})
        assert result['deleted_conversations'] == 2
        assert result['deleted_messages'] == 3
        assert conversations(gateway) == {}
        assert req(gateway, 'GET', '/app/conversations')['unread_total'] == 0
        assert any(e['event'] == 'sms.conversations_changed' for e in ws.snapshot())
    assert req(gateway, 'GET', '/app/inbox')['items'] == []


def test_delete_read_preserves_unread_drafts_and_inflight(gateway):
    for peer in ['13800000201', '13800000202', '13800000203']:
        receive(gateway, peer)
    for peer in ['13800000201', '13800000203']:
        req(gateway, 'POST', f'/app/conversations/{peer}/read')
    req(gateway, 'POST', '/app/conversations/13800000203/draft', {'draft': '保留草稿'})
    result = req(gateway, 'POST', '/app/conversations/delete_read')
    assert result['deleted_conversations'] == 1
    assert set(conversations(gateway)) == {'13800000202', '13800000203'}
    req(gateway, 'POST', '/app/conversations/13800000203/draft', {'draft': None})
    result = req(gateway, 'POST', '/app/conversations/delete_read')
    assert result['deleted_conversations'] == 1
    assert set(conversations(gateway)) == {'13800000202'}
    # A failed selection is atomic: do not partly delete other selected threads.
    server, _ = gateway
    from app.config import settings
    db_path = server.root / 'data' / settings.db_path.name
    with sqlite3.connect(db_path) as c:
        c.execute("INSERT INTO sms_outbox (recipient,content,status,queued_at) VALUES (?,?,?,?)",
                  ('13800000204', 'pending', 'queued', int(time.time())))
    req(gateway, 'POST', '/app/conversations/bulk_delete', {'peers': ['13800000202', '13800000204']}, expect=409)
    assert '13800000202' in conversations(gateway)
    req(gateway, 'POST', '/app/conversations/delete_read')
    assert '13800000204' in conversations(gateway)


def test_delete_normalized_inbound_and_outbound_and_auth(gateway):
    receive(gateway, '13800000301')
    req(gateway, 'POST', '/app/send', {'to': '+8613800000301', 'content': '模拟发送', 'device_id': 1})
    req(gateway, 'POST', '/app/conversations/13800000301/unread')
    result = req(gateway, 'DELETE', '/app/conversations/' + quote('+8613800000301', safe=''))
    assert result['deleted_messages'] >= 2  # Mock modem can also echo the sent message.
    assert conversations(gateway) == {}
    assert req(gateway, 'GET', '/app/outbox')['items'] == []
    server, _ = gateway
    server.request('POST', '/app/conversations/delete_read', expect=401)
    req(gateway, 'POST', '/app/conversations/bulk_delete', {'peers': []}, expect=422)
    req(gateway, 'POST', '/app/conversations/bulk_delete', {'peers': ['bad']}, expect=400)
    req(gateway, 'POST', '/app/conversations/13800000999/unread', expect=404)

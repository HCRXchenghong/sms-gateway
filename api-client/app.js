(function() {
    'use strict';

    var state = {
        baseUrl: localStorage.getItem('smsgw.client.baseUrl') || '',
        apiKey: localStorage.getItem('smsgw.client.apiKey') || '',
        devices: [],
        activeView: 'send',
        activeDevice: ''
    };

    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
    function el(tag, attrs, text) {
        var node = document.createElement(tag);
        if (attrs) Object.keys(attrs).forEach(function(k) {
            if (attrs[k] === null || attrs[k] === undefined || attrs[k] === false) return;
            if (k === 'class') node.className = attrs[k];
            else node.setAttribute(k, attrs[k]);
        });
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }
    function fmtTime(epoch) {
        if (!epoch) return '';
        return new Date(epoch * 1000).toLocaleString();
    }
    function deviceName(device) {
        return device ? (device.display_name || ('编号' + device.code)) : '未绑定';
    }
    function setStatus(text, ok) {
        var node = $('#connect-status');
        node.textContent = text || '';
        node.style.color = ok === false ? 'var(--bad)' : 'var(--muted)';
    }
    async function readError(resp) {
        try {
            var data = await resp.json();
            if (data.detail && data.detail.error) return data.detail.error.message || data.detail.error.code;
            if (data.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
            return JSON.stringify(data);
        } catch (e) {
            return await resp.text();
        }
    }
    async function api(path, opts) {
        var resp = await fetch(state.baseUrl.replace(/\/+$/, '') + path, Object.assign({
            headers: { 'Authorization': 'Bearer ' + state.apiKey, 'Accept': 'application/json' }
        }, opts || {}));
        if (!resp.ok) throw new Error(await readError(resp));
        return resp.json();
    }

    function showApp() {
        $('#connect-screen').classList.add('hidden');
        $('#app-screen').classList.remove('hidden');
    }
    function showConnect() {
        $('#app-screen').classList.add('hidden');
        $('#connect-screen').classList.remove('hidden');
    }
    function switchView(view) {
        state.activeView = view;
        var titles = { send: '发送短信', inbox: '接收短信', sent: '发送记录' };
        $('#view-title').textContent = titles[view] || '短信客户端';
        $$('.view').forEach(function(v) { v.classList.toggle('active', v.id === 'view-' + view); });
        $$('[data-view]').forEach(function(b) { b.classList.toggle('active', b.dataset.view === view); });
        if (view === 'inbox' || view === 'sent') refreshMessages();
    }

    function renderDeviceSelectors() {
        var send = $('#send-device');
        var filter = $('#device-filter');
        send.innerHTML = '';
        filter.innerHTML = '';
        send.appendChild(el('option', { value: '' }, '自动选择在线 SIM'));
        filter.appendChild(el('option', { value: '' }, '全部授权 SIM'));
        state.devices.forEach(function(d) {
            var name = deviceName(d) + (d.status === 'online' ? ' · 在线' : ' · 离线');
            send.appendChild(el('option', { value: String(d.id), disabled: d.status !== 'online' ? 'disabled' : null }, name));
            filter.appendChild(el('option', { value: String(d.id) }, deviceName(d)));
        });
        filter.value = state.activeDevice;
    }
    async function refreshDevices() {
        var data = await api('/api/v1/devices');
        state.devices = data.items || [];
        renderDeviceSelectors();
    }
    function messageCard(item, kind) {
        var card = el('article', { class: 'message-card' });
        var head = el('div', { class: 'message-head' });
        head.appendChild(el('span', { class: 'message-title' }, kind === 'sent' ? (item.recipient || '-') : (item.sender || '-')));
        head.appendChild(el('span', null, fmtTime(item.received_at || item.sent_at || item.queued_at)));
        card.appendChild(head);
        card.appendChild(el('div', { class: 'message-head' }, deviceName(item.device) + (kind === 'sent' ? ' · ' + item.status : '')));
        card.appendChild(el('div', { class: 'message-text' }, item.content || ''));
        return card;
    }
    function renderList(selector, items, kind) {
        var list = $(selector);
        list.innerHTML = '';
        if (!items.length) {
            list.appendChild(el('div', { class: 'empty' }, kind === 'sent' ? '暂无发送记录' : '暂无收件短信'));
            return;
        }
        items.forEach(function(item) { list.appendChild(messageCard(item, kind)); });
    }
    async function refreshMessages() {
        var query = '?limit=100' + (state.activeDevice ? '&device_id=' + encodeURIComponent(state.activeDevice) : '');
        if (state.activeView === 'inbox') {
            renderList('#inbox-list', (await api('/api/v1/sms/inbox' + query)).items || [], 'inbox');
        }
        if (state.activeView === 'sent') {
            renderList('#sent-list', (await api('/api/v1/sms/outbox' + query)).items || [], 'sent');
        }
    }
    async function connect(baseUrl, apiKey) {
        state.baseUrl = baseUrl.replace(/\/+$/, '');
        state.apiKey = apiKey;
        setStatus('连接中...');
        await refreshDevices();
        localStorage.setItem('smsgw.client.baseUrl', state.baseUrl);
        localStorage.setItem('smsgw.client.apiKey', state.apiKey);
        showApp();
        switchView('send');
    }

    $('#connect-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        var fd = new FormData(e.target);
        try {
            await connect(String(fd.get('base_url') || ''), String(fd.get('api_key') || ''));
        } catch (err) {
            setStatus('连接失败：' + err.message, false);
        }
    });
    $('#send-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        var fd = new FormData(e.target);
        var status = $('#send-status');
        status.textContent = '发送中...';
        status.style.color = 'var(--muted)';
        var deviceId = fd.get('device_id');
        try {
            await api('/api/v1/sms/send', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + state.apiKey,
                    'Accept': 'application/json',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    device_id: deviceId ? Number(deviceId) : null,
                    to: fd.get('to'),
                    content: fd.get('content')
                })
            });
            status.textContent = '已发送';
            status.style.color = 'var(--ok)';
            e.target.reset();
        } catch (err) {
            status.textContent = '失败：' + err.message;
            status.style.color = 'var(--bad)';
        }
    });
    $('#device-filter').addEventListener('change', function(e) {
        state.activeDevice = e.target.value;
        refreshMessages();
    });
    function disconnect() {
        localStorage.removeItem('smsgw.client.apiKey');
        state.apiKey = '';
        showConnect();
    }
    $('#disconnect-button').addEventListener('click', disconnect);
    $$('.disconnect-action').forEach(function(btn) { btn.addEventListener('click', disconnect); });
    $$('[data-view]').forEach(function(btn) {
        btn.addEventListener('click', function() { switchView(btn.dataset.view); });
    });

    $('input[name="base_url"]').value = state.baseUrl;
    $('input[name="api_key"]').value = state.apiKey;
    if (state.baseUrl && state.apiKey) {
        connect(state.baseUrl, state.apiKey).catch(function(err) {
            showConnect();
            setStatus('自动连接失败：' + err.message, false);
        });
    }
    setInterval(function() {
        if (!$('#app-screen').classList.contains('hidden')) {
            refreshDevices().catch(function() {});
            refreshMessages().catch(function() {});
        }
    }, 5000);
})();

/* SMS Gateway dashboard logic. Vanilla JS, no build step. */
(function() {
    'use strict';

    var state = { devices: [], activeDeviceFilter: '', editingKey: null };

    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
    function el(tag, attrs, text) {
        var n = document.createElement(tag);
        if (attrs) for (var k in attrs) {
            if (attrs[k] === null || attrs[k] === undefined || attrs[k] === false) continue;
            if (k === 'class') n.className = attrs[k];
            else if (k === 'dataset') Object.assign(n.dataset, attrs[k]);
            else n.setAttribute(k, attrs[k]);
        }
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }
    function fmtTime(epoch) {
        if (!epoch) return '';
        return new Date(epoch * 1000).toLocaleString();
    }
    function deviceName(d) {
        return d ? (d.display_name || ('编号' + d.code)) : '未绑定';
    }
    function messageDeviceName(m) {
        return m.device ? deviceName(m.device) : '未绑定';
    }
    async function api(path, opts) {
        var r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts || {}));
        if (r.status === 401) { window.location.href = '/login'; throw new Error('unauthorized'); }
        return r;
    }
    async function errorText(r) {
        try {
            var data = await r.json();
            if (data.detail && data.detail.error) return data.detail.error.message || data.detail.error.code;
            if (data.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
            return JSON.stringify(data);
        } catch (e) {
            return await r.text();
        }
    }

    $$('.nav-item').forEach(function(t) {
        t.addEventListener('click', function() {
            $$('.nav-item').forEach(function(x) { x.classList.remove('active'); });
            $$('.view').forEach(function(x) { x.classList.remove('active'); });
            t.classList.add('active');
            $('#tab-' + t.dataset.tab).classList.add('active');
        });
    });

    function statusChip(status) {
        var cls = status === 'online' ? 'chip ok' : (status === 'connecting' ? 'chip warn' : 'chip bad');
        var text = status === 'online' ? '在线' : (status === 'connecting' ? '连接中' : '离线');
        return el('span', { class: cls }, text);
    }

    function renderDevices(items) {
        state.devices = items || [];
        var grid = $('#devices-grid');
        grid.innerHTML = '';
        if (!state.devices.length) {
            grid.appendChild(el('div', { class: 'panel muted' }, '暂未发现设备。插入模块后系统会自动扫描。'));
        }
        state.devices.forEach(function(d) {
            var card = el('div', { class: 'device-card' });
            var top = el('div', { class: 'device-top' });
            var title = el('div');
            title.appendChild(el('div', { class: 'device-name' }, deviceName(d)));
            title.appendChild(el('div', { class: 'device-code' }, '设备编号 ' + d.code));
            top.appendChild(title);
            top.appendChild(statusChip(d.status));
            card.appendChild(top);

            var meta = el('div', { class: 'device-meta' });
            meta.appendChild(el('div', null, 'SIM：' + (d.sim_number || '未识别/未备注')));
            meta.appendChild(el('div', null, '端口：' + (d.at_port || '-')));
            meta.appendChild(el('div', null, 'IMEI：' + (d.imei || '-')));
            meta.appendChild(el('div', null, 'ICCID：' + (d.iccid || '-')));
            if (d.last_error) meta.appendChild(el('div', null, '错误：' + d.last_error));
            card.appendChild(meta);

            var edit = el('form', { class: 'device-edit', dataset: { id: String(d.id) } });
            edit.appendChild(el('input', { type: 'text', name: 'label', placeholder: '设备备注', value: d.label || '' }));
            edit.appendChild(el('input', { type: 'text', name: 'sim', placeholder: 'SIM 号码备注', value: d.sim_number_manual || '' }));
            edit.appendChild(el('button', { type: 'submit' }, '保存'));
            edit.addEventListener('submit', saveDevice);
            card.appendChild(edit);
            grid.appendChild(card);
        });
        renderDeviceSelectors();
        renderKeyDeviceOptions();
        var online = state.devices.filter(function(d) { return d.status === 'online'; }).length;
        $('#online-summary').textContent = online + '/' + state.devices.length + ' 在线';
    }

    function renderDeviceSelectors() {
        var send = $('#send-device');
        var filter = $('#message-device-filter');
        if (!send || !filter) return;
        var currentSend = send.value;
        var currentFilter = filter.value || state.activeDeviceFilter;
        send.innerHTML = '';
        filter.innerHTML = '';
        send.appendChild(el('option', { value: '' }, '自动选择在线设备'));
        filter.appendChild(el('option', { value: '' }, '全部设备'));
        state.devices.forEach(function(d) {
            var label = deviceName(d) + (d.status === 'online' ? ' · 在线' : ' · 离线');
            send.appendChild(el('option', { value: String(d.id), disabled: d.status !== 'online' ? 'disabled' : null }, label));
            filter.appendChild(el('option', { value: String(d.id) }, deviceName(d)));
        });
        send.value = currentSend;
        filter.value = currentFilter;
    }

    function renderKeyDeviceOptions() {
        var box = $('#key-device-options');
        var all = $('input[name="all_devices"]');
        if (!box || !all) return;
        var selected = new Set($$('input[name="allowed_device"]', box).filter(function(x) { return x.checked; }).map(function(x) { return x.value; }));
        box.innerHTML = '';
        state.devices.forEach(function(d) {
            var label = el('label', { class: 'check device-check' });
            label.appendChild(el('input', {
                type: 'checkbox',
                name: 'allowed_device',
                value: String(d.id),
                checked: selected.has(String(d.id)) ? 'checked' : null,
                disabled: all.checked ? 'disabled' : null
            }));
            label.appendChild(document.createTextNode(deviceName(d)));
            box.appendChild(label);
        });
    }

    function epochToDateInput(epoch) {
        if (!epoch) return '';
        var d = new Date(epoch * 1000);
        var y = d.getFullYear();
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return y + '-' + m + '-' + day;
    }

    function dateInputToEpoch(value) {
        if (!value) return null;
        var d = new Date(value + 'T23:59:59');
        if (Number.isNaN(d.getTime())) return null;
        return Math.floor(d.getTime() / 1000);
    }

    function setKeyFormMode(key) {
        state.editingKey = key || null;
        var modal = $('#key-modal');
        var title = $('#key-modal-title');
        var form = $('#key-create-form');
        var submit = $('#key-submit-button');
        var cancel = $('#key-cancel-edit');
        if (!modal || !form || !submit || !cancel) return;
        if (!key) {
            form.reset();
            $('input[name="all_devices"]').checked = true;
            if (title) title.textContent = '新建 Key';
            submit.textContent = '保存并生成';
            renderKeyDeviceOptions();
            modal.hidden = false;
            form.elements.label.focus();
            return;
        }
        if (title) title.textContent = '编辑 Key';
        form.elements.label.value = key.label || '';
        form.elements.scope_read.checked = (key.scopes || []).indexOf('sms:read') !== -1;
        form.elements.scope_send.checked = (key.scopes || []).indexOf('sms:send') !== -1;
        form.elements.send_limit.value = key.send_limit || '';
        form.elements.expires_at_date.value = epochToDateInput(key.expires_at);
        form.elements.all_devices.checked = key.allowed_device_ids === null || key.allowed_device_ids === undefined;
        renderKeyDeviceOptions();
        var allowed = new Set((key.allowed_device_ids || []).map(String));
        $$('#key-device-options input[name="allowed_device"]').forEach(function(input) {
            input.checked = allowed.has(input.value);
        });
        submit.textContent = '保存策略';
        modal.hidden = false;
        form.elements.label.focus();
    }

    function closeKeyModal() {
        var modal = $('#key-modal');
        if (modal) modal.hidden = true;
        state.editingKey = null;
    }

    function showGeneratedKeyModal(key) {
        var modal = $('#key-display-modal');
        var code = $('#generated-key-text');
        if (!modal || !code) return;
        code.textContent = key || '';
        modal.hidden = false;
    }

    function closeGeneratedKeyModal() {
        var modal = $('#key-display-modal');
        if (modal) modal.hidden = true;
    }

    async function copyGeneratedKey() {
        var code = $('#generated-key-text');
        var button = $('#copy-generated-key');
        var value = code ? code.textContent : '';
        if (!value) return;
        try {
            await navigator.clipboard.writeText(value);
            if (button) button.textContent = '已复制，可直接粘贴';
        } catch (err) {
            var range = document.createRange();
            range.selectNodeContents(code);
            var sel = window.getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
            if (button) button.textContent = '已选中，请按 Ctrl+C';
        }
    }

    async function saveDevice(e) {
        e.preventDefault();
        var form = e.target;
        var id = form.dataset.id;
        var fd = new FormData(form);
        var r = await api('/app/devices/' + id, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                label: fd.get('label') || null,
                sim_number_manual: fd.get('sim') || null
            })
        });
        if (!r.ok) alert(await errorText(r));
        await refreshDevices();
        await refreshMessages();
    }

    async function refreshDevices() {
        var r = await api('/app/devices');
        var data = await r.json();
        renderDevices(data.items || []);
    }

    function initDeviceScanModal() {
        var trigger = $('#scan-devices-button');
        if (!trigger) return;

        var backdrop = el('div', { class: 'modal-backdrop' });
        backdrop.hidden = true;
        var modal = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true' });
        var head = el('div', { class: 'modal-head' });
        var titleWrap = el('div');
        titleWrap.appendChild(el('h2', null, '搜索可用设备'));
        titleWrap.appendChild(el('p', { class: 'muted' }, '系统会持续扫描 60 秒，发现设备后会自动显示在这里。'));
        var close = el('button', { type: 'button', class: 'icon-button', 'aria-label': '关闭' }, '×');
        head.appendChild(titleWrap);
        head.appendChild(close);
        modal.appendChild(head);

        var scanStatus = el('div', { class: 'scan-status' });
        var dot = el('span', { class: 'scan-dot' });
        var statusText = el('span', null, '准备开始搜索');
        var countdown = el('span', { class: 'scan-countdown' }, '60 秒');
        scanStatus.appendChild(dot);
        scanStatus.appendChild(statusText);
        scanStatus.appendChild(countdown);
        modal.appendChild(scanStatus);

        var list = el('div', { class: 'scan-list' });
        list.appendChild(el('div', { class: 'empty-state' }, '点击开始后会自动搜索 USB/AT 口设备。'));
        modal.appendChild(list);

        var actions = el('div', { class: 'modal-actions' });
        var start = el('button', { type: 'button', class: 'button primary' }, '开始搜索');
        var stop = el('button', { type: 'button', class: 'button' }, '停止');
        actions.appendChild(start);
        actions.appendChild(stop);
        modal.appendChild(actions);
        backdrop.appendChild(modal);
        document.body.appendChild(backdrop);

        var running = false;
        var busy = false;
        var deadline = 0;
        var countdownTimer = null;
        var pollTimer = null;

        function labelStatus(status) {
            if (status === 'online') return '在线可用';
            if (status === 'connecting') return '连接中';
            if (status === 'offline') return '离线';
            return status || '未知';
        }

        function renderScanItems(items) {
            list.innerHTML = '';
            if (!items.length) {
                list.appendChild(el('div', { class: 'empty-state' }, '暂时没有发现设备，请确认模块已插入并等待搜索完成。'));
                return;
            }
            items.forEach(function(device) {
                var card = el('div', { class: 'scan-device' });
                var top = el('div', { class: 'scan-device-top' });
                top.appendChild(el('strong', null, device.display_name || ('编号' + device.code)));
                top.appendChild(el('span', { class: 'chip ' + (device.status === 'online' ? 'ok' : 'warn') }, labelStatus(device.status)));
                card.appendChild(top);
                card.appendChild(el('div', { class: 'scan-device-meta' }, '设备编号 ' + device.code + (device.at_port ? ' · ' + device.at_port : '')));
                card.appendChild(el('div', { class: 'scan-device-meta' }, 'SIM：' + (device.sim_number || '未识别，可登录后手动备注')));
                card.appendChild(el('div', { class: 'scan-device-meta' }, '型号：' + (device.model || '等待识别') + ' · ' + (device.manufacturer || '')));
                if (device.last_error) card.appendChild(el('div', { class: 'scan-error' }, device.last_error));
                list.appendChild(card);
            });
        }

        function updateCountdown() {
            var remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
            countdown.textContent = remaining + ' 秒';
            if (remaining <= 0) stopScan('搜索结束');
        }

        async function pollDevices() {
            if (!running || busy) return;
            busy = true;
            try {
                var r = await api('/app/devices/scan', { method: 'POST', headers: { 'Accept': 'application/json' } });
                if (!r.ok) throw new Error(await errorText(r));
                var data = await r.json();
                renderDevices(data.items || []);
                renderScanItems(data.items || []);
                if (data.scan_error) {
                    statusText.textContent = '搜索时遇到问题：' + data.scan_error;
                    dot.classList.add('warn');
                } else if (data.devices_online > 0) {
                    statusText.textContent = '已发现 ' + data.devices_online + ' 个在线设备，共 ' + data.devices_total + ' 个设备';
                    dot.classList.remove('warn');
                } else {
                    statusText.textContent = '正在搜索可用设备...';
                    dot.classList.remove('warn');
                }
            } catch (err) {
                statusText.textContent = err.message || '搜索失败';
                dot.classList.add('warn');
            } finally {
                busy = false;
            }
        }

        function startScan() {
            if (running) return;
            running = true;
            deadline = Date.now() + 60000;
            statusText.textContent = '正在搜索可用设备...';
            dot.classList.remove('warn');
            start.disabled = true;
            updateCountdown();
            pollDevices();
            countdownTimer = window.setInterval(updateCountdown, 1000);
            pollTimer = window.setInterval(pollDevices, 5000);
        }

        function stopScan(message) {
            running = false;
            window.clearInterval(countdownTimer);
            window.clearInterval(pollTimer);
            countdownTimer = null;
            pollTimer = null;
            start.disabled = false;
            if (message) statusText.textContent = message;
        }

        function openModal() {
            backdrop.hidden = false;
            if (!running) startScan();
        }

        function closeModal() {
            stopScan();
            backdrop.hidden = true;
        }

        trigger.addEventListener('click', openModal);
        close.addEventListener('click', closeModal);
        start.addEventListener('click', startScan);
        stop.addEventListener('click', function() { stopScan('已停止搜索'); });
        backdrop.addEventListener('click', function(event) {
            if (event.target === backdrop) closeModal();
        });
        document.addEventListener('keydown', function(event) {
            if (event.key === 'Escape' && !backdrop.hidden) closeModal();
            if (event.key === 'Escape' && $('#key-modal') && !$('#key-modal').hidden) closeKeyModal();
            if (event.key === 'Escape' && $('#key-display-modal') && !$('#key-display-modal').hidden) closeGeneratedKeyModal();
        });
    }

    function renderInbox(items) {
        var tb = $('#inbox-table tbody');
        tb.innerHTML = '';
        items.forEach(function(m) {
            var row = el('tr');
            row.appendChild(el('td', null, fmtTime(m.received_at)));
            row.appendChild(el('td', null, messageDeviceName(m)));
            row.appendChild(el('td', null, m.sender || '(unknown)'));
            row.appendChild(el('td', null, m.content));
            tb.appendChild(row);
        });
    }
    function prependInbox(m) {
        var tb = $('#inbox-table tbody');
        var row = el('tr');
        row.appendChild(el('td', null, fmtTime(m.received_at)));
        row.appendChild(el('td', null, messageDeviceName(m)));
        row.appendChild(el('td', null, m.sender || '(unknown)'));
        row.appendChild(el('td', null, m.content));
        tb.insertBefore(row, tb.firstChild);
    }

    function renderOutbox(items) {
        var tb = $('#outbox-table tbody');
        tb.innerHTML = '';
        items.forEach(function(m) {
            var row = el('tr');
            row.appendChild(el('td', null, fmtTime(m.sent_at || m.queued_at)));
            row.appendChild(el('td', null, messageDeviceName(m)));
            row.appendChild(el('td', null, m.recipient));
            var st = el('td', null, m.status);
            if (m.status === 'sent') st.style.color = 'var(--ok)';
            if (m.status === 'failed') { st.style.color = 'var(--error)'; st.title = m.error || ''; }
            row.appendChild(st);
            row.appendChild(el('td', null, m.content));
            tb.appendChild(row);
        });
    }

    async function refreshMessages() {
        var qs = state.activeDeviceFilter ? ('?device_id=' + encodeURIComponent(state.activeDeviceFilter)) : '';
        var inbox = await api('/app/inbox' + qs);
        var outbox = await api('/app/outbox' + qs);
        renderInbox((await inbox.json()).items || []);
        renderOutbox((await outbox.json()).items || []);
    }

    $('#message-device-filter').addEventListener('change', function(e) {
        state.activeDeviceFilter = e.target.value;
        refreshMessages();
    });

    $('#send-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        var fd = new FormData(e.target);
        var status = $('#send-status');
        status.textContent = '发送中...';
        status.style.color = '';
        var deviceId = fd.get('device_id');
        try {
            var r = await api('/app/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    device_id: deviceId ? Number(deviceId) : null,
                    to: fd.get('to'),
                    content: fd.get('content')
                })
            });
            if (!r.ok) throw new Error(await errorText(r));
            var data = await r.json();
            status.textContent = '已发送（设备 #' + data.device_id + '，' + data.segments + ' 段）';
            status.style.color = 'var(--ok)';
            e.target.reset();
            await refreshMessages();
        } catch (err) {
            status.textContent = '失败：' + err.message;
            status.style.color = 'var(--error)';
        }
    });

    function keyDeviceScope(k) {
        if (k.allowed_device_ids === null || k.allowed_device_ids === undefined) return '全部设备';
        if (!k.allowed_device_ids.length) return '未授权设备';
        return k.allowed_device_ids.map(function(id) {
            var d = state.devices.find(function(x) { return Number(x.id) === Number(id); });
            return d ? deviceName(d) : ('#' + id);
        }).join('、');
    }
    function keyQuota(k) {
        if (!k.send_limit) return (k.send_count || 0) + ' / 无限';
        return (k.send_count || 0) + ' / ' + k.send_limit;
    }
    function scopeText(scopes) {
        var names = { 'sms:read': '读取', 'sms:send': '发送' };
        return (scopes || []).map(function(s) { return names[s] || s; }).join('、');
    }
    function keyExpiry(k) {
        return k.expires_at ? fmtTime(k.expires_at) : '永不过期';
    }
    function keyStatus(k) {
        var now = Math.floor(Date.now() / 1000);
        if (k.revoked_at) return '已撤销';
        if (k.expires_at && k.expires_at <= now) return '已失效';
        return '有效';
    }

    function renderKeys(items) {
        var tb = $('#keys-table tbody');
        tb.innerHTML = '';
        items.forEach(function(k) {
            var row = el('tr');
            row.appendChild(el('td', null, k.label));
            row.appendChild(el('td', null, k.prefix + '...'));
            row.appendChild(el('td', null, scopeText(k.scopes)));
            row.appendChild(el('td', null, keyDeviceScope(k)));
            row.appendChild(el('td', null, keyQuota(k)));
            row.appendChild(el('td', null, keyExpiry(k)));
            row.appendChild(el('td', null, keyStatus(k)));
            var actions = el('td');
            if (!k.revoked_at) {
                var edit = el('button', { class: 'text-button' }, '编辑');
                edit.addEventListener('click', function() {
                    setKeyFormMode(k);
                });
                actions.appendChild(edit);
                actions.appendChild(document.createTextNode(' '));
            }
            var b = el('button', { class: 'text-button' }, '删除');
            b.addEventListener('click', async function() {
                if (!confirm('删除 Key "' + k.label + '"？删除后客户端会立即不能再使用。')) return;
                await api('/app/keys/' + k.id, { method: 'DELETE' });
                refreshKeys();
            });
            actions.appendChild(b);
            row.appendChild(actions);
            tb.appendChild(row);
        });
    }
    async function refreshKeys() {
        var r = await api('/app/keys');
        var data = await r.json();
        renderKeys(data.items || []);
    }

    var allDevices = $('input[name="all_devices"]');
    if (allDevices) {
        allDevices.addEventListener('change', renderKeyDeviceOptions);
    }
    var openKeyButton = $('#open-key-modal');
    if (openKeyButton) {
        openKeyButton.addEventListener('click', function() { setKeyFormMode(null); });
    }
    var closeKeyButton = $('#close-key-modal');
    if (closeKeyButton) {
        closeKeyButton.addEventListener('click', closeKeyModal);
    }
    var keyModal = $('#key-modal');
    if (keyModal) {
        keyModal.addEventListener('click', function(event) {
            if (event.target === keyModal) closeKeyModal();
        });
    }
    var keyDisplayModal = $('#key-display-modal');
    if (keyDisplayModal) {
        keyDisplayModal.addEventListener('click', function(event) {
            if (event.target === keyDisplayModal) closeGeneratedKeyModal();
        });
    }
    var closeKeyDisplayButton = $('#close-key-display');
    if (closeKeyDisplayButton) {
        closeKeyDisplayButton.addEventListener('click', closeGeneratedKeyModal);
    }
    var dismissKeyButton = $('#dismiss-generated-key');
    if (dismissKeyButton) {
        dismissKeyButton.addEventListener('click', closeGeneratedKeyModal);
    }
    var copyKeyButton = $('#copy-generated-key');
    if (copyKeyButton) {
        copyKeyButton.addEventListener('click', copyGeneratedKey);
    }
    $('#key-cancel-edit').addEventListener('click', closeKeyModal);

    $('#key-create-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        var fd = new FormData(e.target);
        var scopes = [];
        if (fd.get('scope_read')) scopes.push('sms:read');
        if (fd.get('scope_send')) scopes.push('sms:send');
        if (!scopes.length) { alert('请至少选择一个权限'); return; }
        var all = Boolean(fd.get('all_devices'));
        var allowed = null;
        if (!all) {
            allowed = $$('#key-device-options input[name="allowed_device"]:checked').map(function(x) { return Number(x.value); });
            if (!allowed.length) { alert('请选择至少一个设备，或勾选全部可用设备'); return; }
        }
        var limitValue = fd.get('send_limit');
        var expiresAt = dateInputToEpoch(fd.get('expires_at_date'));
        var body = {
            label: fd.get('label'),
            scopes: scopes,
            allowed_device_ids: allowed,
            send_limit: limitValue ? Number(limitValue) : null,
            expires_at: expiresAt
        };
        var editing = state.editingKey;
        var r = await api(editing ? ('/app/keys/' + editing.id) : '/app/keys', {
            method: editing ? 'PATCH' : 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        if (!r.ok) { alert(await errorText(r)); return; }
        var data = await r.json();
        var box = $('#key-result');
        box.style.display = '';
        box.innerHTML = '';
        if (editing) {
            box.appendChild(el('div', null, '已保存 Key 策略。'));
        } else {
            box.appendChild(el('div', null, '已生成 Key，请在弹窗中复制并保存。'));
            showGeneratedKeyModal(data.key);
        }
        closeKeyModal();
        await refreshKeys();
    });

    function connectWs() {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        var ws = new WebSocket(proto + '//' + location.host + '/ws');
        ws.onclose = function() { setTimeout(connectWs, 3000); };
        ws.onmessage = function(ev) {
            try {
                var msg = JSON.parse(ev.data);
                if (msg.event === 'sms.received') prependInbox(msg.data);
                if (msg.event === 'sms.sent') refreshMessages();
            } catch (e) {}
        };
    }

    refreshDevices();
    refreshMessages();
    refreshKeys();
    initDeviceScanModal();
    connectWs();
    setInterval(refreshDevices, 5000);
})();

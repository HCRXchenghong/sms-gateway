/* SMS Gateway dashboard logic. Vanilla JS, no build step. */
(function() {
    'use strict';

    // -------------------------- helpers --------------------------
    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }
    function el(tag, attrs, text) {
        var n = document.createElement(tag);
        if (attrs) for (var k in attrs) {
            if (k === 'class') n.className = attrs[k];
            else if (k === 'dataset') Object.assign(n.dataset, attrs[k]);
            else n.setAttribute(k, attrs[k]);
        }
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }
    function fmtTime(epoch) {
        if (!epoch) return '';
        var d = new Date(epoch * 1000);
        return d.toLocaleString();
    }
    async function api(path, opts) {
        var r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts || {}));
        if (r.status === 401) { window.location.href = '/login'; throw new Error('unauthorized'); }
        return r;
    }

    // -------------------------- tabs --------------------------
    $$('.tab').forEach(function(t) {
        t.addEventListener('click', function() {
            $$('.tab').forEach(function(x) { x.classList.remove('active'); });
            $$('.panel').forEach(function(x) { x.classList.remove('active'); });
            t.classList.add('active');
            $('#tab-' + t.dataset.tab).classList.add('active');
        });
    });

    // -------------------------- inbox --------------------------
    function renderInbox(items) {
        var tb = $('#inbox-table tbody');
        tb.innerHTML = '';
        items.forEach(function(m) {
            var row = el('tr');
            row.appendChild(el('td', null, fmtTime(m.received_at)));
            row.appendChild(el('td', null, m.sender || '(unknown)'));
            row.appendChild(el('td', null, m.content));
            tb.appendChild(row);
        });
    }
    function prependInbox(m) {
        var tb = $('#inbox-table tbody');
        var row = el('tr');
        row.appendChild(el('td', null, fmtTime(m.received_at)));
        row.appendChild(el('td', null, m.sender || '(unknown)'));
        row.appendChild(el('td', null, m.content));
        tb.insertBefore(row, tb.firstChild);
    }
    async function refreshInbox() {
        var r = await api('/app/inbox');
        var data = await r.json();
        renderInbox(data.items || []);
    }

    // -------------------------- outbox --------------------------
    function renderOutbox(items) {
        var tb = $('#outbox-table tbody');
        tb.innerHTML = '';
        items.forEach(function(m) {
            var row = el('tr');
            row.appendChild(el('td', null, fmtTime(m.sent_at || m.queued_at)));
            row.appendChild(el('td', null, m.recipient));
            var st = el('td', null, m.status);
            if (m.status === 'sent') st.style.color = 'var(--ok)';
            if (m.status === 'failed') { st.style.color = 'var(--error)'; st.title = m.error || ''; }
            row.appendChild(st);
            row.appendChild(el('td', null, String(m.segments)));
            row.appendChild(el('td', null, m.content));
            tb.appendChild(row);
        });
    }
    async function refreshOutbox() {
        var r = await api('/app/outbox');
        var data = await r.json();
        renderOutbox(data.items || []);
    }

    // -------------------------- send --------------------------
    $('#send-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        var fd = new FormData(e.target);
        var status = $('#send-status');
        status.textContent = '发送中...';
        status.style.color = '';
        try {
            var r = await api('/app/send', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ to: fd.get('to'), content: fd.get('content') })
            });
            if (!r.ok) {
                var t = await r.text();
                throw new Error(t || ('HTTP ' + r.status));
            }
            var data = await r.json();
            status.textContent = '已发送（' + data.segments + ' 段）';
            status.style.color = 'var(--ok)';
            e.target.reset();
            refreshOutbox();
        } catch (err) {
            status.textContent = '失败：' + err.message;
            status.style.color = 'var(--error)';
        }
    });

    // -------------------------- API keys --------------------------
    function renderKeys(items) {
        var tb = $('#keys-table tbody');
        tb.innerHTML = '';
        items.forEach(function(k) {
            var row = el('tr');
            row.appendChild(el('td', null, k.label));
            row.appendChild(el('td', null, k.prefix + '...'));
            row.appendChild(el('td', null, (k.scopes || []).join(' ')));
            row.appendChild(el('td', null, fmtTime(k.created_at)));
            row.appendChild(el('td', null, k.revoked_at ? '已撤销' : '有效'));
            var actions = el('td');
            if (!k.revoked_at) {
                var b = el('button', { class: 'link' }, '撤销');
                b.addEventListener('click', async function() {
                    if (!confirm('撤销 Key "' + k.label + '"？')) return;
                    await api('/app/keys/' + k.id, { method: 'DELETE' });
                    refreshKeys();
                });
                actions.appendChild(b);
            }
            row.appendChild(actions);
            tb.appendChild(row);
        });
    }
    async function refreshKeys() {
        var r = await api('/app/keys');
        var data = await r.json();
        renderKeys(data.items || []);
    }
    $('#key-create-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        var fd = new FormData(e.target);
        var scopes = [];
        if (fd.get('scope_read')) scopes.push('sms:read');
        if (fd.get('scope_send')) scopes.push('sms:send');
        if (!scopes.length) { alert('请至少选择一个权限'); return; }
        var r = await api('/app/keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label: fd.get('label'), scopes: scopes })
        });
        var data = await r.json();
        var box = $('#key-result');
        box.style.display = '';
        box.innerHTML = '';
        box.appendChild(el('div', null, '已生成 Key（仅显示一次，请立即复制）：'));
        var code = el('code', null, data.key);
        code.style.userSelect = 'all';
        box.appendChild(code);
        e.target.reset();
        refreshKeys();
    });

    // -------------------------- websocket --------------------------
    function connectWs() {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        var ws = new WebSocket(proto + '//' + location.host + '/ws');
        var pill = $('#modem-status');
        ws.onopen = function() { pill.textContent = '● 实时'; pill.className = 'status-pill ok'; };
        ws.onclose = function() {
            pill.textContent = '● 离线';
            pill.className = 'status-pill bad';
            setTimeout(connectWs, 3000);
        };
        ws.onmessage = function(ev) {
            try {
                var msg = JSON.parse(ev.data);
                if (msg.event === 'sms.received') prependInbox(msg.data);
                if (msg.event === 'sms.sent') refreshOutbox();
            } catch (e) {}
        };
    }

    // -------------------------- init --------------------------
    refreshInbox();
    refreshOutbox();
    refreshKeys();
    connectWs();
})();

/* Shared SMS conversation management for the browser and Flutter app. */
(function () {
    'use strict';
    const $ = s => document.querySelector(s);
    const selected = new Set();
    let items = [], activePeer = null, working = false, generation = 0, threadGeneration = 0;
    const root = '/app/conversations';
    const peerUrl = peer => root + '/' + encodeURIComponent(peer);
    function node(tag, text) {
        const n = document.createElement(tag);
        if (text !== undefined) n.textContent = text;
        return n;
    }
    async function api(path, method = 'GET', body) {
        const r = await fetch(path, {method, credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
        const data = await r.json();
        if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '操作失败 (' + r.status + ')');
        return data;
    }
    function status(text) { $('#sms-management-status').textContent = text; }
    function controls() {
        $('#sms-delete-selected').disabled = working || !selected.size;
        $('#sms-delete-selected').textContent = '删除所选' + (selected.size ? ' (' + selected.size + ')' : '');
        $('#sms-delete-read').disabled = working;
        $('#sms-select-all').checked = items.length > 0 && items.every(c => selected.has(c.peer_key));
        $('#sms-select-all').indeterminate = selected.size > 0 && !$('#sms-select-all').checked;
    }
    function button(label, run) {
        const b = node('button', label);
        b.type = 'button'; b.className = 'text-button'; b.disabled = working;
        b.addEventListener('click', run); return b;
    }
    function render() {
        const tbody = $('#conversations-table tbody'); tbody.replaceChildren();
        for (const c of items) {
            const row = node('tr'); row.dataset.peer = c.peer_key;
            const box = node('input'); box.type = 'checkbox'; box.checked = selected.has(c.peer_key);
            box.setAttribute('aria-label', '选择 ' + c.peer); box.disabled = working;
            box.addEventListener('change', () => { if (box.checked) selected.add(c.peer_key); else selected.delete(c.peer_key); controls(); });
            const cell = node('td'); cell.append(box); row.append(cell);
            row.append(node('td', c.name || c.peer), node('td', c.last_content), node('td', c.unread ? '未读 (' + c.unread + ')' : '已读'));
            const actions = node('td');
            actions.append(button('查看', () => open(c)), button(c.unread ? '标为已读' : '标为未读', () => mutate(async () => {
                // An explicitly unread conversation is closed to avoid marking it read on refresh.
                if (!c.unread && activePeer === c.peer_key) closeThread();
                await api(peerUrl(c.peer_key) + (c.unread ? '/read' : '/unread'), 'POST');
            })), button('删除', () => remove([c.peer_key])));
            row.append(actions); tbody.append(row);
        }
        if (!items.length) { const row = node('tr'), cell = node('td', '暂无短信会话'); cell.colSpan = 5; row.append(cell); tbody.append(row); }
        controls();
    }
    async function refresh() {
        const request = ++generation;
        try {
            const data = await api(root + '?limit=500');
            if (request !== generation) return;
            items = data.items;
            for (const peer of selected) if (!items.some(c => c.peer_key === peer)) selected.delete(peer);
            render();
        } catch (e) { status(e.message); }
    }
    function closeThread() { activePeer = null; ++threadGeneration; $('#sms-thread').hidden = true; }
    async function loadThread(markRead = false) {
        if (!activePeer) return;
        const request = ++threadGeneration;
        try {
            const data = await api(peerUrl(activePeer) + '/messages?mark_read=' + markRead);
            if (request !== threadGeneration || !activePeer) return;
            const box = $('#sms-thread-messages'); box.replaceChildren();
            for (const m of data.items) {
                const row = node('p', (m.direction === 'in' ? '收到：' : '发送：') + m.content);
                row.className = 'sms-message'; box.append(row);
            }
            if (!data.items.length) box.append(node('p', '暂无消息'));
        } catch (e) { status(e.message); }
    }
    async function open(c) {
        activePeer = c.peer_key; $('#sms-thread').hidden = false;
        $('#sms-thread-title').textContent = c.name || c.peer;
        await loadThread(true); await refresh();
    }
    async function mutate(run) {
        if (working) return;
        working = true; render(); status('处理中…');
        try { await run(); status('操作成功'); await refresh(); await loadThread();
            window.dispatchEvent(new Event('gateway-sms-refresh'));
        } catch (e) { status(e.message); }
        finally { working = false; render(); }
    }
    function remove(peers, readOnly = false) {
        if (working) return;
        if (!confirm(readOnly ? '删除所有已读会话？未读会话、草稿和正在发送的短信会保留。删除会同步到 App，无法恢复。'
            : '删除选中的 ' + peers.length + ' 个会话及全部短信？删除会同步到 App，无法恢复。')) return;
        mutate(async () => {
            await api(root + (readOnly ? '/delete_read' : '/bulk_delete'), 'POST', readOnly ? undefined : {peers});
            selected.clear();
        });
    }
    $('#sms-select-all').addEventListener('change', e => {
        selected.clear(); if (e.target.checked) items.forEach(c => selected.add(c.peer_key)); render();
    });
    $('#sms-delete-selected').addEventListener('click', () => remove([...selected]));
    $('#sms-delete-read').addEventListener('click', () => remove([], true));
    $('#sms-refresh').addEventListener('click', refresh);
    $('#sms-thread-close').addEventListener('click', closeThread);
    window.addEventListener('gateway-sms', e => {
        refresh(); loadThread(e.detail.event === 'sms.received' || e.detail.event === 'sms.sent');
    });
    // Do not consume messages as read when the operator has moved to another tab.
    document.querySelectorAll('.nav-item').forEach(b => b.addEventListener('click', closeThread));
    refresh();
})();

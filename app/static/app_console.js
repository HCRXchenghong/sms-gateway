/* Voice, audio, AI and account console. Vanilla JS, no build step.
   Loaded after app.js; the two share only the DOM, never globals. */
(function() {
    'use strict';

    function $(sel, root) { return (root || document).querySelector(sel); }
    function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

    function el(tag, attrs, text) {
        var n = document.createElement(tag);
        if (attrs) {
            for (var k in attrs) {
                if (attrs[k] === null || attrs[k] === undefined || attrs[k] === false) continue;
                if (k === 'class') n.className = attrs[k];
                else if (k === 'dataset') Object.assign(n.dataset, attrs[k]);
                else n.setAttribute(k, attrs[k]);
            }
        }
        if (text !== undefined && text !== null) n.textContent = text;
        return n;
    }

    async function api(path, opts) {
        var r = await fetch(path, Object.assign({ credentials: 'same-origin' }, opts || {}));
        if (r.status === 401) { window.location.href = '/login'; throw new Error('unauthorized'); }
        return r;
    }

    async function json(path, opts) {
        var r = await api(path, opts);
        if (!r.ok) throw new Error(await errorText(r));
        return await r.json();
    }

    async function errorText(r) {
        try {
            var data = await r.json();
            if (data.detail && data.detail.error) return data.detail.error.message || data.detail.error.code;
            if (data.detail) return typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
            return JSON.stringify(data);
        } catch (e) {
            try { return await r.text(); } catch (e2) { return r.status + ' ' + r.statusText; }
        }
    }

    function post(path, body) {
        return json(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {})
        });
    }

    function fmtTime(epoch) {
        if (!epoch) return '-';
        return new Date(epoch * 1000).toLocaleString();
    }

    function fmtDuration(seconds) {
        seconds = Math.max(0, seconds || 0);
        var m = Math.floor(seconds / 60);
        var s = seconds % 60;
        return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
    }

    function note(selector, message, isError) {
        var box = $(selector);
        if (!box) return;
        if (!message) { box.style.display = 'none'; box.textContent = ''; return; }
        box.style.display = '';
        box.className = 'alert ' + (isError ? 'error' : 'info');
        box.textContent = message;
        if (!isError) {
            setTimeout(function() {
                if (box.textContent === message) box.style.display = 'none';
            }, 4000);
        }
    }

    var STATE_LABEL = {
        idle: '空闲', dialing: '正在拨号', alerting: '对方振铃', incoming: '来电振铃',
        active: '通话中', held: '已保持', ended: '已结束'
    };
    var REASON_LABEL = {
        local_hangup: '本机挂断', remote_hangup: '对方挂断', rejected: '已拒接',
        busy: '对方忙', no_answer: '无应答', no_carrier: '未接通',
        no_dialtone: '无拨号音', timeout: '超时', error: '失败', unknown: '未知'
    };

    // Why a capture that should have run did not. Reporting this matters more
    // than hiding it: a forced policy that captured nothing is an error.
    var RECORD_FAILURE = {
        audio_capture_disabled: '服务器声卡采集已关闭',
        no_input_device: '服务器没有可用的录音输入设备',
        empty_file: '录音文件为空，已丢弃',
        record_dir_unwritable: '录音目录不可写，请检查 /var/lib/sms-gateway/recordings 的权限'
    };

    var state = { devices: [], calls: [], audio: null, prompts: [], isAdmin: false, voiceState: null };

    // ---------------------------------------------------------------- devices

    function deviceName(d) {
        if (!d) return '未绑定';
        return d.display_name || ('编号' + d.code);
    }

    async function refreshDevices() {
        try {
            var data = await json('/app/devices');
            state.devices = data.items || [];
        } catch (e) {
            return;
        }
        fillDeviceSelect('#dial-device', state.devices, true);
        fillDeviceSelect('#call-device-filter', state.devices, true);
        fillDeviceSelect('#ai-call-device', state.devices, false);
    }

    function fillDeviceSelect(sel, devices, withAll) {
        var box = $(sel);
        if (!box) return;
        var previous = box.value;
        box.innerHTML = '';
        if (withAll) {
            box.appendChild(el('option', { value: '' }, '自动选择在线设备'));
        }
        devices.forEach(function(d) {
            var label = deviceName(d) + (d.sim_number ? '  ' + d.sim_number : '') + (d.status === 'online' ? '' : '（离线）');
            box.appendChild(el('option', { value: String(d.id) }, label));
        });
        if (previous && $$('#' === sel ? '' : sel + ' option[value="' + previous + '"]').length) {
            box.value = previous;
        }
    }

    function pickDeviceId(sel) {
        var box = $(sel);
        if (!box || !box.value) return null;
        return parseInt(box.value, 10);
    }

    // ---------------------------------------------------------------- dialler

    function initDialpad() {
        var input = $('#dial-number');
        var pad = $('#dialpad');
        if (!input || !pad) return;

        pad.addEventListener('click', function(e) {
            var key = e.target.closest('.dialpad-key');
            if (!key) return;
            input.value = (input.value + key.dataset.key).slice(0, 24);
        });

        $('#dial-backspace').addEventListener('click', function() {
            input.value = input.value.slice(0, -1);
        });

        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') { e.preventDefault(); startCall(); }
        });

        $('#dial-call').addEventListener('click', startCall);
    }

    async function startCall() {
        var number = ($('#dial-number').value || '').trim();
        if (number.length < 2) { note('#dial-status', '请先输入要拨打的号码', true); return; }
        var deviceId = pickDeviceId('#dial-device');
        note('#dial-status', '正在下发拨号指令…');
        try {
            await post('/app/voice/dial', { number: number, device_id: deviceId });
            note('#dial-status', '已拨出 ' + number + '，正在接通，请稍候');
            await refreshCalls();
        } catch (e) {
            note('#dial-status', '拨号失败：' + e.message, true);
        }
    }

    // ------------------------------------------------------------ active call

    async function refreshCalls() {
        try {
            state.voiceState = await json('/app/voice/state');
        } catch (e) {
            return;
        }
        state.calls = state.voiceState.active || [];
        renderActiveCalls();
        var pill = $('#call-concurrency');
        if (pill) {
            pill.textContent = state.calls.length + ' / ' + (state.voiceState.max_concurrent || 0) + ' 路并发';
        }
    }

    function renderActiveCalls() {
        var box = $('#active-calls');
        if (!box) return;
        box.innerHTML = '';
        box.className = '';
        if (!state.calls.length) {
            box.className = 'empty-state';
            box.textContent = '当前没有通话';
            return;
        }
        state.calls.forEach(function(c) {
            box.appendChild(renderCallCard(c));
        });
    }

    function renderCallCard(c) {
        var live = c.state !== 'ended';
        var card = el('div', { class: 'call-card' });
        var head = el('div', { class: 'call-card-head' });
        head.appendChild(el('strong', null, c.remote || '未知号码'));
        head.appendChild(el('span', { class: 'chip ' + (c.state === 'active' ? 'ok' : (live ? 'warn' : 'bad')) },
            STATE_LABEL[c.state] || c.state));
        var policy = recordingPolicy();
        if (c.recording) {
            head.appendChild(el('span', { class: 'chip bad' }, '● 录音中'));
        } else if (policy === 'disabled') {
            head.appendChild(el('span', { class: 'chip' }, '不录音'));
        } else if (policy === 'forced') {
            head.appendChild(el('span', { class: 'chip warn' }, '强制录音未生效'));
        }
        card.appendChild(head);

        var meta = el('div', { class: 'call-card-meta' });
        meta.textContent = (c.direction === 'inbound' ? '来电' : '去电') +
            ' · ' + deviceName(findDevice(c.device_id)) +
            (c.answered_at ? ' · 已通话 ' + fmtDuration(Math.floor(Date.now() / 1000) - c.answered_at) : '') +
            (c.end_reason ? ' · ' + (REASON_LABEL[c.end_reason] || c.end_reason) : '');
        card.appendChild(meta);

        var capture = c.recording_detail || recordingFor(c.call_id);
        if (capture && capture.notice) {
            var told = el('div', { class: 'call-card-meta' });
            told.textContent = capture.notice.ok
                ? '已告知对方录音：「' + capture.notice.text + '」'
                : '录音提示未成功播放：' + (capture.notice.error || '未知原因');
            card.appendChild(told);
        } else if (capture && capture.failed_reason) {
            var failed = el('div', { class: 'call-card-meta' });
            failed.textContent = '录音未能开始：' + RECORD_FAILURE[capture.failed_reason];
            card.appendChild(failed);
        }

        if ((c.tts_log || []).length) {
            var logBox = el('div', { class: 'call-tts-log' });
            c.tts_log.slice(-3).forEach(function(t) {
                logBox.appendChild(el('div', null, '播报：' + (t.text || '')));
            });
            card.appendChild(logBox);
        }

        var actions = el('div', { class: 'call-actions' });
        if (c.state === 'incoming') {
            actions.appendChild(callButton('接听', 'primary', function() { callAction(c.device_id, 'answer'); }));
            actions.appendChild(callButton('拒接', '', function() { callAction(c.device_id, 'hangup'); }));
        } else {
            actions.appendChild(callButton('挂断', 'danger', function() { callAction(c.device_id, 'hangup'); }));
        }
        if (c.state === 'active' || c.state === 'held') {
            actions.appendChild(callButton('按键', '', function() { openDtmf(c); }));
            actions.appendChild(callButton('TTS 播报', '', function() { openTts(c); }));
            actions.appendChild(callButton('停止播报', '', function() { callAction(c.device_id, 'tts/stop'); }));
            if (policy === 'optional') {
                if (c.recording) {
                    actions.appendChild(callButton('停止录音', '', function() { setRecording(c.call_id, false); }));
                    actions.appendChild(callButton('播报录音提示', '', function() { announceRecording(c.call_id); }));
                } else {
                    actions.appendChild(callButton('开始录音', '', function() { setRecording(c.call_id, true, false); }));
                    actions.appendChild(callButton('录音并告知对方', '', function() { setRecording(c.call_id, true, true); }));
                }
            }
        }
        card.appendChild(actions);
        return card;
    }

    // ------------------------------------------------------------- recording

    function recordingPolicy() {
        return (state.voiceState && state.voiceState.recording_policy) || 'optional';
    }

    function recordingFor(callId) {
        var list = (state.voiceState && state.voiceState.recording) || [];
        for (var i = 0; i < list.length; i++) {
            if (list[i].call_id === callId) return list[i];
        }
        return null;
    }

    async function setRecording(callId, enabled, announce) {
        var body = { enabled: !!enabled };
        if (announce !== undefined && announce !== null) body.announce = !!announce;
        try {
            await post('/app/voice/calls/' + encodeURIComponent(callId) + '/recording', body);
            note('#dial-status', enabled ? '已开始录音' : '已停止录音，文件已保存');
        } catch (e) {
            note('#dial-status', '录音操作失败：' + e.message, true);
        }
        await refreshCalls();
    }

    async function announceRecording(callId) {
        try {
            var data = await post('/app/voice/calls/' + encodeURIComponent(callId) + '/recording/announce', {});
            note('#dial-status', data && data.ok ? '已向对方播报录音提示' : '提示播放失败');
        } catch (e) {
            note('#dial-status', '播报录音提示失败：' + e.message, true);
        }
        await refreshCalls();
    }

    function callButton(label, cls, handler) {
        var b = el('button', { type: 'button', class: 'button ' + cls }, label);
        b.addEventListener('click', handler);
        return b;
    }

    function findDevice(id) {
        for (var i = 0; i < state.devices.length; i++) {
            if (state.devices[i].id === id) return state.devices[i];
        }
        return null;
    }

    async function callAction(deviceId, action, body) {
        try {
            await post('/app/voice/' + deviceId + '/' + action, body);
            note('#dial-status', '指令已下发');
        } catch (e) {
            note('#dial-status', '操作失败：' + e.message, true);
        }
        await refreshCalls();
    }

    function openDtmf(c) {
        var digits = prompt('输入要发送的 DTMF 按键（0-9 * #）', '1');
        if (!digits) return;
        callAction(c.device_id, 'dtmf', { digits: digits.replace(/[^0-9*#]/g, '').slice(0, 32) });
    }

    function openTts(c) {
        var text = prompt('输入要在通话中播报的文字（通过模块 AT+CTTS 合成）', '您好，这里是自动语音通知。');
        if (!text) return;
        callAction(c.device_id, 'tts', { text: text.slice(0, 512) });
    }

    // ------------------------------------------------------------- mock hooks

    function initMock() {
        var callBtn = $('#mock-incoming-call');
        var smsBtn = $('#mock-incoming-sms');
        var scenarioBtn = $('#mock-set-scenario');
        if (!callBtn || !smsBtn || !scenarioBtn) return;

        function deviceId() {
            var picked = pickDeviceId('#dial-device');
            if (picked) return picked;
            var online = state.devices.filter(function(d) { return d.status === 'online'; });
            return online.length ? online[0].id : null;
        }

        callBtn.addEventListener('click', async function() {
            var id = deviceId();
            if (!id) { note('#dial-status', '没有可用设备', true); return; }
            try {
                await post('/app/voice/mock/' + id + '/incoming', { number: ($('#mock-caller').value || '').trim() });
                note('#dial-status', '已模拟来电');
                await refreshCalls();
            } catch (e) { note('#dial-status', e.message, true); }
        });

        smsBtn.addEventListener('click', async function() {
            var id = deviceId();
            if (!id) { note('#dial-status', '没有可用设备', true); return; }
            var content = prompt('模拟来信内容', '这是一条来自网关的模拟短信');
            if (!content) return;
            try {
                await post('/app/sms/mock/' + id + '/incoming', { sender: ($('#mock-caller').value || '').trim(), content: content });
                note('#dial-status', '已模拟来信');
            } catch (e) { note('#dial-status', e.message, true); }
        });

        scenarioBtn.addEventListener('click', async function() {
            var id = deviceId();
            if (!id) { note('#dial-status', '没有可用设备', true); return; }
            try {
                await post('/app/voice/mock/' + id + '/scenario', { scenario: $('#mock-scenario').value });
                note('#dial-status', '下次拨号结果已设为 ' + $('#mock-scenario').value);
            } catch (e) { note('#dial-status', e.message, true); }
        });
    }

    // ---------------------------------------------------------- call history

    async function refreshCallStats() {
        var box = $('#call-stats');
        if (!box) return;
        try {
            var s = await json('/app/voice/stats');
            box.innerHTML = '';
            [
                ['通话总数', s.total], ['去电', s.outbound], ['来电', s.inbound],
                ['已接通', s.answered], ['接通率', (s.answer_rate || 0).toFixed(1) + '%'],
                ['通话时长', fmtDuration(s.talk_seconds)], ['统计天数', s.days + ' 天']
            ].forEach(function(pair) {
                var cell = el('div', { class: 'stat-cell' });
                cell.appendChild(el('span', { class: 'stat-value' }, String(pair[1])));
                cell.appendChild(el('span', { class: 'stat-label' }, pair[0]));
                box.appendChild(cell);
            });
        } catch (e) {
            box.textContent = '读取统计失败：' + e.message;
        }
    }

    async function refreshCallHistory() {
        var tbody = $('#calls-table tbody');
        if (!tbody) return;
        var params = new URLSearchParams({ limit: '100' });
        var direction = $('#call-direction-filter');
        var device = $('#call-device-filter');
        if (direction && direction.value) params.set('direction', direction.value);
        if (device && device.value) params.set('device_id', device.value);
        try {
            var data = await json('/app/voice/history?' + params.toString());
            tbody.innerHTML = '';
            (data.items || []).forEach(function(c) {
                var tr = el('tr');
                tr.appendChild(el('td', null, fmtTime(c.created_at)));
                tr.appendChild(el('td', null, c.device ? deviceName(c.device) : '-'));
                tr.appendChild(el('td', null, c.direction === 'inbound' ? '来电' : '去电'));
                tr.appendChild(el('td', null, c.remote || '-'));
                tr.appendChild(el('td', null, STATE_LABEL[c.state] || c.state || '-'));
                tr.appendChild(el('td', null, REASON_LABEL[c.end_reason] || c.end_reason || '-'));
                tr.appendChild(el('td', null, fmtDuration(c.duration)));
                var recCell = el('td');
                if (c.recording_path) {
                    var url = '/app/voice/calls/' + encodeURIComponent(c.call_id) + '/recording';
                    recCell.appendChild(el('audio', {
                        controls: 'controls',
                        preload: 'none',
                        src: url,
                        style: 'height:30px;width:220px;vertical-align:middle'
                    }));
                    var dl = el('a', { href: url, download: c.call_id + '.wav' }, '下载');
                    dl.className = 'linklike';
                    dl.style.marginLeft = '8px';
                    recCell.appendChild(dl);
                    // Whether the far end was told is part of the record, so it
                    // travels with the audio instead of living in a settings page.
                    if (c.recording_notice) {
                        recCell.appendChild(el('div', { class: 'hint' },
                            (c.recording_notice_ok ? '已告知对方：' : '提示未成功播放：') +
                            '「' + c.recording_notice + '」' +
                            (c.recording_notice_error ? ' · ' + c.recording_notice_error : '')));
                    } else {
                        recCell.appendChild(el('div', { class: 'hint' }, '静默录音，未告知对方'));
                    }
                } else {
                    recCell.textContent = c.recording_reason === 'policy_disabled' ? '策略关闭' : '-';
                }
                tr.appendChild(recCell);
                tbody.appendChild(tr);
            });
            if (!tbody.children.length) {
                tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '8', class: 'empty-state' }, '还没有通话记录'));
            }
        } catch (e) {
            tbody.innerHTML = '';
            tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '8' }, '读取失败：' + e.message));
        }
    }

    function initCallsTab() {
        var refresh = $('#refresh-call-stats');
        if (refresh) refresh.addEventListener('click', function() { refreshCallStats(); refreshCallHistory(); });
        var d = $('#call-direction-filter');
        if (d) d.addEventListener('change', refreshCallHistory);
        var dev = $('#call-device-filter');
        if (dev) dev.addEventListener('change', refreshCallHistory);
    }

    // ----------------------------------------------------------------- audio

    async function refreshAudio() {
        try {
            state.audio = await json('/app/audio/devices');
        } catch (e) {
            note('#audio-status', '读取声卡失败：' + e.message, true);
            return;
        }
        var a = state.audio;
        var backend = $('#audio-backend');
        if (backend) { backend.textContent = (a.backend || '-').toUpperCase(); }

        fillAudioSelect('#audio-output', a.outputs || [], a.output_device, '输出');
        fillAudioSelect('#audio-input', a.inputs || [], a.input_device, '输入');

        var slider = $('#audio-volume');
        if (slider) {
            slider.value = String(Math.round((a.volume || 0) * 100));
            var label = $('#audio-volume-label');
            if (label) label.textContent = slider.value + '%';
        }
        refreshPrompts();
    }

    function fillAudioSelect(sel, devices, selected, kindLabel) {
        var box = $(sel);
        if (!box) return;
        box.innerHTML = '';
        box.appendChild(el('option', { value: '' }, '系统默认' + kindLabel + '设备'));
        devices.forEach(function(d) {
            var opt = el('option', { value: d.id }, d.name + '  [' + d.id + ']' + (d.is_default ? '（默认）' : ''));
            box.appendChild(opt);
        });
        box.value = selected || '';
        if (box.value !== (selected || '')) box.value = '';
    }

    async function refreshPrompts() {
        var tbody = $('#prompts-table tbody');
        if (!tbody) return;
        try {
            var data = await json('/app/audio/prompts');
            state.prompts = data.items || [];
        } catch (e) {
            return;
        }
        tbody.innerHTML = '';
        state.prompts.forEach(function(p) {
            var tr = el('tr');
            tr.appendChild(el('td', null, p.id));
            tr.appendChild(el('td', null, p.text || '（纯音调，无文案）'));
            tr.appendChild(el('td', null, (p.tone || '-') + ' · ' + (p.category || '-')));
            var actions = el('td');
            var play = el('button', { type: 'button', class: 'button' }, '试听');
            play.addEventListener('click', function() { playPrompt(p.id); });
            actions.appendChild(play);
            if (state.isAdmin) {
                var render = el('button', { type: 'button', class: 'button' }, 'AI 合成');
                render.addEventListener('click', function() { renderPrompt(p.id); });
                actions.appendChild(render);
            }
            tr.appendChild(actions);
            tbody.appendChild(tr);
        });
        if (!tbody.children.length) {
            tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '4', class: 'empty-state' }, '没有可用提示音'));
        }
    }

    async function playPrompt(id) {
        try {
            await post('/app/audio/prompts/' + encodeURIComponent(id) + '/play', {});
            note('#audio-status', '已在服务器输出声卡播放 ' + id);
        } catch (e) {
            note('#audio-status', '播放失败：' + e.message, true);
        }
    }

    async function renderPrompt(id) {
        if (!confirm('用 AI 语音合成替换提示音「' + id + '」？原来的音调会保留为备用。')) return;
        note('#audio-status', '正在合成…');
        try {
            var data = await post('/app/ai/tts/prompt/' + encodeURIComponent(id), {});
            note('#audio-status', '已合成 ' + (data.bytes || 0) + ' 字节，写入 ' + (data.path || ''));
            refreshPrompts();
        } catch (e) {
            note('#audio-status', '合成失败：' + e.message, true);
        }
    }

    function initAudioTab() {
        var refresh = $('#refresh-audio');
        if (refresh) refresh.addEventListener('click', refreshAudio);

        var out = $('#audio-output');
        if (out) out.addEventListener('change', async function() {
            try {
                await post('/app/audio/output', { device: out.value });
                note('#audio-status', '输出声卡已切换');
                refreshAudio();
            } catch (e) { note('#audio-status', '切换失败：' + e.message, true); }
        });

        var inp = $('#audio-input');
        if (inp) inp.addEventListener('change', async function() {
            try {
                await post('/app/audio/input', { device: inp.value });
                note('#audio-status', '输入声卡已切换');
                refreshAudio();
            } catch (e) { note('#audio-status', '切换失败：' + e.message, true); }
        });

        var slider = $('#audio-volume');
        if (slider) {
            var label = $('#audio-volume-label');
            slider.addEventListener('input', function() { if (label) label.textContent = slider.value + '%'; });
            slider.addEventListener('change', async function() {
                try {
                    await post('/app/audio/volume', { volume: parseInt(slider.value, 10) / 100 });
                    note('#audio-status', '音量已设为 ' + slider.value + '%');
                } catch (e) { note('#audio-status', '设置失败：' + e.message, true); }
            });
        }
    }

    // ------------------------------------------------------------- websocket

    function connectConsoleWs() {
        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        var ws;
        try {
            ws = new WebSocket(proto + '//' + location.host + '/ws');
        } catch (e) {
            setTimeout(connectConsoleWs, 5000);
            return;
        }
        ws.onclose = function() { setTimeout(connectConsoleWs, 3000); };
        ws.onerror = function() { try { ws.close(); } catch (e) {} };
        ws.onmessage = function(ev) {
            var msg;
            try { msg = JSON.parse(ev.data); } catch (e) { return; }
            var name = msg.event || '';
            var data = msg.data || {};
            if (name.indexOf('call.') === 0) {
                refreshCalls();
                if (name === 'call.ended') { refreshCallHistory(); refreshCallStats(); }
                // A finished capture writes the file path onto the CDR row, so
                // the log has to be re-read before the play control appears.
                if (name === 'call.recording' && (data.phase === 'stopped' || data.phase === 'empty')) {
                    refreshCallHistory();
                }
                if (name === 'call.prompt' && data.prompt) {
                    var text = promptText(data.prompt);
                    if (text) note('#dial-status', text);
                }
            }
            if (name === 'call.transcript' || name === 'call.translation') appendFlowLine(name, data);
            if (name.indexOf('call.flow') === 0 || name === 'call.summary') refreshFlowPanel();
            if (name.indexOf('ai_call.') === 0) refreshTasks();
            if (name === 'sms.received' || name === 'sms.sent') {
                // app.js already refreshes the message tables; nothing to do.
            }
        };
    }

    function promptText(id) {
        for (var i = 0; i < state.prompts.length; i++) {
            if (state.prompts[i].id === id) return state.prompts[i].text;
        }
        return null;
    }

    // -------------------------------------------------------------------- AI

    var AI_LABELS = {
        'ai.enabled': '启用 AI 能力',
        'ai.provider': '供应商策略',
        'ai.auto_answer_call': '来电自动接听并 AI 应答',
        'ai.auto_reply_sms': '收到短信自动回复',
        'ai.chat.base_url': '对话接口地址',
        'ai.chat.model': '对话模型',
        'ai.chat.api_key': '对话 API Key',
        'ai.chat.system_prompt': '对话系统提示词',
        'ai.asr.base_url': 'ASR 接口地址',
        'ai.asr.model': 'ASR 模型',
        'ai.asr.api_key': 'ASR API Key',
        'ai.tts.engine': 'TTS 合成方式',
        'ai.tts.base_url': 'TTS 接口地址',
        'ai.tts.model': 'TTS 模型',
        'ai.tts.voice': 'TTS 音色',
        'ai.tts.format': 'TTS 音频格式',
        'ai.tts.api_key': 'TTS API Key'
    };
    var SECRET_KEYS = ['ai.chat.api_key', 'ai.asr.api_key', 'ai.tts.api_key'];
    // Display order for the AI form. The server splits editable settings from
    // write-only secrets, so the two lists are merged here to keep each
    // channel's fields together.
    var AI_ORDER = [
        'ai.enabled', 'ai.provider', 'ai.auto_answer_call', 'ai.auto_reply_sms',
        'ai.chat.base_url', 'ai.chat.model', 'ai.chat.api_key', 'ai.chat.system_prompt',
        'ai.asr.base_url', 'ai.asr.model', 'ai.asr.api_key',
        'ai.tts.engine', 'ai.tts.base_url', 'ai.tts.model', 'ai.tts.voice',
        'ai.tts.format', 'ai.tts.api_key'
    ];
    var AI_HINTS = {
        'ai.provider': 'auto 会依次尝试已配置的供应商，mock 用于离线联调',
        'ai.tts.engine': 'module = 模块内置 AT+CTTS（免 Key，零延迟）；audio = 云端合成后走声卡播放',
        'ai.chat.system_prompt': '语音场景建议要求模型回答简短、口语化，方便合成播报'
    };

    var aiForm = { values: {}, editable: [], controllers: {} };

    async function refreshAi() {
        try {
            var status = await json('/app/ai/status');
            var chip = $('#ai-provider');
            if (chip) {
                chip.textContent = status.provider || '-';
                chip.className = 'chip ' + (status.loaded ? 'ok' : 'bad');
            }
            var engine = $('#ai-tts-engine');
            if (engine) engine.textContent = status.tts_engine || '-';
        } catch (e) {
            note('#ai-status', '读取 AI 状态失败：' + e.message, true);
        }
        try {
            var health = await json('/app/ai/health');
            var box = $('#ai-health');
            if (box) {
                box.style.display = '';
                box.className = 'alert info';
                box.textContent = Object.keys(health).map(function(k) {
                    var v = health[k];
                    var okFlag = v && v.ok !== false;
                    return k + ': ' + (okFlag ? '正常' : (v && v.error ? v.error : '不可用'));
                }).join('　|　');
            }
        } catch (e) { /* health is informational only */ }
        await refreshAiSettings();
    }

    async function refreshAiSettings() {
        var form = $('#ai-settings-form');
        if (!form) return;
        var data;
        try {
            data = await json('/app/admin/settings');
        } catch (e) {
            form.innerHTML = '';
            form.appendChild(el('p', { class: 'hint' }, '需要管理员权限才能编辑 AI 配置：' + e.message));
            return;
        }
        aiForm.values = data.settings || {};
        aiForm.editable = data.editable || [];
        aiForm.controllers = {};
        form.innerHTML = '';

        var secrets = data.secrets || [];
        var allowed = aiForm.editable.concat(secrets).filter(function(k) { return k.indexOf('ai.') === 0; });
        // Known keys in curated order first, then anything the server adds later.
        var ordered = AI_ORDER.filter(function(k) { return allowed.indexOf(k) >= 0; });
        allowed.forEach(function(k) { if (ordered.indexOf(k) < 0) ordered.push(k); });

        ordered.forEach(function(key) {
            var value = aiForm.values[key];
            var wrap = el('label', null, AI_LABELS[key] || key);

            if (typeof value === 'boolean') {
                var cb = el('input', { type: 'checkbox' });
                cb.checked = !!value;
                cb.addEventListener('change', function() { aiForm.controllers[key] = cb.checked; });
                aiForm.controllers[key] = null;
                wrap.appendChild(cb);
                wrap.className = 'check';
            } else if (SECRET_KEYS.indexOf(key) >= 0) {
                var secret = el('input', { type: 'password', placeholder: value ? '已配置（留空表示不修改）' : '未配置', autocomplete: 'new-password' });
                secret.addEventListener('input', function() { aiForm.controllers[key] = secret.value; });
                wrap.appendChild(secret);
            } else if (key === 'ai.chat.system_prompt') {
                var area = el('textarea', { rows: '4' });
                area.value = value === null || value === undefined ? '' : String(value);
                area.addEventListener('input', function() { aiForm.controllers[key] = area.value; });
                wrap.appendChild(area);
            } else if (key === 'ai.provider') {
                var sel = el('select');
                ['auto', 'mock', 'openai', 'custom'].forEach(function(v) {
                    sel.appendChild(el('option', { value: v }, v));
                });
                sel.value = String(value || 'auto');
                if (!sel.value) sel.value = 'auto';
                sel.addEventListener('change', function() { aiForm.controllers[key] = sel.value; });
                wrap.appendChild(sel);
            } else if (key === 'ai.tts.engine') {
                var engineSel = el('select');
                engineSel.appendChild(el('option', { value: 'module' }, 'module（模块内置 AT+CTTS）'));
                engineSel.appendChild(el('option', { value: 'audio' }, 'audio（云端 TTS 走声卡）'));
                engineSel.value = String(value || 'module');
                engineSel.addEventListener('change', function() { aiForm.controllers[key] = engineSel.value; });
                wrap.appendChild(engineSel);
            } else {
                var input = el('input', { type: 'text' });
                input.value = value === null || value === undefined ? '' : String(value);
                input.addEventListener('input', function() { aiForm.controllers[key] = input.value; });
                wrap.appendChild(input);
            }

            form.appendChild(wrap);
            if (AI_HINTS[key]) form.appendChild(el('p', { class: 'hint' }, AI_HINTS[key]));
        });

        if (!form.children.length) {
            form.appendChild(el('p', { class: 'hint' }, '没有可编辑的 AI 配置项。'));
        }
    }

    function collectAiPatch() {
        var patch = {};
        Object.keys(aiForm.controllers).forEach(function(key) {
            var value = aiForm.controllers[key];
            if (value === null || value === undefined) return;
            if (SECRET_KEYS.indexOf(key) >= 0 && String(value).trim() === '') return;
            if (typeof value === 'string' && value === String(aiForm.values[key] === undefined ? '' : aiForm.values[key])) return;
            patch[key] = value;
        });
        return patch;
    }

    function initAiTab() {
        var refresh = $('#refresh-ai');
        if (refresh) refresh.addEventListener('click', refreshAi);

        var save = $('#save-ai-settings');
        if (save) save.addEventListener('click', async function(e) {
            e.preventDefault();
            var patch = collectAiPatch();
            if (!Object.keys(patch).length) { note('#ai-status', '没有需要保存的改动'); return; }
            note('#ai-status', '保存中…');
            try {
                var r = await api('/app/admin/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: patch })
                });
                if (!r.ok) throw new Error(await errorText(r));
                var data = await r.json();
                note('#ai-status', '已保存并热加载：' + Object.keys(data.applied || {}).join('、'));
                await refreshAi();
            } catch (err) {
                note('#ai-status', '保存失败：' + err.message, true);
            }
        });

        var chatBtn = $('#ai-chat-send');
        if (chatBtn) chatBtn.addEventListener('click', async function() {
            var message = ($('#ai-chat-input').value || '').trim();
            if (!message) return;
            var statusBox = $('#ai-chat-status');
            statusBox.textContent = '请求中…';
            var box = $('#ai-chat-result');
            box.style.display = 'none';
            try {
                var data = await post('/app/ai/chat', { message: message, history: [] });
                statusBox.textContent = '供应商 ' + (data.provider || '-') + '，耗时 ' + (data.elapsed_ms || 0) + ' ms';
                box.style.display = '';
                box.className = 'alert info';
                box.textContent = data.reply || '（空回复）';
            } catch (e) {
                statusBox.textContent = '';
                box.style.display = '';
                box.className = 'alert error';
                box.textContent = '对话失败：' + e.message;
            }
        });

        var ttsBtn = $('#ai-tts-run');
        if (ttsBtn) ttsBtn.addEventListener('click', async function() {
            var text = ($('#ai-tts-input').value || '').trim();
            if (!text) return;
            var audio = $('#ai-tts-audio');
            try {
                var r = await api('/app/ai/tts', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text: text })
                });
                if (!r.ok) throw new Error(await errorText(r));
                var blob = await r.blob();
                if (audio.src) URL.revokeObjectURL(audio.src);
                audio.src = URL.createObjectURL(blob);
                audio.style.display = '';
                audio.play();
                note('#ai-status', '已合成 ' + blob.size + ' 字节音频');
            } catch (e) {
                note('#ai-status', 'TTS 失败：' + e.message, true);
            }
        });

        var toPromptBtn = $('#ai-tts-to-prompt');
        if (toPromptBtn) toPromptBtn.addEventListener('click', async function() {
            var candidates = state.prompts.filter(function(p) { return p.text; });
            if (!candidates.length) { note('#ai-status', '请先在「声卡与提示音」页加载提示音列表', true); return; }
            var id = prompt('要把哪个提示音替换为 AI 语音？\n可选：' + candidates.map(function(p) { return p.id; }).join(', '), candidates[0].id);
            if (!id) return;
            renderPrompt(id);
        });

        var speakBtn = $('#ai-call-speak');
        if (speakBtn) speakBtn.addEventListener('click', async function() {
            var deviceId = pickDeviceId('#ai-call-device');
            var text = ($('#ai-call-tts-input').value || '').trim();
            if (!deviceId) { note('#ai-status', '请选择设备', true); return; }
            if (!text) { note('#ai-status', '请输入内容', true); return; }
            note('#ai-status', '正在生成并播报…');
            try {
                var data = await post('/app/voice/' + deviceId + '/ai/speak', { text: text, engine: 'module' });
                note('#ai-status', '已通过 ' + (data.channel || '-') + ' 播报：' + (data.reply || ''));
            } catch (e) {
                note('#ai-status', '播报失败：' + e.message + '（该设备当前可能没有通话）', true);
            }
        });
    }

    // --------------------------------------------------------------- accounts

    async function refreshUsers() {
        var tbody = $('#users-table tbody');
        if (!tbody) return;
        var data;
        try {
            data = await json('/app/admin/users');
        } catch (e) {
            tbody.innerHTML = '';
            tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '7' }, '读取失败：' + e.message));
            return;
        }
        tbody.innerHTML = '';
        (data.items || []).forEach(function(u) {
            var tr = el('tr');
            tr.appendChild(el('td', null, u.username));
            tr.appendChild(el('td', null, u.display_name || '-'));
            tr.appendChild(el('td', null, u.role === 'admin' ? '管理员' : '坐席'));
            tr.appendChild(el('td', null, u.totp_enabled ? '已开启' : '未开启'));
            tr.appendChild(el('td', null, fmtTime(u.last_login_at)));
            var statusCell = el('td');
            statusCell.appendChild(el('span', { class: 'chip ' + (u.is_active ? 'ok' : 'bad') }, u.is_active ? '启用' : '停用'));
            if (u.must_change_password) statusCell.appendChild(el('span', { class: 'chip warn' }, '需改密'));
            tr.appendChild(statusCell);

            var actions = el('td', { class: 'row-actions' });
            actions.appendChild(userButton('角色', function() { toggleRole(u); }));
            actions.appendChild(userButton('改密', function() { openPasswordModal(u); }));
            actions.appendChild(userButton(u.is_active ? '停用' : '启用', function() { toggleActive(u); }));
            if (u.totp_enabled) actions.appendChild(userButton('清除双因子', function() { resetTotp(u); }));
            actions.appendChild(userButton('删除', function() { removeUser(u); }, 'danger'));
            tr.appendChild(actions);
            tbody.appendChild(tr);
        });
    }

    function userButton(label, handler, cls) {
        var b = el('button', { type: 'button', class: 'button ' + (cls || '') }, label);
        b.addEventListener('click', handler);
        return b;
    }

    async function toggleRole(u) {
        var next = u.role === 'admin' ? 'operator' : 'admin';
        if (!confirm('把「' + u.username + '」的角色改为' + (next === 'admin' ? '管理员' : '坐席') + '？')) return;
        await patchUser(u.id, { role: next }, '角色已更新');
    }

    async function toggleActive(u) {
        await patchUser(u.id, { is_active: !u.is_active }, u.is_active ? '已停用' : '已启用');
    }

    async function resetTotp(u) {
        if (!confirm('清除「' + u.username + '」的双因子设置？')) return;
        try {
            await post('/app/admin/users/' + u.id + '/totp/reset', {});
            note('#account-status', '已清除双因子，已关闭，可登录后台自行开启');
            refreshUsers();
        } catch (e) { note('#account-status', e.message, true); }
    }

    async function removeUser(u) {
        if (!confirm('删除账号「' + u.username + '」？该操作不可撤销。')) return;
        try {
            var r = await api('/app/admin/users/' + u.id, { method: 'DELETE' });
            if (!r.ok) throw new Error(await errorText(r));
            note('#account-status', '账号已删除');
            refreshUsers();
            refreshAudit();
        } catch (e) { note('#account-status', '删除失败：' + e.message, true); }
    }

    async function patchUser(id, body, okMessage) {
        try {
            var r = await api('/app/admin/users/' + id, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            if (!r.ok) throw new Error(await errorText(r));
            note('#account-status', okMessage);
            refreshUsers();
            refreshAudit();
        } catch (e) { note('#account-status', '操作失败：' + e.message, true); }
    }

    var passwordTarget = null;

    function openPasswordModal(u) {
        passwordTarget = u;
        $('#password-modal-subtitle').textContent = '为「' + u.username + '」设置新密码，用户下次登录生效。';
        $('#password-reset-form').reset();
        $('#password-modal').hidden = false;
    }

    function closePasswordModal() {
        passwordTarget = null;
        $('#password-modal').hidden = true;
    }

    function initAccountsTab() {
        if (!$('#users-table')) return;
        state.isAdmin = true;

        var modal = $('#user-modal');
        var openBtn = $('#open-user-modal');
        if (openBtn) openBtn.addEventListener('click', function() {
            $('#user-create-form').reset();
            note('#user-form-error', '');
            modal.hidden = false;
        });
        $('#close-user-modal').addEventListener('click', function() { modal.hidden = true; });
        $('#user-cancel').addEventListener('click', function() { modal.hidden = true; });
        modal.addEventListener('click', function(e) { if (e.target === modal) modal.hidden = true; });

        $('#user-create-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            var f = e.target;
            var body = {
                username: f.elements.username.value.trim(),
                password: f.elements.password.value,
                role: f.elements.role.value
            };
            var display = f.elements.display_name.value.trim();
            if (display) body.display_name = display;
            try {
                var r = await api('/app/admin/users', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (!r.ok) throw new Error(await errorText(r));
                modal.hidden = true;
                note('#account-status', '账号「' + body.username + '」已创建');
                refreshUsers();
                refreshAudit();
            } catch (err) {
                note('#user-form-error', err.message, true);
            }
        });

        var pwModal = $('#password-modal');
        $('#close-password-modal').addEventListener('click', closePasswordModal);
        $('#password-cancel').addEventListener('click', closePasswordModal);
        pwModal.addEventListener('click', function(e) { if (e.target === pwModal) closePasswordModal(); });
        $('#password-reset-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            if (!passwordTarget) return;
            var password = e.target.elements.password.value;
            try {
                await post('/app/admin/users/' + passwordTarget.id + '/password', { password: password });
                closePasswordModal();
                note('#account-status', '密码已重置');
                refreshAudit();
            } catch (err) {
                note('#account-status', '重置失败：' + err.message, true);
            }
        });

        refreshUsers();
        refreshAudit();
    }

    async function refreshTotp() {
        if (!$('#totp-current')) return;
        try {
            var d = await json('/app/account/totp');
            state.totpEnabled = d.totp_enabled;
            $('#totp-current').textContent = d.totp_enabled ? '当前状态：已开启' : '当前状态：未开启';
            $('#totp-off').style.display = d.totp_enabled ? 'none' : '';
            $('#totp-on').style.display = d.totp_enabled ? '' : 'none';
            $('#totp-enrolling').style.display = 'none';
        } catch (err) { note('#totp-status', err.message, true); }
    }

    function initSecurityTab() {
        if (!$('#totp-setup-form')) return;
        refreshTotp();
        var cancel = $('#totp-cancel');
        if (cancel) cancel.addEventListener('click', refreshTotp);
        $('#totp-setup-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            try {
                var d = await post('/app/account/totp/setup', { current_password: e.target.elements.current_password.value });
                $('#totp-qr').src = d.qr_data_url;
                $('#totp-secret').textContent = '手动密钥：' + d.secret;
                $('#totp-off').style.display = 'none';
                $('#totp-enrolling').style.display = '';
                note('#totp-status', '');
            } catch (err) { note('#totp-status', err.message, true); }
        });
        $('#totp-confirm-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            try {
                var d = await post('/app/account/totp/confirm', { code: e.target.elements.code.value });
                state.recoveryCodes = d.recovery_codes || [];
                $('#recovery-list').innerHTML = '';
                state.recoveryCodes.forEach(function(code) {
                    $('#recovery-list').appendChild(el('li', null, code));
                });
                $('#recovery-modal').hidden = false;
                await refreshTotp();
                refreshAudit();
            } catch (err) { note('#totp-status', err.message, true); }
        });
        $('#totp-disable-form').addEventListener('submit', async function(e) {
            e.preventDefault();
            var f = e.target;
            try {
                await post('/app/account/totp/disable', { current_password: f.elements.current_password.value, code: f.elements.code.value });
                f.reset();
                await refreshTotp();
                note('#totp-status', '已关闭两步验证');
                refreshAudit();
            } catch (err) { note('#totp-status', err.message, true); }
        });
    }

    async function refreshAudit() {
        var tbody = $('#audit-table tbody');
        if (!tbody) return;
        try {
            var data = await json('/app/admin/audit?limit=50');
            tbody.innerHTML = '';
            (data.items || []).forEach(function(a) {
                var tr = el('tr');
                tr.appendChild(el('td', null, fmtTime(a.at)));
                tr.appendChild(el('td', null, (a.actor_label || '-') + ' (' + (a.actor_kind || '-') + ')'));
                tr.appendChild(el('td', null, a.action || '-'));
                tr.appendChild(el('td', null, a.target || '-'));
                tr.appendChild(el('td', null, a.ip || '-'));
                tr.appendChild(el('td', null, String(a.status_code === undefined ? '-' : a.status_code)));
                tbody.appendChild(tr);
            });
            if (!tbody.children.length) {
                tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '6', class: 'empty-state' }, '暂无审计记录'));
            }
        } catch (e) {
            tbody.innerHTML = '';
            tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '6' }, '读取失败：' + e.message));
        }
    }

    // --------------------------------- call flow: translation, agent, minutes

    var flowState = { callId: null, flows: {} };
    var TERMINAL_TASK = ['finished', 'failed', 'cancelled', 'no_answer', 'rejected', 'busy'];
    var TASK_LABEL = {
        queued: '排队中', dialing: '拨号中', in_call: '通话中', finished: '已完成',
        failed: '失败', cancelled: '已取消', no_answer: '无人接听', rejected: '已拒接', busy: '占线'
    };

    async function refreshFlowPanel() {
        var sel = $('#flow-call');
        if (!sel) return;
        var data;
        try {
            data = await json('/app/voice/flow');
        } catch (e) {
            return;
        }
        flowState.flows = {};
        var calls = data.calls || [];
        calls.forEach(function(c) { flowState.flows[c.call_id] = c; });
        var live = calls.filter(function(c) { return c.state !== 'ended'; });
        var keep = sel.value;
        sel.innerHTML = '';
        if (!live.length) {
            sel.appendChild(el('option', { value: '' }, '当前没有通话'));
            flowState.callId = null;
        } else {
            live.forEach(function(c) {
                sel.appendChild(el('option', { value: c.call_id },
                    (c.remote || c.call_id) + ' · ' + (STATE_LABEL[c.state] || c.state)));
            });
            var hasKeep = live.some(function(c) { return c.call_id === keep; });
            sel.value = hasKeep ? keep : live[0].call_id;
            flowState.callId = sel.value;
        }
        syncFlowControls();
    }

    function currentFlow() {
        if (!flowState.callId) return null;
        return flowState.flows[flowState.callId] || null;
    }

    function agentLive(flow) {
        return !!(flow && flow.agent && flow.agent.pending !== true && flow.agent.finished !== true);
    }

    function syncFlowControls() {
        var flow = currentFlow();
        var live = !!flow;
        ['#flow-apply-mode', '#flow-say-send', '#flow-summary', '#flow-agent'].forEach(function(sel) {
            var b = $(sel);
            if (b) b.disabled = !live;
        });
        var hand = $('#flow-handover');
        if (hand) hand.disabled = !(agentLive(flow) && !flow.handover);
        var modeSel = $('#flow-mode');
        if (modeSel) modeSel.value = (flow && flow.translation && flow.translation.mode) || 'off';
        var noteBox = $('#flow-note');
        if (noteBox) {
            var parts = [];
            if (flow && flow.translation) {
                parts.push('我：' + (flow.translation.my_language || '自动') + '　对方：' + (flow.translation.peer_language || '自动'));
            }
            if (flow && flow.translation_note) parts.push('说明：' + flow.translation_note);
            if (flow && flow.agent && flow.agent.pending) parts.push('AI 接管请求已提交');
            else if (agentLive(flow)) parts.push('AI 正在替你说这通电话');
            if (flow && flow.handover) parts.push('已接管回来');
            if (flow && flow.capture_error) parts.push('采集异常：' + flow.capture_error);
            noteBox.textContent = parts.join('　|　');
        }
    }

    function appendFlowLine(name, data) {
        var box = $('#flow-subs');
        if (!box || !data || data.call_id !== flowState.callId) return;
        var line;
        if (name === 'call.translation') {
            line = (data.speaker === 'me' ? '我' : '对方') + '（' + (data.source_language || '') +
                '→' + (data.target_language || '') + '）：' + (data.source_text || '') +
                ' ⇒ ' + (data.target_text || '');
        } else {
            var entry = data.entry || {};
            var role = entry.role === 'peer' ? '对方' : entry.role === 'me' ? '我' : entry.role === 'agent' ? 'AI' : '系统';
            line = role + '：' + (entry.content || '');
            if (entry.original && entry.original !== entry.content) line += '（原话：' + entry.original + '）';
        }
        box.appendChild(el('div', null, line));
        while (box.children.length > 120) box.removeChild(box.firstChild);
        box.scrollTop = box.scrollHeight;
    }

    async function flowPost(path, body, okText) {
        if (!flowState.callId) { note('#flow-status', '先选择一通通话', true); return; }
        try {
            await post('/app/voice/calls/' + flowState.callId + path, body);
            note('#flow-status', okText);
        } catch (e) {
            note('#flow-status', '操作失败：' + e.message, true);
            return;
        }
        await refreshFlowPanel();
    }

    async function flowSummary() {
        if (!flowState.callId) { note('#flow-status', '先选择一通通话', true); return; }
        var box = $('#flow-summary-box');
        try {
            var data = await json('/app/voice/calls/' + flowState.callId + '/summary');
            var m = data.summary || {};
            var lines = [m.summary || '（空纪要）'];
            if (m.purpose_achieved === true) lines.push('目的已达成');
            if (m.purpose_achieved === false) lines.push('目的未达成');
            if (m.purpose_evidence) lines.push('依据：' + m.purpose_evidence);
            ['key_facts', 'agreements', 'unconfirmed'].forEach(function(key) {
                (m[key] || []).forEach(function(item) { lines.push('· ' + item); });
            });
            (m.todos || []).forEach(function(t) {
                lines.push('· 待办：' + (t.task || '') + (t.owner ? '（' + t.owner + (t.due ? ' ' + t.due : '') + '）' : ''));
            });
            box.style.display = '';
            box.className = 'alert info';
            box.style.whiteSpace = 'pre-wrap';
            box.textContent = lines.join('\n');
            note('#flow-status', '纪要已生成');
        } catch (e) {
            note('#flow-status', '生成纪要失败：' + e.message, true);
        }
    }

    function initFlowPanel() {
        var sel = $('#flow-call');
        if (!sel) return;
        sel.addEventListener('change', function() {
            flowState.callId = sel.value || null;
            var box = $('#flow-subs');
            if (box) box.innerHTML = '';
            syncFlowControls();
        });
        $('#flow-refresh').addEventListener('click', refreshFlowPanel);
        $('#flow-apply-mode').addEventListener('click', function() {
            flowPost('/translate', { mode: $('#flow-mode').value }, '翻译模式已应用');
        });
        $('#flow-say-send').addEventListener('click', function() {
            var input = $('#flow-say');
            var text = (input.value || '').trim();
            if (!text) return;
            flowPost('/say', { text: text }, '已说给对方');
            input.value = '';
        });
        $('#flow-agent').addEventListener('click', function() {
            var purpose = prompt('要让 AI 在这通电话里谈成什么？', '');
            if (!purpose) return;
            flowPost('/agent', { purpose: purpose }, 'AI 接管请求已提交');
        });
        $('#flow-handover').addEventListener('click', function() {
            flowPost('/agent/handover', null, '已接管回来');
        });
        $('#flow-summary').addEventListener('click', flowSummary);
    }

    // --------------------------------------------------------- AI call tasks

    async function refreshTasks() {
        var tbody = $('#tasks-table tbody');
        if (!tbody) return;
        try {
            var data = await json('/app/ai-call/tasks?limit=50');
            tbody.innerHTML = '';
            (data.items || []).forEach(function(t) {
                var tr = el('tr');
                tr.appendChild(el('td', null, String(t.id)));
                tr.appendChild(el('td', null, t.purpose || ''));
                tr.appendChild(el('td', null, (t.direction === 'inbound' ? '待接入' : '外呼') + (t.number ? ' ' + t.number : '')));
                tr.appendChild(el('td', null, TASK_LABEL[t.status] || t.status));
                tr.appendChild(el('td', null, t.achieved === true ? '目的已达成' : t.achieved === false ? '目的未达成' : (t.result || t.error || '-')));
                var actions = el('td');
                actions.appendChild(callButton('查看', '', function() { viewTask(t.id); }));
                if (TERMINAL_TASK.indexOf(t.status) < 0) {
                    actions.appendChild(callButton('取消', 'danger', function() { cancelTask(t.id); }));
                }
                tr.appendChild(actions);
                tbody.appendChild(tr);
            });
            if (!tbody.children.length) {
                tbody.appendChild(el('tr', null)).appendChild(el('td', { colspan: '6', class: 'empty-state' }, '还没有任务'));
            }
        } catch (e) {
            note('#task-status', '读取任务失败：' + e.message, true);
        }
    }

    async function cancelTask(id) {
        try {
            await post('/app/ai-call/tasks/' + id + '/cancel', null);
            note('#task-status', '任务 ' + id + ' 已取消');
        } catch (e) {
            note('#task-status', '取消失败：' + e.message, true);
        }
        await refreshTasks();
    }

    async function viewTask(id) {
        var box = $('#task-detail');
        if (!box) return;
        try {
            var data = await json('/app/ai-call/tasks/' + id);
            var t = data.task || {};
            var lines = [
                '#' + t.id + '　' + (TASK_LABEL[t.status] || t.status) + '　' +
                    (t.direction === 'inbound' ? '待接入' : '外呼 ' + (t.number || '')),
                '目的：' + (t.purpose || ''),
                t.context ? '背景：' + t.context : null,
                t.result ? '结论：' + t.result : null,
                t.error ? '错误：' + t.error : null,
                t.achieved === true ? '目的已达成' : t.achieved === false ? '目的未达成' : null
            ].filter(function(x) { return x; });
            var m = t.summary || null;
            if (m) {
                if (m.summary) lines.push('纪要：' + m.summary);
                if (m.purpose_evidence) lines.push('依据：' + m.purpose_evidence);
                (m.key_facts || []).forEach(function(x) { lines.push('· 事实：' + x); });
                (m.agreements || []).forEach(function(x) { lines.push('· 约定：' + x); });
                (m.todos || []).forEach(function(x) {
                    lines.push('· 待办：' + (x.task || '') + (x.owner ? '（' + x.owner + (x.due ? ' ' + x.due : '') + '）' : ''));
                });
                (m.unconfirmed || []).forEach(function(x) { lines.push('· 未确认：' + x); });
            }
            var flow = data.flow || null;
            if (flow && (flow.transcript || []).length) {
                lines.push('通话内容：');
                flow.transcript.slice(-40).forEach(function(entry) {
                    var role = entry.role === 'peer' ? '对方' : entry.role === 'me' ? '我' : entry.role === 'agent' ? 'AI' : '系统';
                    lines.push('　' + role + '：' + (entry.content || ''));
                });
            }
            box.style.display = '';
            box.className = 'alert info';
            box.style.whiteSpace = 'pre-wrap';
            box.textContent = lines.join('\n');
        } catch (e) {
            note('#task-status', '读取任务详情失败：' + e.message, true);
        }
    }

    function initTasksPanel() {
        var form = $('#task-create-form');
        if (!form) return;
        form.addEventListener('submit', async function(e) {
            e.preventDefault();
            var purpose = ($('#task-purpose').value || '').trim();
            if (purpose.length < 2) { note('#task-status', '写清楚这通电话要谈成什么', true); return; }
            var direction = $('#task-direction').value;
            var number = ($('#task-number').value || '').trim();
            if (direction === 'outbound' && !number) { note('#task-status', '外呼任务需要填写号码', true); return; }
            var minutes = parseInt($('#task-minutes').value || '5', 10);
            var body = {
                purpose: purpose,
                direction: direction,
                context: ($('#task-context').value || '').trim() || null,
                peer_language: ($('#task-peer-language').value || '').trim() || null,
                max_seconds: minutes * 60
            };
            if (number) body.number = number;
            note('#task-status', '创建中…');
            try {
                var r = await api('/app/ai-call/tasks', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                });
                if (!r.ok) throw new Error(await errorText(r));
                var data = await r.json();
                note('#task-status', data.idempotent
                    ? '这个请求已提交过，返回原任务 #' + data.task.id
                    : '已创建任务 #' + data.task.id);
            } catch (err) {
                note('#task-status', '创建失败：' + err.message, true);
            }
            await refreshTasks();
        });
        $('#refresh-tasks').addEventListener('click', refreshTasks);
    }

    // ------------------------------------------------------- call policy form

    var CF_KEYS = [
        'inbound.mode', 'inbound.auto_answer_delay_seconds', 'inbound.ai_max_seconds',
        'inbound.ai_purpose', 'inbound.ai_opener',
        'translate.mode', 'translate.cadence', 'translate.my_language', 'translate.peer_language',
        'translate.stream_degrade_to_sentence',
        'recording.policy', 'recording.announce', 'recording.default_enabled',
        'transcript.save', 'summary.save', 'summary.auto_generate', 'summary.template'
    ];
    var CF_LABELS = {
        'inbound.mode': '来电处理',
        'inbound.auto_answer_delay_seconds': '自动接听延迟（秒）',
        'inbound.ai_max_seconds': '来电 AI 通话上限（秒）',
        'inbound.ai_purpose': '来电 AI 的默认目的',
        'inbound.ai_opener': '来电 AI 的开场白',
        'translate.mode': '实时翻译默认模式',
        'translate.cadence': '翻译节奏',
        'translate.my_language': '我方语言',
        'translate.peer_language': '对方语言',
        'translate.stream_degrade_to_sentence': '流式不稳时退回整句',
        'recording.policy': '录音策略',
        'recording.announce': '录音开始时告知对方',
        'recording.default_enabled': '默认开启录音',
        'transcript.save': '保存通话文字记录',
        'summary.save': '保存通话纪要',
        'summary.auto_generate': '挂断后自动生成纪要',
        'summary.template': '纪要模板要求'
    };
    var CF_SELECTS = {
        'inbound.mode': [['manual', '人工接听'], ['auto_answer', '自动接听'], ['auto_answer_ai', '自动接听并让 AI 谈']],
        'translate.mode': [['off', '关'], ['subtitle', '字幕'], ['voice', '字幕+语音']],
        'translate.cadence': [['sentence', '整句'], ['stream', '流式']],
        'recording.policy': [['forced', '强制录音'], ['optional', '允许关闭'], ['disabled', '不录音']]
    };
    var CF_AREA = ['inbound.ai_purpose', 'inbound.ai_opener', 'summary.template'];
    var cfForm = { values: {}, controllers: {} };

    async function refreshCallflowSettings() {
        var form = $('#callflow-settings-form');
        if (!form) return;
        var data;
        try {
            data = await json('/app/admin/settings');
        } catch (e) {
            form.innerHTML = '';
            form.appendChild(el('p', { class: 'hint' }, '需要管理员权限才能编辑通话策略：' + e.message));
            return;
        }
        cfForm.values = data.settings || {};
        cfForm.controllers = {};
        form.innerHTML = '';
        var editable = data.editable || [];
        CF_KEYS.forEach(function(key) {
            if (editable.indexOf(key) < 0) return;
            var value = cfForm.values[key];
            var wrap = el('label', null, CF_LABELS[key] || key);
            if (typeof value === 'boolean') {
                var cb = el('input', { type: 'checkbox' });
                cb.checked = !!value;
                cb.addEventListener('change', function() { cfForm.controllers[key] = cb.checked; });
                wrap.appendChild(cb);
                wrap.className = 'check';
            } else if (CF_SELECTS[key]) {
                var sel = el('select');
                CF_SELECTS[key].forEach(function(pair) {
                    sel.appendChild(el('option', { value: pair[0] }, pair[1]));
                });
                sel.value = String(value === null || value === undefined ? '' : value);
                sel.addEventListener('change', function() { cfForm.controllers[key] = sel.value; });
                wrap.appendChild(sel);
            } else if (CF_AREA.indexOf(key) >= 0) {
                var area = el('textarea', { rows: '3' });
                area.value = value === null || value === undefined ? '' : String(value);
                area.addEventListener('input', function() { cfForm.controllers[key] = area.value; });
                wrap.appendChild(area);
            } else {
                var input = el('input', { type: 'text' });
                input.value = value === null || value === undefined ? '' : String(value);
                input.addEventListener('input', function() { cfForm.controllers[key] = input.value; });
                wrap.appendChild(input);
            }
            form.appendChild(wrap);
        });
        if (!form.children.length) {
            form.appendChild(el('p', { class: 'hint' }, '没有可编辑的通话策略项。'));
        }
    }

    function initCallflowTab() {
        initFlowPanel();
        initTasksPanel();
        var refresh = $('#refresh-callflow');
        if (refresh) refresh.addEventListener('click', refreshCallflowSettings);
        var save = $('#save-callflow-settings');
        if (save) save.addEventListener('click', async function() {
            var patch = {};
            Object.keys(cfForm.controllers).forEach(function(key) {
                var value = cfForm.controllers[key];
                if (value === null || value === undefined) return;
                if (typeof value === 'string' &&
                        value === String(cfForm.values[key] === undefined ? '' : cfForm.values[key])) return;
                patch[key] = value;
            });
            if (!Object.keys(patch).length) { note('#callflow-status', '没有需要保存的改动'); return; }
            note('#callflow-status', '保存中…');
            try {
                var r = await api('/app/admin/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ settings: patch })
                });
                if (!r.ok) throw new Error(await errorText(r));
                var data = await r.json();
                note('#callflow-status', '已保存并热加载：' + Object.keys(data.applied || {}).join('、'));
                await refreshCallflowSettings();
            } catch (err) {
                note('#callflow-status', '保存失败：' + err.message, true);
            }
        });
    }

    // ------------------------------------------------------ service autostart

    async function refreshService() {
        var mode = $('#service-mode');
        if (!mode) return;
        var data;
        try {
            data = await json('/app/admin/service');
        } catch (e) {
            note('#service-status', '读取服务状态失败：' + e.message, true);
            return;
        }
        mode.textContent = data.service_managed ? 'systemd 服务' : (data.systemd ? '手动进程' : '无 systemd');
        mode.className = 'chip ' + (data.service_managed ? 'ok' : 'warn');
        var enabledChip = $('#service-enabled');
        enabledChip.textContent = data.enabled ? '已开启' : '未开启';
        enabledChip.className = 'chip ' + (data.enabled ? 'ok' : 'warn');
        var activeChip = $('#service-active');
        activeChip.textContent = data.active ? '运行中' : '未运行';
        activeChip.className = 'chip ' + (data.active ? 'ok' : 'bad');
        var cb = $('#service-autostart');
        cb.checked = !!data.enabled;
        cb.disabled = !data.systemd;
        var sw = $('#service-switch');
        if (sw) sw.hidden = !(data.systemd && data.installed && !data.service_managed);
        if (data.error) note('#service-status', data.error, true);
    }

    function initServicePanel() {
        var cb = $('#service-autostart');
        if (!cb) return;
        cb.addEventListener('change', async function() {
            var want = cb.checked;
            note('#service-status', want ? '开启开机自启…' : '关闭开机自启…');
            try {
                var r = await api('/app/admin/service/autostart', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ enabled: want })
                });
                if (!r.ok) throw new Error(await errorText(r));
                note('#service-status', want
                    ? '开机自启已开启，重启后由 systemd 拉起'
                    : '开机自启已关闭，正在运行的网关不受影响');
            } catch (e) {
                note('#service-status', '设置失败：' + e.message, true);
            }
            await refreshService();
        });
        var sw = $('#service-switch');
        if (sw) sw.addEventListener('click', async function() {
            if (!confirm('将结束当前手动进程并由 systemd 接管，连接会中断几秒。继续？')) return;
            try {
                var r = await api('/app/admin/service/switch', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: '{}'
                });
                if (!r.ok) throw new Error(await errorText(r));
                note('#service-status', '正在切换，页面稍后自动重连…');
                setTimeout(refreshService, 6000);
                setTimeout(refreshService, 12000);
            } catch (e) {
                note('#service-status', '切换失败：' + e.message, true);
            }
        });
        var refresh = $('#service-refresh');
        if (refresh) refresh.addEventListener('click', refreshService);
    }

    // ------------------------------------------------------------------- init

    async function init() {
        initDialpad();
        initMock();
        initCallsTab();
        initAudioTab();
        initAiTab();
        initCallflowTab();
        initServicePanel();
        initSecurityTab();
    initAccountsTab();

        await refreshDevices();
        await refreshAudio();
        await refreshCalls();
        refreshCallStats();
        refreshCallHistory();
        refreshAi();
        refreshCallflowSettings();
        refreshTasks();
        refreshFlowPanel();
        refreshService();

        connectConsoleWs();
        setInterval(refreshCalls, 5000);
        setInterval(refreshFlowPanel, 5000);
        setInterval(refreshTasks, 10000);
        setInterval(refreshDevices, 15000);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();



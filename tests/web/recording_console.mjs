// Console recording controls against a live mock gateway.
//
// The console is vanilla JS in one IIFE, so this drives it the way a person
// would: load the page, click the buttons on the live call card, then reload
// the page after the call and look for the player in the log. Run only against
// an isolated mock gateway; SMSGW_URL is required for that reason.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';

const base = process.env.SMSGW_URL;
assert(base, 'Set SMSGW_URL to an isolated mock gateway');
let token;

async function api(path, method = 'GET', body) {
  const r = await fetch(base + path, {
    method,
    headers: {'Content-Type': 'application/json', ...(token ? {Authorization: 'Bearer ' + token} : {})},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  assert(r.ok, path + ' -> ' + r.status + ' ' + JSON.stringify(data));
  return data;
}

token = (await api('/api/v1/auth/login', 'POST', {
  username: process.env.SMSGW_USER || 'appdev',
  password: process.env.SMSGW_PASS || 'AppDev12345',
})).token;

const wait = async (fn, ms = 10000) => {
  const end = Date.now() + ms;
  while (!(await fn())) {
    if (Date.now() > end) throw new Error('wait timed out');
    await new Promise(r => setTimeout(r, 100));
  }
};

function openConsole() {
  return fetch(base + '/app', {headers: {Authorization: 'Bearer ' + token}})
    .then(r => r.text())
    .then(html => {
      const dom = new JSDOM(html, {url: base + '/app', runScripts: 'outside-only'});
      const w = dom.window;
      w.fetch = (p, opts = {}) => fetch(base + p, {
        ...opts,
        headers: {...opts.headers, Authorization: 'Bearer ' + token},
      });
      w.eval(readFileSync(new URL('../../app/static/app_console.js', import.meta.url), 'utf8'));
      return dom;
    });
}

const cardButtons = dom => {
  const card = dom.window.document.querySelector('#active-calls .call-card');
  return card ? Array.from(card.querySelectorAll('button')) : [];
};
const cardText = dom => dom.window.document.querySelector('#active-calls').textContent;
const click = (dom, label) => {
  const b = cardButtons(dom).find(x => x.textContent === label);
  assert(b, 'missing button ' + label);
  b.click();
};

// ---------------------------------------------------------------- live call
// The console card only offers 开始录音 while nothing is capturing, so the
// test pins the policy it needs instead of inheriting whatever the gateway
// under test happens to store.
await api('/app/admin/settings', 'PUT', {
  settings: {'recording.policy': 'optional', 'recording.default_enabled': false},
});
await api('/app/voice/mock/1/scenario', 'POST', {scenario: 'answer'});
const session = await api('/app/voice/dial', 'POST', {number: '13800138000', device_id: 1});
const callId = session.call_id;
await wait(async () => (await api('/app/voice/state')).active.some(s => s.call_id === callId && s.state === 'active'));

const live = await openConsole();
try {
  await wait(async () => cardButtons(live).some(b => b.textContent === '开始录音'));
  assert(!cardText(live).includes('录音中'), 'nothing is recording yet');

  click(live, '开始录音');
  await wait(async () => (await api('/app/voice/state')).recording_call_ids.includes(callId));
  await wait(async () => cardText(live).includes('录音中'));

  click(live, '播报录音提示');
  await wait(async () => cardText(live).includes('已告知对方录音'));
  assert(cardText(live).includes('录音已开启，接下来的通话将被记录'));
  assert(!cardText(live).includes('证词'), 'the notice must never claim to be evidence');

  // A real sound card needs a moment before the first PCM chunk arrives; a
  // capture stopped inside that window is correctly reported as empty, so
  // give the card time before hanging the recording up.
  await new Promise(r => setTimeout(r, 2000));
  click(live, '停止录音');
  await wait(async () => !(await api('/app/voice/state')).recording_call_ids.includes(callId));
  await wait(async () => !cardText(live).includes('录音中'));
  console.log('PASS: 通话卡片录音开关与告知对方');
} finally {
  live.window.close();
  await api('/app/voice/1/hangup', 'POST');
  await wait(async () => !(await api('/app/voice/state')).active.some(s => s.call_id === callId));
}

// ------------------------------------------------------- player in the log
const after = await openConsole();
try {
  await wait(async () => {
    const rows = Array.from(after.window.document.querySelectorAll('#calls-table tbody tr'));
    return rows.some(tr => tr.querySelector('audio') && tr.textContent.includes(callId.slice(0, 4)) === true) ||
      rows.some(tr => tr.querySelector('audio'));
  });
  const row = Array.from(after.window.document.querySelectorAll('#calls-table tbody tr'))
    .find(tr => tr.querySelector('audio'));
  assert(row, 'no row carries a player');
  const audio = row.querySelector('audio');
  assert(audio.getAttribute('src').includes('/app/voice/calls/' + callId + '/recording'));
  assert(row.textContent.includes('已告知对方'), row.textContent);
  console.log('PASS: 通话记录内嵌播放器与告知结果');
} finally {
  after.window.close();
}
process.exit(0);


// Run only against an isolated mock gateway. SMSGW_URL is deliberately required.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
const base = process.env.SMSGW_URL;
assert(base, 'Set SMSGW_URL to an isolated mock gateway');
let token;
async function api(path, method='GET', body) {
  const r = await fetch(base+path, {method, headers:{'Content-Type':'application/json', ...(token ? {Authorization:'Bearer '+token} : {})},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await r.json(); assert(r.ok, JSON.stringify(data)); return data;
}
token = (await api('/api/v1/auth/login','POST',{username:process.env.SMSGW_USER || 'appdev',password:process.env.SMSGW_PASS || 'AppDev12345'})).token;
// This endpoint cannot succeed for a real modem; never delete on a production gateway.
await api('/app/voice/mock/1/debug');
await api('/app/conversations/bulk_delete','POST',{peers:(await api('/app/conversations')).items.map(c=>c.peer_key).concat('13900000999')});
for (const n of ['13900000001','13900000002','13900000003']) {
  await api('/app/sms/mock/1/incoming','POST',{sender:n,content:'网页联调短信 '+n});
}
const html = await (await fetch(base+'/app', {headers:{Authorization:'Bearer '+token}})).text();
const dom = new JSDOM(html, {url:base+'/app',runScripts:'outside-only'});
const w = dom.window;
w.fetch = (p, opts={}) => fetch(base+p, {...opts, headers:{...opts.headers, Authorization:'Bearer '+token}});
let accept = true;
w.confirm = () => accept;
w.eval(readFileSync(new URL('../../app/static/app_messages.js', import.meta.url),'utf8'));
const $ = sel => w.document.querySelector(sel);
const wait = async fn => {const end = Date.now()+6000; while (!(await fn())) {if (Date.now()>end) throw new Error('wait timed out'); await new Promise(r=>setTimeout(r,50));}};
const row = peer => $(`tr[data-peer="${peer}"]`);
const clickAction = (peer,label) => [...row(peer).querySelectorAll('button')].find(b=>b.textContent===label).click();
const unread = async peer => (await api('/app/conversations')).items.find(c=>c.peer_key===peer)?.unread;
await wait(()=>row('13900000003'));
clickAction('13900000001','查看');
await wait(async ()=>await unread('13900000001')===0);
await wait(()=>row('13900000001').textContent.includes('标为未读'));
clickAction('13900000001','标为未读');
await wait(async ()=>await unread('13900000001')>0);
await wait(()=>!$('#sms-delete-read').disabled);
assert($('#sms-thread').hidden);
clickAction('13900000002','查看');
await wait(async ()=>await unread('13900000002')===0);
await wait(()=>row('13900000002').textContent.includes('标为未读'));
$('#sms-delete-read').click();
await wait(()=>!row('13900000002'));
assert(row('13900000001')); assert(row('13900000003'));
await wait(()=>!$('#sms-delete-read').disabled);
accept=false;
clickAction('13900000001','删除');
await new Promise(r=>setTimeout(r,100)); assert(row('13900000001'));
accept=true;
for (const peer of ['13900000001','13900000003']) {
 const box=row(peer).querySelector('input'); box.checked=true; box.dispatchEvent(new w.Event('change'));
}
assert(!$('#sms-delete-selected').disabled);
$('#sms-delete-selected').click();
await wait(()=>$('tr[data-peer]')===null);
assert.equal((await api('/app/conversations')).unread_total,0);
// Realtime events from the shared app.js socket refresh the management table.
await api('/app/sms/mock/1/incoming','POST',{sender:'13900000004',content:'实时刷新验证'});
w.dispatchEvent(new w.CustomEvent('gateway-sms',{detail:{event:'sms.received'}}));
await wait(()=>row('13900000004'));
console.log('PASS: 查看已读、标未读、删除已读保护、取消删除、多选删除、未读计数、实时刷新');
dom.window.close();

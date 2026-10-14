/// Live end to end check of the Flutter client contract against a running
/// gateway.
///
/// This is not a unit test: it drives the same ApiClient the app uses, over
/// real HTTP, against a server started in mock modem mode. Run it after any
/// change to the API layer or to the server routes.
///
///   dart run tool/live_integration.dart --server http://127.0.0.1:8811 \
///       --user appdev --pass AppDev12345
library;

import 'dart:async';
import 'dart:io';

import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/data/models.dart';

int _passed = 0;
int _failed = 0;
final List<String> _failures = [];

void _ok(String name, [String detail = '']) {
  _passed++;
  stdout.writeln('  PASS  ' + name + (detail.isEmpty ? '' : '  (' + detail + ')'));
}

void _fail(String name, String reason) {
  _failed++;
  _failures.add(name + ': ' + reason);
  stdout.writeln('  FAIL  ' + name + '  -> ' + reason);
}

void _section(String title) {
  stdout.writeln('');
  stdout.writeln('== ' + title + ' ==');
}

Future<void> _check(String name, Future<void> Function() body) async {
  try {
    await body();
  } on ApiException catch (e) {
    _fail(name, 'ApiException ' + e.statusCode.toString() + ' ' + e.message);
  } catch (e) {
    _fail(name, e.toString());
  }
}

void _expect(bool condition, String name, [String detail = '']) {
  if (condition) {
    _ok(name, detail);
  } else {
    _fail(name, detail.isEmpty ? 'condition false' : detail);
  }
}

Future<void> _waitFor(
  Future<bool> Function() predicate, {
  Duration timeout = const Duration(seconds: 12),
  String label = 'condition',
}) async {
  final deadline = DateTime.now().add(timeout);
  while (DateTime.now().isBefore(deadline)) {
    if (await predicate()) return;
    await Future<void>.delayed(const Duration(milliseconds: 150));
  }
  throw StateError('timed out waiting for ' + label);
}

Map<String, String> _args(List<String> argv) {
  final out = <String, String>{};
  for (var i = 0; i < argv.length; i++) {
    final a = argv[i];
    if (a.startsWith('--') && i + 1 < argv.length) {
      out[a.substring(2)] = argv[i + 1];
      i++;
    }
  }
  return out;
}

Future<void> main(List<String> argv) async {
  final args = _args(argv);
  final server = args['server'] ?? Platform.environment['SMSGW_URL'] ?? 'http://127.0.0.1:8811';
  final user = args['user'] ?? Platform.environment['SMSGW_USER'] ?? 'appdev';
  final pass = args['pass'] ?? Platform.environment['SMSGW_PASS'] ?? 'AppDev12345';

  stdout.writeln('Live integration against ' + server + ' as ' + user);

  final api = ApiClient(baseUrl: server);

  // ------------------------------------------------------------- auth
  _section('登录与会话');
  LoginResult? login;
  await _check('POST /api/v1/auth/login', () async {
    login = await api.login(username: user, password: pass);
    _expect(login!.token.isNotEmpty, '返回会话令牌');
    _expect(login!.user.username == user, '用户名为 ' + user, login!.user.username);
  });
  if (login == null) {
    stdout.writeln('无法登录，后续检查跳过。');
    exitCode = 1;
    return;
  }

  MeResult? me;
  await _check('GET /api/v1/auth/me', () async {
    me = await api.me();
    _expect(me!.caps.voice, '服务端已开启语音能力');
    _expect(me!.caps.sms, '服务端已开启短信能力');
    _expect(me!.devices.isNotEmpty, '发现模块 ' + me!.devices.length.toString() + ' 台');
  });

  // ---------------------------------------------------------- devices
  _section('设备');
  List<DeviceInfo> devices = [];
  await _check('GET /app/devices', () async {
    devices = await api.devices();
    final online = devices.where((d) => d.isOnline).toList();
    _expect(online.isNotEmpty, '在线设备 ' + online.length.toString() + ' 台');
    _expect(devices.first.code.isNotEmpty, '设备编号 ' + devices.first.code);
  });
  if (devices.isEmpty) {
    stdout.writeln('没有设备，语音与短信检查跳过。');
    exitCode = 1;
    return;
  }
  final device = devices.firstWhere((d) => d.isOnline, orElse: () => devices.first);
  final deviceId = device.id;

  await _check('GET /app/devices/{id}/signal', () async {
    final sig = await api.deviceSignal(deviceId);
    _expect(sig.isNotEmpty, '信号读数 ' + sig.entries.take(3).map((e) => e.key + '=' + e.value.toString()).join(' '));
  });

  await _check('POST /app/devices/{id}/at (AT 直通)', () async {
    final res = await api.sendRawAt(deviceId, 'AT+CSQ');
    _expect(res != null, '返回 ' + res.toString().replaceAll('\n', ' ').substring(0, res.toString().length.clamp(0, 60)));
  });

  // --------------------------------------------------------- contacts
  _section('通讯录');
  final testNumber = '+861390000' + (1000 + deviceId).toString();
  var contactId = 0;
  await _check('POST /app/contacts', () async {
    final created = await api.createContact(
        Contact(id: 0, name: '联调联系人', number: testNumber, groupName: '测试', favorite: true));
    contactId = created.id;
    _expect(contactId > 0, '新联系人 id=' + contactId.toString());
  });
  await _check('GET /app/contacts', () async {
    final page = await api.contacts();
    _expect(page.items.any((c) => c.id == contactId), '列表中包含新联系人');
    _expect(page.groups.contains('测试'), '分组 测试 已建立', page.groups.join(','));
  });
  await _check('PATCH /app/contacts/{id}', () async {
    final updated = await api.updateContact(
        contactId, Contact(id: contactId, name: '联调联系人改', number: testNumber, favorite: false));
    _expect(updated.name == '联调联系人改', '改名生效');
    _expect(!updated.favorite, '取消常用生效');
  });

  // -------------------------------------------------------------- sms
  _section('短信收发');
  await _check('POST /app/send 发送短信', () async {
    await api.sendSms(to: testNumber, content: '联调测试短信 A', deviceId: deviceId);
    _ok('发送请求已受理');
  });
  await _check('GET /app/conversations 出现会话', () async {
    await _waitFor(() async {
      final list = await api.conversations();
      return list.any((c) => c.peer.replaceAll(RegExp(r'[^0-9]'), '').endsWith(
          testNumber.replaceAll(RegExp(r'[^0-9]'), '')));
    }, label: '会话出现');
    final list = await api.conversations();
    final conv = list.firstWhere((c) =>
        c.peer.replaceAll(RegExp(r'[^0-9]'), '').endsWith(
            testNumber.replaceAll(RegExp(r'[^0-9]'), '')));
    _expect(conv.messageCount >= 1, '会话消息数 ' + conv.messageCount.toString());
  });
  await _check('模拟收到短信 + 会话更新', () async {
    await api.mockIncomingSms(deviceId, testNumber, '这是网关模拟的来信');
    await _waitFor(() async {
      final msgs = await api.thread(testNumber);
      return msgs.any((m) => m.isInbound && m.content.contains('模拟的来信'));
    }, label: '来信入库');
    final msgs = await api.thread(testNumber);
    _expect(msgs.any((m) => m.isInbound), '收件方向正确');
    _expect(msgs.any((m) => !m.isInbound), '发件方向正确');
  });
  await _check('GET /app/conversations/{peer}/messages 排序', () async {
    final msgs = await api.thread(testNumber);
    for (var i = 1; i < msgs.length; i++) {
      if (msgs[i].at < msgs[i - 1].at) {
        _fail('消息按时间升序', '第 ' + i.toString() + ' 条时间倒挂');
        return;
      }
    }
    _ok('消息按时间升序', msgs.length.toString() + ' 条');
  });
  await _check('GET /app/search 全文搜索', () async {
    final res = await api.search('联调测试短信');
    final msgs = (res['messages'] as List?) ?? [];
    _expect(msgs.isNotEmpty, '命中 ' + msgs.length.toString() + ' 条');
  });
  await _check('草稿保存', () async {
    await api.saveDraft(testNumber, '未发出的草稿');
    final list = await api.conversations();
    final conv = list.firstWhere(
        (c) => c.peer.replaceAll(RegExp(r'[^0-9]'), '').endsWith(
            testNumber.replaceAll(RegExp(r'[^0-9]'), '')),
        orElse: () => list.first);
    _expect(conv.draft == '未发出的草稿', '草稿回读 ' + (conv.draft ?? 'null'));
    await api.saveDraft(testNumber, null);
  });

  // ------------------------------------------------------------ voice
  _section('语音通话');
  await _check('GET /app/voice/state 初始空闲', () async {
    final st = await api.voiceState();
    _expect(st['voice_enabled'] == true, '语音已启用');
    _expect(((st['active'] as List?) ?? []).isEmpty, '当前无通话');
  });

  await _check('拨号 -> 振铃 -> 接通 -> 挂断（scenario=answer）', () async {
    await api.mockScenario(deviceId, 'answer');
    final session = await api.dial(number: testNumber, deviceId: deviceId);
    _expect(session.callId.isNotEmpty, 'call_id=' + session.callId);
    _expect(CallState.isLive(session.state), '初始状态 ' + session.state);

    await _waitFor(() async {
      final active = await api.activeCalls();
      return active.any((c) => c.callId == session.callId && c.state == CallState.active);
    }, label: '进入 active');
    _ok('呼叫已接通');

    await api.speak(deviceId, '联调语音播报测试');
    _ok('通话中 TTS 播报已下发');

    await api.dtmf(deviceId, '123#');
    _ok('DTMF 123# 已下发');

    await api.hangup(deviceId);
    await _waitFor(() async {
      final active = await api.activeCalls();
      return !active.any((c) => c.callId == session.callId);
    }, label: '通话结束');
    _ok('挂断后会话已清除');
  });

  await _check('GET /app/voice/history 生成话单', () async {
    await _waitFor(() async {
      final hist = await api.callHistory(limit: 20);
      return hist.any((c) => c.remote?.contains(testNumber.substring(testNumber.length - 7)) == true);
    }, label: '话单落库');
    final hist = await api.callHistory(limit: 20);
    final rec = hist.firstWhere(
        (c) => c.remote?.contains(testNumber.substring(testNumber.length - 7)) == true);
    _expect(rec.state == CallState.ended, '话单状态 ' + rec.state);
    _expect(rec.direction == 'outbound', '话单方向 ' + rec.direction);
    _expect(rec.duration >= 0, '通话时长 ' + rec.duration.toString() + ' 秒');
  });

  await _check('对方占线（scenario=busy）', () async {
    await api.mockScenario(deviceId, 'busy');
    await api.dial(number: testNumber, deviceId: deviceId);
    await _waitFor(() async {
      final hist = await api.callHistory(limit: 5);
      return hist.isNotEmpty && hist.first.endReason == 'busy';
    }, label: 'busy 话单');
    _ok('忙线原因正确记录为 busy');
  });

  await _check('来电 -> 接听 -> 挂断', () async {
    await api.mockIncomingCall(deviceId, '+8613700001234');
    await _waitFor(() async {
      final active = await api.activeCalls();
      return active.any((c) => c.state == CallState.incoming);
    }, label: '来电振铃');
    _ok('收到来电');

    final incoming = (await api.activeCalls()).firstWhere((c) => c.state == CallState.incoming);
    await api.answer(incoming.deviceId);
    await _waitFor(() async {
      final active = await api.activeCalls();
      return active.any((c) => c.callId == incoming.callId && c.state == CallState.active);
    }, label: '接听后 active');
    _ok('接听成功');

    await api.hangup(incoming.deviceId);
    await _waitFor(() async {
      final active = await api.activeCalls();
      return !active.any((c) => c.callId == incoming.callId);
    }, label: '挂断');
    _ok('来电已挂断');
  });

  await _check('恢复默认 scenario', () async {
    await api.mockScenario(deviceId, 'answer');
    _ok('scenario=answer');
  });

  // ------------------------------------------------------------ audio
  _section('声卡与提示音');
  AudioState? audio;
  await _check('GET /app/audio/devices', () async {
    audio = await api.audioDevices();
    _expect(audio!.backend.isNotEmpty, '后端 ' + audio!.backend);
    _expect(audio!.outputs.isNotEmpty, '输出声卡 ' + audio!.outputs.length.toString() + ' 个');
    _expect(audio!.inputs.isNotEmpty, '输入声卡 ' + audio!.inputs.length.toString() + ' 个');
  });
  await _check('切换输出声卡并回读', () async {
    if (audio!.outputs.length < 2) {
      _ok('只有一个输出设备，跳过切换', audio!.outputs.first.id);
      return;
    }
    final target = audio!.outputs.firstWhere((d) => !d.isDefault);
    await api.setOutput(target.id);
    final after = await api.audioDevices();
    _expect(after.outputDevice == target.id, '输出已切到 ' + target.id, after.outputDevice);
    // Restore the default so the run leaves no side effects.
    await api.setOutput('');
  });
  await _check('切换输入声卡并回读', () async {
    if (audio!.inputs.isEmpty) {
      _ok('没有输入设备，跳过');
      return;
    }
    final target = audio!.inputs.first;
    await api.setInput(target.id);
    final after = await api.audioDevices();
    _expect(after.inputDevice == target.id, '输入已切到 ' + target.id, after.inputDevice);
    await api.setInput('');
  });
  await _check('音量调节', () async {
    final after = await api.setVolume(0.55);
    _expect((after.volume - 0.55).abs() < 0.01, '音量 ' + after.volume.toString());
    await api.setVolume(0.8);
  });
  await _check('提示音列表与试听', () async {
    final prompts = await api.prompts();
    _expect(prompts.length >= 6, '提示音 ' + prompts.length.toString() + ' 条');
    final ids = prompts.map((p) => p.id).toSet();
    for (final required in const [
      'dial_connecting',
      'ringback',
      'incoming_call',
      'call_busy',
      'call_no_answer',
      'call_ended'
    ]) {
      _expect(ids.contains(required), '包含提示音 ' + required);
    }
    final connecting = prompts.firstWhere((p) => p.id == 'dial_connecting');
    _expect(connecting.text.contains('正在接通'), '接通提示文案: ' + connecting.text);
    await api.playPrompt('dial_connecting');
    _ok('试听请求已下发');
  });

  // --------------------------------------------------------------- ai
  _section('AI 语音与对话');
  await _check('GET /app/ai/status', () async {
    final st = await api.aiStatus();
    _expect(st['provider'] != null, '对话供应商 ' + st['provider'].toString());
    _expect(st['config'] is Map, '返回可编辑配置');
  });
  await _check('GET /app/ai/health', () async {
    final h = await api.aiHealth();
    _expect(h.isNotEmpty, '健康检查 ' + h.keys.take(4).join(','));
  });
  await _check('POST /app/ai/chat 对话', () async {
    final reply = await api.chat(message: '你好，请回复连接正常。', history: const []);
    _expect(reply.reply.isNotEmpty, '回复 ' + reply.reply.substring(0, reply.reply.length.clamp(0, 40)));
  });
  await _check('多轮对话携带历史', () async {
    final history = [
      ChatTurn(role: 'user', content: '我叫小张'),
      ChatTurn(role: 'assistant', content: '你好小张'),
    ];
    final reply = await api.chat(message: '我叫什么？', history: history);
    _expect(reply.reply.isNotEmpty, '回复长度 ' + reply.reply.length.toString());
  });
  await _check('POST /app/ai/tts 文字转语音', () async {
    final bytes = await api.tts('联调语音合成测试');
    _expect(bytes.isNotEmpty, 'TTS 返回 ' + bytes.length.toString() + ' 字节');
    // A WAV starts with the RIFF magic; anything else means the gateway handed
    // back an error page that the client would have tried to play as audio.
    final magic = String.fromCharCodes(bytes.take(4));
    _expect(magic == 'RIFF', '响应是 WAV 音频', magic);
  });

  await _check('POST /app/voice/{id}/ai/speak 通话内 AI 播报', () async {
    // 'answer' connects and then holds the call up until we hang up, which is
    // what this check needs. 'hang_forever' parks the call in alerting and
    // never emits CONNECT, so the call never becomes active.
    await api.mockScenario(deviceId, 'answer');
    final session = await api.dial(number: testNumber, deviceId: deviceId);
    await _waitFor(() async {
      final active = await api.activeCalls();
      return active.any((c) => c.callId == session.callId && c.state == CallState.active);
    }, label: 'AI 播报前接通');

    final moduleRes =
        await api.aiSpeakInCall(deviceId, '对方询问业务办理进度，请简短回复。');
    _expect((moduleRes['reply'] ?? '').toString().isNotEmpty,
        'module 引擎回复 ' + moduleRes['reply'].toString().substring(0, 20));
    _expect(moduleRes['channel'] == 'module_tts', '走模块内置合成 ' + moduleRes['channel'].toString());

    final audioRes = await api.aiSpeakInCall(deviceId, '请再确认一次。', engine: 'audio');
    _expect(audioRes['channel'] == 'cloud_tts', '走云端 TTS ' + audioRes['channel'].toString());

    await api.hangup(deviceId);
    await _waitFor(() async {
      final active = await api.activeCalls();
      return !active.any((c) => c.callId == session.callId);
    }, label: 'AI 播报后挂断');
    _ok('AI 播报后已挂断');
  });

  // ---------------------------------------------------------- settings
  _section('后台配置');
  Map<String, dynamic>? settingsData;
  await _check('GET /app/admin/settings', () async {
    settingsData = await api.settings();
    final values = Map<String, dynamic>.from(settingsData!['settings'] as Map);
    final editable = ((settingsData!['editable'] as List?) ?? []).map((e) => e.toString()).toList();
    _expect(values.containsKey('ai.chat.model'), '包含 ai.chat.model');
    _expect(values.containsKey('voice.tts_mode'), '包含 voice.tts_mode');
    _expect(editable.isNotEmpty, '可编辑项 ' + editable.length.toString() + ' 个');
  });
  await _check('PUT /app/admin/settings 热更新', () async {
    final res = await api.saveSettings({'ai.chat.model': 'gpt-4o-mini-test'});
    _expect(res['ok'] == true, '服务端接受写入');
    final applied = Map<String, dynamic>.from(res['applied'] as Map);
    _expect(applied['ai.chat.model'] == 'gpt-4o-mini-test',
        '写入生效 ' + applied['ai.chat.model'].toString());
    // Read it back through the getter to prove it was persisted, not just echoed.
    final reread = await api.settings();
    _expect((reread['settings'] as Map)['ai.chat.model'] == 'gpt-4o-mini-test', '重新读取一致');
    await api.saveSettings({'ai.chat.model': 'gpt-4o-mini'});
    final back = await api.settings();
    _expect((back['settings'] as Map)['ai.chat.model'] == 'gpt-4o-mini', '已还原');
  });
  await _check('GET /app/admin/stats', () async {
    final s = await api.stats();
    _expect((s['devices_total'] as num) > 0, '设备总数 ' + s['devices_total'].toString());
    _expect(s['calls'] is Map, '通话统计 ' + ((s['calls'] as Map)['total']).toString() + ' 通');
  });

  // ------------------------------------------------------- admin users
  _section('账号管理');
  if (me!.caps.admin) {
    final tmpUser = 'itest' + DateTime.now().millisecondsSinceEpoch.toString().substring(8);
    var tmpId = 0;
    await _check('POST /app/admin/users 新建账号', () async {
      final res = await api.createUser(
          username: tmpUser, password: 'ItPass12345', role: 'operator', displayName: '联调账号');
      tmpId = ((res is Map ? res['id'] : null) as num?)?.toInt() ?? 0;
      if (tmpId == 0) {
        final users = await api.users();
        tmpId = users.firstWhere((u) => u['username'] == tmpUser)['id'] as int;
      }
      _expect(tmpId > 0, '新账号 id=' + tmpId.toString());
    });
    await _check('新账号可以登录', () async {
      final probe = ApiClient(baseUrl: server);
      final res = await probe.login(username: tmpUser, password: 'ItPass12345');
      _expect(res.token.isNotEmpty, '登录成功');
      _expect(res.user.role == 'operator', '角色 operator');
      await probe.logout();
    });
    await _check('PATCH 停用后无法登录', () async {
      await api.updateUser(tmpId, isActive: false);
      final probe = ApiClient(baseUrl: server);
      var blocked = false;
      try {
        await probe.login(username: tmpUser, password: 'ItPass12345');
      } on ApiException catch (e) {
        blocked = e.statusCode == 401 || e.statusCode == 403;
      }
      _expect(blocked, '停用账号被拒绝登录');
      await api.updateUser(tmpId, isActive: true);
    });
    await _check('重置密码', () async {
      await api.resetPassword(tmpId, 'NewPass54321');
      final probe = ApiClient(baseUrl: server);
      final res = await probe.login(username: tmpUser, password: 'NewPass54321');
      _expect(res.token.isNotEmpty, '新密码可登录');
      await probe.logout();
    });
    await _check('切换角色为管理员', () async {
      await api.updateUser(tmpId, role: 'admin');
      final users = await api.users();
      final row = users.firstWhere((u) => u['id'] == tmpId);
      _expect(row['role'] == 'admin', '角色 ' + row['role'].toString());
    });
    await _check('DELETE 删除账号', () async {
      await api.deleteUser(tmpId);
      final users = await api.users();
      _expect(!users.any((u) => u['id'] == tmpId), '账号已删除');
    });
  } else {
    _ok('当前账号非管理员，跳过账号管理检查');
  }

  // ---------------------------------------------------------- cleanup
  _section('清理');
  await _check('删除联调联系人', () async {
    if (contactId > 0) {
      await api.deleteContact(contactId);
      final page = await api.contacts();
      _expect(!page.items.any((c) => c.id == contactId), '联系人已删除');
    }
  });
  await _check('退出登录', () async {
    await api.logout();
    var rejected = false;
    try {
      await api.devices();
    } on ApiException catch (e) {
      rejected = e.isAuthError;
    }
    _expect(rejected, '令牌注销后接口拒绝访问');
  });

  stdout.writeln('');
  stdout.writeln('----------------------------------------');
  stdout.writeln('通过 ' + _passed.toString() + ' 项，失败 ' + _failed.toString() + ' 项');
  if (_failures.isNotEmpty) {
    stdout.writeln('失败明细:');
    for (final f in _failures) {
      stdout.writeln('  - ' + f);
    }
  }
  exitCode = _failed == 0 ? 0 : 1;
}


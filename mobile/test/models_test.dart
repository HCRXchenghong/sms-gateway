import 'package:flutter_test/flutter_test.dart';
import 'package:voicesms_app/src/data/models.dart';

/// Payloads copied verbatim from a running gateway so a server side rename
/// breaks these tests immediately.
void main() {
  group('DeviceInfo', () {
    const raw = {
      'id': 1,
      'number': 1,
      'code': '001',
      'label': null,
      'display_name': '编号001',
      'sim_number': '+10000000001',
      'iccid': 'MOCKICCID001',
      'imei': 'MOCKIMEI001',
      'model': 'A7670G Mock',
      'manufacturer': 'SIMCom',
      'at_port': 'MOCK1',
      'status': 'online',
      'last_error': null,
      'last_seen_at': 1791867555,
    };

    test('parses the /app/devices row', () {
      final d = DeviceInfo.fromJson(Map<String, dynamic>.from(raw));
      expect(d.id, 1);
      expect(d.code, '001');
      expect(d.displayName, '编号001');
      expect(d.isOnline, isTrue);
      expect(d.simNumber, '+10000000001');
      expect(d.model, 'A7670G Mock');
    });

    test('falls back to the label when display_name is absent', () {
      final d = DeviceInfo.fromJson({'id': 3, 'code': '003', 'label': '前台卡'});
      expect(d.displayName, '前台卡');
      expect(d.isOnline, isFalse);
    });
  });

  group('CallSession', () {
    test('parses an active outbound call', () {
      final c = CallSession.fromJson({
        'call_id': 'c-1',
        'device_id': 2,
        'direction': 'outbound',
        'remote': '+8613800138000',
        'state': 'active',
        'created_at': 1700000000,
        'answered_at': 1700000010,
        'ended_at': null,
        'end_reason': null,
        'duration': 35,
        'tts_log': [
          {'text': '你好', 'mode': 1}
        ],
      });
      expect(c.state, CallState.active);
      expect(c.duration, 35);
      expect(c.isEnded, isFalse);
      expect(c.isInbound, isFalse);
      expect(c.ttsLog, hasLength(1));
    });

    test('copyWith keeps identity fields', () {
      final c = CallSession.fromJson({
        'call_id': 'c-2',
        'device_id': 1,
        'direction': 'inbound',
        'state': 'incoming',
      });
      final next = c.copyWith(state: CallState.active, answeredAt: 99);
      expect(next.callId, 'c-2');
      expect(next.state, CallState.active);
      expect(next.answeredAt, 99);
      expect(next.isInbound, isTrue);
    });
  });

  test('CallState.isLive covers every non terminal state', () {
    expect(CallState.isLive(CallState.dialing), isTrue);
    expect(CallState.isLive(CallState.alerting), isTrue);
    expect(CallState.isLive(CallState.incoming), isTrue);
    expect(CallState.isLive(CallState.active), isTrue);
    expect(CallState.isLive(CallState.held), isTrue);
    expect(CallState.isLive(CallState.ended), isFalse);
    expect(CallState.isLive(CallState.idle), isFalse);
  });

  group('Conversation and SmsMessage', () {
    test('parses a conversation row', () {
      final c = Conversation.fromJson({
        'peer_key': '8613800138000',
        'peer': '+8613800138000',
        'name': '张三',
        'last_content': '在吗',
        'last_at': 1700000000,
        'last_direction': 'in',
        'unread': 2,
        'draft': '稍等',
        'message_count': 5,
        'device_id': 1,
      });
      expect(c.title, '张三');
      expect(c.unread, 2);
      expect(c.draft, '稍等');
      expect(c.messageCount, 5);
    });

    test('title falls back to the number when there is no contact name', () {
      final c = Conversation.fromJson({
        'peer_key': 'k',
        'peer': '+8613800138000',
        'name': null,
        'last_content': '',
        'last_at': 0,
        'last_direction': 'out',
        'unread': 0,
        'message_count': 0,
      });
      expect(c.title, '+8613800138000');
    });

    test('parses a message and flags direction correctly', () {
      final inbound = SmsMessage.fromJson({
        'id': 11,
        'direction': 'in',
        'peer': '+8613800138000',
        'content': '你好',
        'at': 1700000000,
        'device_id': 1,
        'failed': false,
      });
      expect(inbound.isInbound, isTrue);
      expect(inbound.isPending, isFalse);

      final pending = SmsMessage.fromJson({
        'id': 12,
        'direction': 'out_pending',
        'peer': '+8613800138000',
        'content': 'hi',
        'at': 1700000001,
      });
      expect(pending.isInbound, isFalse);
      expect(pending.isPending, isTrue);
    });
  });

  group('AudioState', () {
    test('parses the sound card lists from /app/audio/devices', () {
      final a = AudioState.fromJson({
        'backend': 'alsa',
        'volume': 0.8,
        'output_device': '',
        'input_device': '',
        'outputs': [
          {
            'backend': 'alsa',
            'kind': 'output',
            'id': 'plughw:0,0',
            'name': 'HDA Intel PCH · ALC887-VD Analog',
            'description': 'card 0 device 0',
            'state': 'available',
            'is_default': true,
            'is_monitor': false,
          },
          {
            'backend': 'alsa',
            'kind': 'output',
            'id': 'plughw:1,3',
            'name': 'HDA NVidia · HDMI 0',
            'is_default': false,
          },
        ],
        'inputs': [
          {'kind': 'input', 'id': 'plughw:2,0', 'name': 'A7670X Audio', 'is_default': true},
        ],
      });
      expect(a.backend, 'alsa');
      expect(a.volume, 0.8);
      expect(a.outputs, hasLength(2));
      expect(a.outputs.first.isDefault, isTrue);
      expect(a.inputs.single.name, 'A7670X Audio');
    });
  });

  group('Contact', () {
    test('round trips through toJson for the create and update bodies', () {
      final c = Contact(
          id: 4, name: '李四', number: '+8613900139000', groupName: '客户', favorite: true);
      final json = c.toJson();
      expect(json['name'], '李四');
      expect(json['number'], '+8613900139000');
      expect(json['group_name'], '客户');
      expect(json['favorite'], isTrue);
      // The id is assigned by the server and must not be posted back.
      expect(json.containsKey('id'), isFalse);
      expect(c.initial, '李');
    });
  });

  test('UserProfile exposes role and display label', () {
    final u = UserProfile.fromJson({
      'id': 1,
      'username': 'appdev',
      'role': 'operator',
      'display_name': '',
      'is_active': true,
      'totp_enabled': false,
    });
    expect(u.isAdmin, isFalse);
    expect(u.label, 'appdev');
  });

  group('CallRecord', () {
    test('exposes the recording path published by the gateway', () {
      final r = CallRecord.fromJson(const {
        'call_id': 'c-9',
        'device_id': 3,
        'direction': 'outbound',
        'state': 'ended',
        'remote': '15331986287',
        'duration': 27,
        'recording_path': '/var/lib/sms-gateway/recordings/c-9.wav',
      });
      expect(r.hasRecording, isTrue);
      expect(r.recordingPath, endsWith('c-9.wav'));
    });

    test('calls without a capture report no recording', () {
      final r = CallRecord.fromJson(const {
        'call_id': 'c-10',
        'device_id': 3,
        'direction': 'inbound',
        'state': 'ended',
        'recording_path': null,
      });
      expect(r.hasRecording, isFalse);
    });

    test('the call log says whether the far end was told', () {
      final silent = CallRecord.fromJson(const {
        'call_id': 'c-11',
        'device_id': 3,
        'direction': 'outbound',
        'state': 'ended',
        'recording_path': '/var/lib/sms-gateway/recordings/c-11.wav',
      });
      expect(silent.recordingLabel, '已录音 · 静默');

      final told = CallRecord.fromJson(const {
        'call_id': 'c-12',
        'device_id': 3,
        'direction': 'outbound',
        'state': 'ended',
        'recording_path': '/var/lib/sms-gateway/recordings/c-12.wav',
        'recording_notice': '本次通话已开启录音',
        'recording_notice_ok': true,
      });
      expect(told.recordingLabel, '已录音 · 已告知对方');

      final failed = CallRecord.fromJson(const {
        'call_id': 'c-13',
        'device_id': 3,
        'direction': 'outbound',
        'state': 'ended',
        'recording_path': '/var/lib/sms-gateway/recordings/c-13.wav',
        'recording_notice': '本次通话已开启录音',
        'recording_notice_ok': false,
      });
      expect(failed.recordingLabel, '已录音 · 提示未成功播放');

      final off = CallRecord.fromJson(const {
        'call_id': 'c-14',
        'device_id': 3,
        'direction': 'outbound',
        'state': 'ended',
        'recording_reason': 'policy_disabled',
      });
      expect(off.recordingLabel, '策略关闭');
    });
  });

  group('CallRecording', () {
    test('the status payload carries the policy and whether it may change', () {
      final rec = CallRecording.fromJson('c-20', const {
        'call_id': 'c-20',
        'recording': true,
        'live': true,
        'policy': 'optional',
        'capture': {
          'call_id': 'c-20',
          'recording': true,
          'record': true,
          'announce': false,
          'can_change': true,
          'reason': 'operator_enabled',
          'path': '/var/lib/sms-gateway/recordings/c-20.wav',
        },
        'decision': {
          'record': true,
          'announce': false,
          'can_change': true,
          'reason': 'operator_enabled',
        },
      });
      expect(rec.recording, isTrue);
      expect(rec.canChange, isTrue);
      expect(rec.policyLabel, '可选择录音');
      expect(rec.reasonLabel, '手动开启');
      expect(rec.path, endsWith('c-20.wav'));
    });

    test('a forced policy says the switch will be refused', () {
      final rec = CallRecording.fromJson('c-21', const {
        'call_id': 'c-21',
        'policy': 'forced',
        'capture': {'recording': true, 'can_change': false, 'reason': 'policy_forced'},
        'decision': {'record': true, 'announce': false, 'can_change': false, 'reason': 'policy_forced'},
      });
      expect(rec.canChange, isFalse);
      expect(rec.forced, isTrue);
      expect(rec.policyLabel, '强制录音');
      expect(rec.reasonLabel, '后台策略要求全程录音');
    });

    test('the flat websocket frame derives the policy from can_change', () {
      // call.recording publishes the capture dict on its own, with no policy
      // name, and it must not turn a mandated capture into a switchable one.
      final rec = CallRecording.fromJson('c-22', const {
        'call_id': 'c-22',
        'device_id': 3,
        'recording': true,
        'record': true,
        'announce': false,
        'can_change': false,
        'reason': 'policy_forced',
        'phase': 'started',
      });
      expect(rec.policy, 'forced');
      expect(rec.canChange, isFalse);
      expect(rec.recording, isTrue);
    });

    test('a later frame keeps the fields an earlier response supplied', () {
      final first = CallRecording.fromJson('c-23', const {
        'call_id': 'c-23',
        'policy': 'optional',
        'decision': {'can_change': true, 'record': false, 'reason': 'policy_default'},
      });
      final second = CallRecording.fromJson(
        'c-23',
        const {
          'call_id': 'c-23',
          'recording': true,
          'record': true,
          'can_change': true,
          'reason': 'operator_enabled',
          'notice': {'text': '录音已开启，接下来的通话将被记录', 'ok': true, 'mid_call': true},
          'phase': 'announced',
        },
        fallback: first,
      );
      expect(second.recording, isTrue);
      expect(second.policy, 'optional', reason: 'the frame carries no policy name');
      expect(second.noticeText, '录音已开启，接下来的通话将被记录');
      expect(second.announced, isTrue);
      // The sentence tells the far end recording started and claims nothing else.
      expect(second.noticeText, isNot(contains('证词')));
    });

    test('a capture that could not run says why', () {
      final rec = CallRecording.fromJson('c-24', const {
        'call_id': 'c-24',
        'recording': false,
        'record': true,
        'can_change': false,
        'reason': 'policy_forced',
        'failed_reason': 'no_input_device',
      });
      expect(rec.recording, isFalse);
      expect(rec.failedLabel, '服务器没有可用的录音输入设备');
    });
  });
}


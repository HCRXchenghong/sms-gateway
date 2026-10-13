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
}


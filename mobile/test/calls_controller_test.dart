import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/data/models.dart';
import 'package:voicesms_app/src/data/realtime_client.dart';
import 'package:voicesms_app/src/state/controllers.dart';

/// The call controller is the piece that turns raw gateway events into what the
/// user sees, so these tests drive it with the exact payloads the server emits.
ApiClient stubApi(Map<String, dynamic> Function(String path, String body) responder) {
  final mock = MockClient((request) async {
    return http.Response(
      jsonEncode(responder(request.url.path, request.body)),
      200,
      headers: {'content-type': 'application/json'},
    );
  });
  return ApiClient(baseUrl: 'http://gw.test', httpClient: mock)..token = 't';
}

RealtimeEvent event(String name, Map<String, dynamic> data) => RealtimeEvent(name, data);

void main() {
  test('an incoming call event puts a live session on screen', () {
    final calls = CallsController(stubApi((p, b) => {'items': []}));
    var notified = 0;
    calls.addListener(() => notified++);

    calls.applyEvent(event('call.incoming', {
      'call_id': 'c-1',
      'device_id': 1,
      'direction': 'inbound',
      'remote': '+8613800138000',
      'state': 'incoming',
    }));

    expect(calls.hasActive, isTrue);
    expect(calls.foreground?.state, CallState.incoming);
    expect(calls.foreground?.remote, '+8613800138000');
    expect(calls.foreground?.isInbound, isTrue);
    expect(notified, greaterThan(0));
  });

  test('call.state replaces the session instead of duplicating it', () {
    final calls = CallsController(stubApi((p, b) => {'items': []}));

    calls.applyEvent(event('call.state', {
      'session': {
        'call_id': 'c-2',
        'device_id': 1,
        'direction': 'outbound',
        'state': 'dialing',
        'remote': '10086',
      }
    }));
    expect(calls.active, hasLength(1));

    calls.applyEvent(event('call.state', {
      'session': {
        'call_id': 'c-2',
        'device_id': 1,
        'direction': 'outbound',
        'state': 'active',
        'remote': '10086',
        'answered_at': 1700000000,
      }
    }));

    expect(calls.active, hasLength(1));
    expect(calls.active.single.state, CallState.active);
    expect(calls.forDevice(1)?.state, CallState.active);
  });

  test('call.ended clears the session and drops the notice', () {
    final calls = CallsController(stubApi((p, b) => {'items': []}));
    calls.setNotice('正在接通，请稍候');

    calls.applyEvent(event('call.ring', {
      'call_id': 'c-3',
      'device_id': 2,
      'direction': 'outbound',
      'state': 'alerting',
    }));
    expect(calls.hasActive, isTrue);

    calls.applyEvent(event('call.ended', {
      'call_id': 'c-3',
      'device_id': 2,
      'direction': 'outbound',
      'state': 'ended',
      'end_reason': 'busy',
    }));

    expect(calls.hasActive, isFalse);
    expect(calls.notice, isNull);
  });

  test('a prompt event becomes the operator facing Chinese notice', () {
    final calls = CallsController(stubApi((p, b) => {'items': []}));

    calls.applyEvent(event('call.prompt', {
      'device_id': 1,
      'prompt': 'dial_connecting',
      'state': 'dialing',
    }));
    expect(calls.notice, '正在接通，请稍候');

    calls.applyEvent(event('call.prompt', {
      'device_id': 1,
      'prompt': 'call_busy',
      'state': 'ended',
    }));
    expect(calls.notice, '您拨打的电话正忙，请稍后再拨');

    // An empty prompt id means the state machine asked for silence.
    calls.applyEvent(event('call.prompt', {'device_id': 1, 'prompt': '', 'state': 'idle'}));
    expect(calls.notice, isNull);
  });

  test('every prompt id in the gateway library has a label', () {
    for (final id in const [
      'dial_connecting',
      'ringback',
      'incoming_call',
      'call_busy',
      'call_no_answer',
      'call_invalid_number',
      'call_ended',
    ]) {
      expect(CallsController.describePrompt(id, null), isNotNull, reason: id);
      expect(CallsController.describePrompt(id, null), isNotEmpty, reason: id);
    }
  });

  test('dial posts to the gateway and tracks the returned session', () async {
    final seen = <String>[];
    final calls = CallsController(stubApi((path, body) {
      seen.add(path + ' ' + body);
      if (path == '/app/voice/dial') {
        return {
          'session': {
            'call_id': 'c-9',
            'device_id': 1,
            'direction': 'outbound',
            'state': 'dialing',
            'remote': '+8613800138000',
          }
        };
      }
      return {'items': []};
    }));

    final session = await calls.dial(number: '+8613800138000', deviceId: 1);

    expect(session, isNotNull);
    expect(session!.callId, 'c-9');
    expect(calls.active, hasLength(1));
    expect(seen.single, contains('/app/voice/dial'));
    expect(seen.single, contains('+8613800138000'));
  });

  test('refresh reads voice state including the enabled flag', () async {
    final calls = CallsController(stubApi((path, body) {
      if (path == '/app/voice/state') {
        return {
          'voice_enabled': true,
          'max_concurrent': 4,
          'online_device_ids': [1, 2],
          'active': [
            {
              'call_id': 'c-5',
              'device_id': 2,
              'direction': 'inbound',
              'state': 'active',
              'remote': '10010',
            }
          ],
          'count': 1,
        };
      }
      return {'items': []};
    }));

    await calls.refresh();

    expect(calls.voiceEnabled, isTrue);
    expect(calls.maxConcurrent, 4);
    expect(calls.active.single.remote, '10010');
    expect(calls.forDevice(2), isNotNull);
    expect(calls.forDevice(1), isNull);
  });

  test('an API failure is recorded on the controller, not thrown at the UI',
      () async {
    final mock = MockClient(
        (request) async => http.Response(jsonEncode({'detail': 'no online device'}), 409));
    final calls = CallsController(
        ApiClient(baseUrl: 'http://gw.test', httpClient: mock)..token = 't');

    final session = await calls.dial(number: '123');

    expect(session, isNull);
    expect(calls.error, isNotNull);
    expect(calls.hasActive, isFalse);
  });

  group('a call the gateway no longer lists as live', () {
    /// The far end hangs up while our phone is still ringing and the
    /// call.ended frame never arrives — the socket is reconnecting at exactly
    /// that moment. All the app gets is the next /app/voice/state poll, where
    /// the call is simply absent.
    CallsController controller(bool Function() live, {Map<String, dynamic>? detail}) {
      return CallsController(stubApi((path, body) {
        if (path == '/app/voice/state') {
          return {
            'voice_enabled': true,
            'max_concurrent': 4,
            'online_device_ids': [3],
            'active': live()
                ? [
                    {
                      'call_id': 'c-30',
                      'device_id': 3,
                      'direction': 'outbound',
                      'state': 'alerting',
                      'remote': '15331986287',
                    }
                  ]
                : [],
            'count': live() ? 1 : 0,
            'recording': [],
            'recording_policy': 'optional',
          };
        }
        if (path == '/app/voice/calls/c-30' && detail != null) return detail;
        return {'items': []};
      }));
    }

    test('is booked as ended by the next poll', () async {
      var live = true;
      final calls = controller(() => live);
      calls.setNotice('请稍候，对方正在接听');

      await calls.refresh();
      expect(calls.registry['c-30']?.state, CallState.alerting);

      live = false;
      await calls.refresh();

      expect(calls.active, isEmpty);
      expect(calls.registry['c-30']?.isEnded, isTrue,
          reason: 'the in-call screen reads this and would stay on 对方振铃中');
      expect(calls.notice, isNull);
    });

    test('takes the end reason from the call detail route', () async {
      var live = true;
      final calls = controller(
        () => live,
        detail: {
          'call_id': 'c-30',
          'live': false,
          'call': {
            'call_id': 'c-30',
            'device_id': 3,
            'direction': 'outbound',
            'remote': '15331986287',
            'state': 'ended',
            'end_reason': 'remote_hangup',
            'answered_at': null,
            'ended_at': 1792027780,
          },
        },
      );

      await calls.refresh();
      live = false;
      await calls.refresh();
      expect(calls.registry['c-30']?.endReason, isNull,
          reason: 'the live list only says the call is gone, not who ended it');

      await calls.fetchCallDetail('c-30');

      expect(calls.registry['c-30']?.isEnded, isTrue);
      expect(calls.registry['c-30']?.endReason, 'remote_hangup');
    });

    test('the persisted row corrects a stale live session', () async {
      final calls = CallsController(stubApi((path, body) {
        if (path == '/app/voice/state') {
          return {
            'voice_enabled': true,
            'max_concurrent': 4,
            'active': [],
            'count': 0,
            'recording': [],
          };
        }
        if (path == '/app/voice/history') {
          return {
            'items': [
              {
                'call_id': 'c-31',
                'device_id': 3,
                'direction': 'outbound',
                'remote': '15331986287',
                'state': 'ended',
                'end_reason': 'remote_hangup',
                'ended_at': 1792027780,
              }
            ],
            'count': 1,
          };
        }
        return {'items': []};
      }));
      calls.applyEvent(event('call.ring', {
        'call_id': 'c-31',
        'device_id': 3,
        'direction': 'outbound',
        'state': 'alerting',
      }));
      expect(calls.registry['c-31']?.state, CallState.alerting);

      await calls.refreshHistory();

      expect(calls.registry['c-31']?.isEnded, isTrue);
      expect(calls.registry['c-31']?.endReason, 'remote_hangup');
    });
  });

  group('recording', () {
    test('follows the gateway snapshot on every poll', () async {
      final calls = CallsController(stubApi((path, body) {
        if (path == '/app/voice/state') {
          return {
            'voice_enabled': true,
            'max_concurrent': 4,
            'active': [
              {
                'call_id': 'c-40',
                'device_id': 3,
                'direction': 'outbound',
                'state': 'active',
                'recording': true,
                'recording_detail': {
                  'call_id': 'c-40',
                  'recording': true,
                  'record': true,
                  'announce': true,
                  'can_change': true,
                  'reason': 'operator_enabled',
                }
              }
            ],
            'count': 1,
            'recording': [
              {
                'call_id': 'c-40',
                'recording': true,
                'record': true,
                'announce': true,
                'can_change': true,
                'reason': 'operator_enabled',
              }
            ],
            'recording_call_ids': ['c-40'],
            'recording_policy': 'optional',
          };
        }
        return {'items': []};
      }));

      await calls.refresh();

      expect(calls.isRecording('c-40'), isTrue);
      expect(calls.recordingFor('c-40')?.canChange, isTrue);
      expect(calls.recordingFor('c-40')?.policyLabel, '可选择录音');
      expect(calls.recordingFor('c-40')?.reasonLabel, '手动开启');
    });

    test('a websocket frame moves the marker without waiting for a poll', () {
      final calls = CallsController(stubApi((p, b) => {'items': []}));

      calls.applyEvent(event('call.recording', {
        'call_id': 'c-41',
        'device_id': 3,
        'recording': true,
        'record': true,
        'announce': false,
        'can_change': true,
        'reason': 'operator_enabled',
        'phase': 'started',
      }));
      expect(calls.isRecording('c-41'), isTrue);

      calls.applyEvent(event('call.recording', {
        'call_id': 'c-41',
        'device_id': 3,
        'recording': false,
        'record': false,
        'announce': false,
        'can_change': true,
        'reason': 'operator_disabled',
        'phase': 'stopped',
        'size': 48044,
      }));
      expect(calls.isRecording('c-41'), isFalse);
      expect(calls.recordingFor('c-41')?.reasonLabel, '手动关闭');
    });

    test('the switch sends the announce choice and absorbs the new status', () async {
      final seen = <String>[];
      final calls = CallsController(stubApi((path, body) {
        seen.add(path + ' ' + body);
        if (path == '/app/voice/calls/c-42/recording') {
          return {
            'ok': true,
            'recording': true,
            'status': {
              'call_id': 'c-42',
              'recording': true,
              'policy': 'optional',
              'capture': {
                'call_id': 'c-42',
                'recording': true,
                'record': true,
                'announce': true,
                'can_change': true,
                'reason': 'operator_enabled',
                'notice': {
                  'text': '录音已开启，接下来的通话将被记录',
                  'ok': true,
                  'mid_call': true,
                },
              },
              'decision': {
                'record': true,
                'announce': true,
                'can_change': true,
                'reason': 'operator_enabled',
              },
            },
          };
        }
        return {'items': []};
      }));

      final ok = await calls.setRecording('c-42', true, announce: true);

      expect(ok, isTrue);
      expect(seen.single, contains('"enabled":true'));
      expect(seen.single, contains('"announce":true'));
      expect(calls.isRecording('c-42'), isTrue);
      expect(calls.recordingFor('c-42')?.announced, isTrue);
      expect(calls.recordingFor('c-42')?.noticeText, '录音已开启，接下来的通话将被记录');
    });

    test('a policy that refuses the switch reports why', () async {
      final mock = MockClient((request) async => http.Response(
          jsonEncode({
            'detail': {
              'error': {'code': 'recording_policy', 'message': '当前录音策略不允许修改'}
            }
          }),
          409,
          headers: {'content-type': 'application/json'}));
      final calls = CallsController(
          ApiClient(baseUrl: 'http://gw.test', httpClient: mock)..token = 't');

      final ok = await calls.setRecording('c-43', false);

      expect(ok, isFalse);
      expect(calls.error, '当前录音策略不允许修改');
    });
  });
}


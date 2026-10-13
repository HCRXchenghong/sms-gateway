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
}


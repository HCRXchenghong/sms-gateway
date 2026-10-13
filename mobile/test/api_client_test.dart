import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/data/models.dart';

/// Recorded request, so assertions can check method, path and headers.
class Recorded {
  Recorded(this.method, this.url, this.headers, this.body);
  final String method;
  final Uri url;
  final Map<String, String> headers;
  final String body;
}

ApiClient buildClient(
  Map<String, dynamic> Function(String path, String body) responder, {
  List<Recorded>? recorded,
}) {
  final mock = MockClient((request) async {
    final body = request.body;
    recorded?.add(Recorded(request.method, request.url, request.headers, body));
    return http.Response(
      jsonEncode(responder(request.url.path, body)),
      200,
      headers: {'content-type': 'application/json'},
    );
  });
  return ApiClient(baseUrl: 'http://gw.test:8443', httpClient: mock);
}

void main() {
  group('ApiClient', () {
    test('login stores the bearer token and parses the user payload', () async {
      final recorded = <Recorded>[];
      final client = buildClient((path, body) {
        if (path == '/api/v1/auth/login') {
          return {
            'token': 'sid-123',
            'expires_in': 28800,
            'must_change_password': false,
            'user': {
              'id': 7,
              'username': 'appdev',
              'role': 'admin',
              'display_name': 'App 开发账号',
              'is_active': true,
            },
            'server': {'name': 'VoiceSMS Gateway', 'voice_enabled': true},
          };
        }
        return {};
      }, recorded: recorded);

      final result = await client.login(username: 'appdev', password: 'pw');

      expect(result.token, 'sid-123');
      expect(result.user.username, 'appdev');
      expect(result.user.isAdmin, isTrue);
      expect(result.user.label, 'App 开发账号');
      expect(client.token, 'sid-123');

      final sent = jsonDecode(recorded.single.body) as Map<String, dynamic>;
      expect(sent['username'], 'appdev');
      expect(sent['password'], 'pw');
      // TOTP fields are omitted unless the user filled them in.
      expect(sent.containsKey('code'), isFalse);
    });

    test('every authenticated call sends Authorization: Bearer', () async {
      final recorded = <Recorded>[];
      final client = buildClient((path, body) => {'items': []}, recorded: recorded)
        ..token = 'sid-abc';

      await client.devices();

      expect(recorded.single.headers['Authorization'], 'Bearer sid-abc');
      expect(recorded.single.url.path, '/app/devices');
    });

    test('dial posts the number and optional device id', () async {
      final recorded = <Recorded>[];
      final client = buildClient((path, body) {
        return {
          'session': {
            'call_id': 'call-1',
            'device_id': 2,
            'direction': 'outbound',
            'state': 'dialing',
            'remote': '+8613800138000',
          }
        };
      }, recorded: recorded)..token = 't';

      final session = await client.dial(number: '+8613800138000', deviceId: 2);

      expect(session.callId, 'call-1');
      expect(session.state, CallState.dialing);
      expect(session.deviceId, 2);
      expect(session.isInbound, isFalse);

      final sent = jsonDecode(recorded.single.body) as Map<String, dynamic>;
      expect(sent['number'], '+8613800138000');
      expect(sent['device_id'], 2);
    });

    test('sending SMS maps to /app/send with the gateway field names', () async {
      final recorded = <Recorded>[];
      final client = buildClient((path, body) => {'ok': true}, recorded: recorded)
        ..token = 't';

      await client.sendSms(to: '+8613800138000', content: '你好', deviceId: 1);

      expect(recorded.single.url.path, '/app/send');
      final sent = jsonDecode(recorded.single.body) as Map<String, dynamic>;
      expect(sent['to'], '+8613800138000');
      expect(sent['content'], '你好');
      expect(sent['device_id'], 1);
    });

    test('a 401 fires onUnauthorized so the UI can force re-login', () async {
      var fired = false;
      final mock = MockClient((request) async => http.Response('{"detail":"expired"}', 401));
      final client = ApiClient(baseUrl: 'http://gw.test', httpClient: mock)
        ..token = 'stale'
        ..onUnauthorized = () => fired = true;

      await expectLater(client.devices(), throwsA(isA<ApiException>()));
      expect(fired, isTrue);
    });

    test('an error body is surfaced as a readable message', () async {
      final mock = MockClient((request) async => http.Response(
          jsonEncode({
            'error': {'code': 'no_online_device', 'message': '没有在线设备'}
          }),
          409,
          headers: {'content-type': 'application/json; charset=utf-8'}));
      final client = ApiClient(baseUrl: 'http://gw.test', httpClient: mock)..token = 't';

      try {
        await client.dial(number: '123');
        fail('expected an ApiException');
      } on ApiException catch (e) {
        expect(e.statusCode, 409);
        expect(e.code, 'no_online_device');
        expect(e.message, '没有在线设备');
        expect(e.isAuthError, isFalse);
      }
    });

    test('query parameters are appended and null values dropped', () async {
      final recorded = <Recorded>[];
      final client = buildClient((path, body) => {'items': []}, recorded: recorded)
        ..token = 't';

      await client.conversations(limit: 20, deviceId: null, q: 'abc');

      final uri = recorded.single.url;
      expect(uri.queryParameters['limit'], '20');
      expect(uri.queryParameters['q'], 'abc');
      expect(uri.queryParameters.containsKey('device_id'), isFalse);
    });

    test('a trailing slash in the server address does not double up', () async {
      Uri? seen;
      final mock = MockClient((request) async {
        seen = request.url;
        return http.Response('{"items":[]}', 200);
      });
      final client = ApiClient(baseUrl: 'http://gw.test:8443/', httpClient: mock)
        ..token = 't';

      await client.devices();

      expect(seen.toString(), 'http://gw.test:8443/app/devices');
    });
  });
}


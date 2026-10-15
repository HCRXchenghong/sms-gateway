import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/data/realtime_client.dart';
import 'package:voicesms_app/src/state/app_scope.dart';
import 'package:voicesms_app/src/ui/in_call_page.dart';

/// The in-call screen driven by the payloads the gateway actually serves.
///
/// What is being pinned down here is the reported failure: the far end hangs up
/// while our phone is still ringing, the call.ended frame is missed, and the
/// screen keeps showing 对方振铃中 until the user gives up and presses hangup
/// themselves. The screen has periodic timers, so every step advances the fake
/// clock explicitly instead of waiting for the tree to settle.
const _callId = 'c-50';

Map<String, dynamic> _session(String state, {String? endReason}) => {
      'call_id': _callId,
      'device_id': 3,
      'direction': 'outbound',
      'remote': '15331986287',
      'state': state,
      'created_at': 1792027774,
      'ringing_at': state == 'alerting' || state == 'ended' ? 1792027776 : null,
      'answered_at': null,
      'ended_at': state == 'ended' ? 1792027780 : null,
      'end_reason': endReason,
    };

void main() {
  testWidgets('a hangup during ringing closes the screen and says who ended it',
      (tester) async {
    var live = true;
    final scope = AppScope(
      api: ApiClient(
        httpClient: MockClient((request) async {
          final path = request.url.path;
          final Object body;
          if (path == '/app/voice/state') {
            body = {
              'voice_enabled': true,
              'max_concurrent': 4,
              'online_device_ids': [3],
              // The finished call is simply absent, which is all a poll can say.
              'active': live ? [_session('alerting')] : [],
              'count': live ? 1 : 0,
              'recording': [],
              'recording_call_ids': [],
              'recording_policy': 'optional',
            };
          } else if (path == '/app/voice/calls/$_callId') {
            body = {
              'call_id': _callId,
              'live': false,
              'call': _session('ended', endReason: 'remote_hangup'),
            };
          } else if (path == '/app/voice/calls/$_callId/recording/state') {
            body = {
              'call_id': _callId,
              'recording': false,
              'live': live,
              'policy': 'optional',
              'decision': {
                'record': false,
                'announce': false,
                'can_change': true,
                'reason': 'policy_default',
              },
            };
          } else {
            body = {'items': []};
          }
          return http.Response(jsonEncode(body), 200,
              headers: {'content-type': 'application/json'});
        }),
      )..token = 't',
    );
    scope.calls.applyEvent(
        RealtimeEvent('call.state', {'session': _session('alerting')}));

    var popped = false;
    await tester.pumpWidget(ChangeNotifierProvider.value(
      value: scope,
      child: MaterialApp(
        home: Builder(
          builder: (context) => Scaffold(
            body: Center(
              child: ElevatedButton(
                onPressed: () => Navigator.of(context)
                    .push<void>(MaterialPageRoute<void>(
                        builder: (_) => const InCallPage(callId: _callId)))
                    .then((_) => popped = true),
                child: const Text('open'),
              ),
            ),
          ),
        ),
      ),
    ));
    await tester.tap(find.text('open'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));

    expect(find.text('对方振铃中…'), findsOneWidget);
    // Capture starts when both ends are connected, so during ringing the
    // control says when it becomes available instead of offering a dead tap.
    expect(find.text('接通后可录音'), findsOneWidget,
        reason: 'the recording control is on the call screen, not in a menu');

    // 对方挂断. No websocket frame is delivered, so the poll has to catch it.
    live = false;
    await tester.pump(const Duration(seconds: 2));
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 100));

    expect(find.text('对方振铃中…'), findsNothing);
    expect(find.text('通话已结束'), findsOneWidget);
    expect(find.text('对方挂断'), findsOneWidget);
    expect(popped, isFalse, reason: 'the reason is shown before the screen goes');

    await tester.pump(const Duration(seconds: 2));
    expect(popped, isTrue);
    // Let the pop animation run out before checking the tree is clear.
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.text('通话已结束'), findsNothing);

    await tester.pumpWidget(const SizedBox());
    scope.dispose();
  });

  testWidgets('the recording sheet refuses to switch a forced policy off',
      (tester) async {
    final scope = AppScope(
      api: ApiClient(
        httpClient: MockClient((request) async {
          final Object body;
          if (request.url.path == '/app/voice/calls/$_callId/recording/state') {
            body = {
              'call_id': _callId,
              'recording': true,
              'live': true,
              'policy': 'forced',
              'capture': {
                'call_id': _callId,
                'recording': true,
                'record': true,
                'announce': false,
                'can_change': false,
                'reason': 'policy_forced',
              },
              'decision': {
                'record': true,
                'announce': false,
                'can_change': false,
                'reason': 'policy_forced',
              },
            };
          } else if (request.url.path == '/app/voice/state') {
            body = {
              'voice_enabled': true,
              'max_concurrent': 4,
              'active': [_session('active')],
              'count': 1,
              'recording': [],
              'recording_policy': 'forced',
            };
          } else {
            body = {'items': []};
          }
          return http.Response(jsonEncode(body), 200,
              headers: {'content-type': 'application/json'});
        }),
      )..token = 't',
    );
    scope.calls.applyEvent(RealtimeEvent('call.state', {'session': _session('active')}));

    await tester.pumpWidget(ChangeNotifierProvider.value(
      value: scope,
      child: const MaterialApp(home: InCallPage(callId: _callId)),
    ));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(milliseconds: 200));

    expect(find.text('正在录音'), findsOneWidget);

    await tester.tap(find.text('正在录音'));
    // No pumpAndSettle: the page polls on a periodic timer, so the tree never
    // goes quiet. Advance past the sheet animation by hand instead.
    await tester.pump(const Duration(milliseconds: 100));
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pump(const Duration(milliseconds: 300));

    expect(find.text('通话录音'), findsOneWidget);
    expect(find.text('这通电话正在录音 · 强制录音'), findsOneWidget);
    expect(find.byType(SwitchListTile), findsNothing,
        reason: 'a mandated capture cannot be turned off from a phone');
    expect(find.textContaining('强制录音'), findsWidgets);

    await tester.pumpWidget(const SizedBox());
    scope.dispose();
  });
}

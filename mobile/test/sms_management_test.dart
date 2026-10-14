import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/data/realtime_client.dart';
import 'package:voicesms_app/src/state/app_scope.dart';
import 'package:voicesms_app/src/ui/conversations_page.dart';
import 'package:voicesms_app/src/ui/home_page.dart';

class Fixture {
  final requests = <http.Request>[];
  final rows = <Map<String, dynamic>>[
    {
      'peer_key': '13800000001',
      'peer': '13800000001',
      'last_content': '第一条',
      'last_at': 1,
      'unread': 1,
    },
    {
      'peer_key': '13800000002',
      'peer': '13800000002',
      'last_content': '第二条',
      'last_at': 2,
      'unread': 0,
    },
  ];
  late final scope = AppScope(
    api: ApiClient(
      baseUrl: 'http://gw.test',
      httpClient: MockClient((r) async {
        requests.add(r);
        final path = r.url.path;
        dynamic response = {'ok': true};
        if (path == '/app/devices')
          response = {
            'items': [
              {'id': 1, 'display_name': '编号001', 'status': 'offline'},
              {'id': 2, 'display_name': '编号002', 'status': 'offline'},
              {'id': 3, 'display_name': '编号003', 'status': 'online'},
            ],
          };
        if (path == '/app/conversations')
          response = {
            'items': rows,
            'unread_total': rows.fold<int>(
              0,
              (n, c) => n + (c['unread'] as int),
            ),
          };
        if (path.endsWith('/messages')) {
          if (r.url.queryParameters['mark_read'] == 'true') {
            for (final c in rows) {
              if (path.contains(c['peer_key'])) c['unread'] = 0;
            }
          }
          response = {'items': []};
        }
        if (path.endsWith('/unread') || path.endsWith('/read')) {
          for (final c in rows) {
            if (path.contains(c['peer_key']))
              c['unread'] = path.endsWith('/unread') ? 1 : 0;
          }
        }
        if (path.endsWith('/bulk_delete')) {
          final peers = jsonDecode(r.body)['peers'] as List;
          rows.removeWhere((c) => peers.contains(c['peer_key']));
        }
        if (path.endsWith('/delete_read'))
          rows.removeWhere((c) => c['unread'] == 0);
        return http.Response(
          jsonEncode(response),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    ),
  );
}

Future<void> mount(WidgetTester tester, Fixture f, Widget page) async {
  tester.view.physicalSize = const Size(1080, 2400);
  tester.view.devicePixelRatio = 3;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    ChangeNotifierProvider.value(
      value: f.scope,
      child: MaterialApp(home: Scaffold(body: page)),
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> done(WidgetTester tester, Fixture f) async {
  await tester.pumpWidget(const SizedBox());
  f.scope.dispose();
}

void main() {
  testWidgets(
    'picker keeps offline selection and automatic mode really resets it',
    (tester) async {
      final f = Fixture();
      await f.scope.devices.refresh();
      await mount(tester, f, const HomePage());
      for (final id in [1, 2]) {
        await tester.tap(find.byTooltip('选择发送设备'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('编号00$id（离线）').last);
        await tester.pumpAndSettle();
        expect(f.scope.devices.effective?.id, id);
        expect(find.text('编号00$id（离线）'), findsOneWidget);
      }
      expect(await f.scope.dial('15331986287'), isNull);
      expect(f.scope.calls.error, contains('已离线'));
      expect(f.requests.where((r) => r.url.path == '/app/voice/dial'), isEmpty);
      await tester.tap(find.byTooltip('选择发送设备'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('自动选择在线设备'));
      await tester.pumpAndSettle();
      expect(f.scope.devices.preferredId, isNull);
      expect(f.scope.devices.effective?.id, 3);
      await done(tester, f);
    },
  );

  testWidgets(
    'tap reads, long press marks unread, closed threads stay unread on events',
    (tester) async {
      final f = Fixture();
      await mount(tester, f, const ConversationsPage());
      await tester.tap(find.text('13800000001'));
      await tester.pumpAndSettle();
      expect(f.scope.messages.unreadTotal, 0);
      await tester.pageBack();
      await tester.pumpAndSettle();
      await tester.longPress(find.text('13800000001'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('标为未读'));
      await tester.pumpAndSettle();
      expect(f.scope.messages.unreadTotal, 1);
      final count = f.requests
          .where((r) => r.url.path.endsWith('/messages'))
          .length;
      f.scope.handleEvent(
        RealtimeEvent('sms.conversations_changed', {
          'peers': ['13800000001'],
        }),
      );
      await tester.pumpAndSettle();
      expect(f.scope.messages.unreadTotal, 1);
      expect(
        f.requests.where((r) => r.url.path.endsWith('/messages')).length,
        count,
      );
      await done(tester, f);
    },
  );

  testWidgets(
    'delete read preserves unread and bulk select deletes selected only',
    (tester) async {
      final f = Fixture();
      await mount(tester, f, const ConversationsPage());
      await tester.tap(find.byTooltip('短信管理'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除已读'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除'));
      await tester.pumpAndSettle();
      expect(find.text('13800000001'), findsOneWidget);
      expect(find.text('13800000002'), findsNothing);
      await tester.longPress(find.text('13800000001'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('批量选择'));
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('删除所选'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('删除'));
      await tester.pumpAndSettle();
      expect(f.rows, isEmpty);
      expect(f.scope.messages.unreadTotal, 0);
      await done(tester, f);
    },
  );
}

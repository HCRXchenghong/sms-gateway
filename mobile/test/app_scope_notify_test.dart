import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/state/app_scope.dart';
import 'package:voicesms_app/src/ui/home_page.dart';

/// AppScope owns the controllers, but every screen watches AppScope itself.
/// These tests pin the forwarding: when a controller fills in data after the
/// first frame, widgets that watch the scope must rebuild and show it. The
/// device picker used to stay on 'no device' on a real phone for exactly this
/// reason while the API returned three healthy modules.
ApiClient stubApi() {
  final device = {
    'id': 3,
    'code': '003',
    'display_name': '编号003',
    'status': 'online',
    'iccid': '898602D51524FA216310',
    'imei': '867284068161142',
    'model': 'A7670G-LLSE',
    'at_port': '/dev/ttyUSB2',
  };
  final mock = MockClient((request) async {
    final body = request.url.path == '/app/devices'
        ? {'items': [device]}
        : <String, dynamic>{'items': []};
    return http.Response(jsonEncode(body), 200,
        headers: {'content-type': 'application/json'});
  });
  return ApiClient(baseUrl: 'http://gw.test', httpClient: mock)..token = 't';
}

void main() {
  test('a controller refresh notifies AppScope listeners', () async {
    final scope = AppScope(api: stubApi());
    var notified = 0;
    scope.addListener(() => notified++);

    await scope.devices.refresh();

    expect(scope.devices.items, hasLength(1));
    expect(scope.devices.effective?.id, 3);
    expect(notified, greaterThan(0));
    scope.dispose();
  });

  testWidgets('widgets watching AppScope rebuild when device data arrives',
      (tester) async {
    final scope = AppScope(api: stubApi());
    await tester.pumpWidget(
      ChangeNotifierProvider<AppScope>.value(
        value: scope,
        child: MaterialApp(
          home: Consumer<AppScope>(
            builder: (context, s, _) => Text(
              s.devices.effective?.displayName ?? '无设备',
              textDirection: TextDirection.ltr,
            ),
          ),
        ),
      ),
    );
    expect(find.text('无设备'), findsOneWidget);

    await scope.devices.refresh();
    await tester.pump();

    expect(find.text('编号003'), findsOneWidget);
    scope.dispose();
  });

  testWidgets('the home shell device picker fills in after login',
      (tester) async {
    // The dialer keypad needs a phone-sized surface; the 800x600 default
    // overflows the column and fails the test for layout reasons.
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final scope = AppScope(api: stubApi());
    await tester.pumpWidget(
      ChangeNotifierProvider<AppScope>.value(
        value: scope,
        child: const MaterialApp(home: HomePage()),
      ),
    );
    await tester.pump();
    expect(find.text('无设备'), findsOneWidget);

    await scope.devices.refresh();
    await tester.pump();

    expect(find.text('无设备'), findsNothing);
    expect(find.textContaining('编号003'), findsWidgets);
    scope.dispose();
    await tester.pumpAndSettle();
  });
}

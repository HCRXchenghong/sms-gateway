import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:voicesms_app/src/config/server_config.dart';
import 'package:voicesms_app/src/data/api_client.dart';
import 'package:voicesms_app/src/state/app_scope.dart';
import 'package:voicesms_app/src/ui/login_page.dart';

void main() {
  testWidgets(
    'login only shows account/password and uses compiled endpoint despite saved old address',
    (tester) async {
      SharedPreferences.setMockInitialValues({
        'smsgw.server_url': 'http://old.test',
      });
      Uri? called;
      final scope = AppScope(
        api: ApiClient(
          httpClient: MockClient((request) async {
            called = request.url;
            return http.Response(
              jsonEncode({'detail': '测试登录失败'}),
              401,
              headers: {'content-type': 'application/json; charset=utf-8'},
            );
          }),
        ),
      );
      await scope.store.init();
      await tester.pumpWidget(
        ChangeNotifierProvider.value(
          value: scope,
          child: const MaterialApp(home: LoginPage()),
        ),
      );
      expect(find.byType(TextFormField), findsNWidgets(2));
      expect(find.text('服务器地址'), findsNothing);
      expect(find.text(builtInServerUrl), findsNothing);
      await tester.tap(find.text('登录'));
      await tester.pump();
      expect(find.text('请填写账号'), findsOneWidget);
      expect(find.text('请填写密码'), findsOneWidget);
      await tester.enterText(find.byType(TextFormField).at(0), 'tester');
      await tester.enterText(find.byType(TextFormField).at(1), 'password');
      await tester.tap(find.text('登录'));
      await tester.pumpAndSettle();
      expect(called.toString(), '$builtInServerUrl/api/v1/auth/login');
      await tester.pumpWidget(const SizedBox());
      scope.dispose();
    },
  );

  test(
    'deployment change invalidates cached credentials before any request',
    () async {
      SharedPreferences.setMockInitialValues({
        'smsgw.server_url': 'http://old.test',
        'smsgw.token': 'old-secret',
        'smsgw.preferred_device_id': 2,
      });
      var requests = 0;
      final scope = AppScope(
        api: ApiClient(
          httpClient: MockClient((r) async {
            requests++;
            return http.Response('{}', 401);
          }),
        ),
      );
      expect(await scope.restore(), isFalse);
      expect(requests, 0);
      expect(scope.store.token, isNull);
      expect(scope.store.preferredDeviceId, isNull);
      expect(scope.store.serverUrl, builtInServerUrl);
      scope.dispose();
    },
  );
}

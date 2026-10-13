// This is a basic Flutter widget test.
//
// To perform an interaction with a widget in your test, use the WidgetTester
// utility in the flutter_test package. For example, you can send tap and scroll
// gestures. You can also use WidgetTester to find child widgets in the widget
// tree, read text, and verify that the values of widget properties are correct.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:provider/provider.dart';
import 'package:voicesms_app/src/state/app_scope.dart';
import 'package:voicesms_app/src/ui/login_page.dart';

void main() {
  testWidgets('login page renders the server, account and password fields',
      (tester) async {
    await tester.pumpWidget(
      ChangeNotifierProvider<AppScope>(
        create: (_) => AppScope(),
        child: const MaterialApp(home: LoginPage()),
      ),
    );
    await tester.pump();

    expect(find.text('VoiceSMS Gateway'), findsOneWidget);
    expect(find.text('登录'), findsOneWidget);
    // Server address, account and password. The TOTP field only appears after
    // a login attempt reports that two factor auth is required.
    expect(find.byType(TextFormField), findsNWidgets(3));

    // Submitting an empty form must surface validation instead of a request.
    await tester.tap(find.text('登录'));
    await tester.pump();
    expect(find.text('请填写服务器地址'), findsOneWidget);
    expect(find.text('请填写账号'), findsOneWidget);
    expect(find.text('请填写密码'), findsOneWidget);
  });

  testWidgets('login page rejects a malformed server address', (tester) async {
    await tester.pumpWidget(
      ChangeNotifierProvider<AppScope>(
        create: (_) => AppScope(),
        child: const MaterialApp(home: LoginPage()),
      ),
    );
    await tester.pump();

    await tester.enterText(find.byType(TextFormField).at(0), 'not a url');
    await tester.enterText(find.byType(TextFormField).at(1), 'admin');
    await tester.enterText(find.byType(TextFormField).at(2), 'secret');
    await tester.tap(find.text('登录'));
    await tester.pump();

    expect(find.text('地址格式不正确'), findsOneWidget);
  });
}

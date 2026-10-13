/// Root widget: restores the session, then routes to login or the home shell.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'config/app_theme.dart';
import 'state/app_scope.dart';
import 'ui/home_page.dart';
import 'ui/in_call_page.dart';
import 'ui/login_page.dart';

class GatewayApp extends StatefulWidget {
  const GatewayApp({super.key});

  @override
  State<GatewayApp> createState() => _GatewayAppState();
}

class _GatewayAppState extends State<GatewayApp> {
  @override
  void initState() {
    super.initState();
    // Kick off session restore after the first frame so the splash can paint.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) context.read<AppScope>().restore();
    });
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'VoiceSMS Gateway',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light(),
      darkTheme: AppTheme.dark(),
      themeMode: ThemeMode.system,
      home: const _RootGate(),
    );
  }
}

class _RootGate extends StatelessWidget {
  const _RootGate();

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    switch (scope.phase) {
      case AuthPhase.unknown:
        return const _Splash();
      case AuthPhase.loggedOut:
        return const LoginPage();
      case AuthPhase.loggedIn:
        return const _AuthedShell();
    }
  }
}

class _Splash extends StatelessWidget {
  const _Splash();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.cell_tower, size: 56),
            SizedBox(height: 16),
            Text('VoiceSMS Gateway'),
            SizedBox(height: 20),
            CircularProgressIndicator(),
          ],
        ),
      ),
    );
  }
}

/// Logged-in shell. A live incoming call floats the in-call screen on top so it
/// behaves like the system dialer regardless of which tab the user is on.
class _AuthedShell extends StatefulWidget {
  const _AuthedShell();

  @override
  State<_AuthedShell> createState() => _AuthedShellState();
}

class _AuthedShellState extends State<_AuthedShell> {
  bool _inCallRouteOpen = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _sync(context.read<AppScope>()));
  }

  void _sync(AppScope scope) {
    final call = scope.calls.foreground;
    final shouldShow = call != null && !call.isEnded;
    if (shouldShow && !_inCallRouteOpen) {
      _inCallRouteOpen = true;
      Navigator.of(context, rootNavigator: true)
          .push(MaterialPageRoute<void>(
            fullscreenDialog: true,
            builder: (_) => InCallPage(callId: call.callId),
          ))
          .then((_) {
        if (mounted) setState(() => _inCallRouteOpen = false);
      });
    } else if (!shouldShow && _inCallRouteOpen) {
      _inCallRouteOpen = false;
      Navigator.of(context, rootNavigator: true).popUntil((r) => r.isFirst);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Consumer<AppScope>(
      builder: (context, scope, _) {
        // Re-evaluate the call overlay whenever call state changes.
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted) _sync(scope);
        });
        return const HomePage();
      },
    );
  }
}


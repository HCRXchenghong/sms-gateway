import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import 'src/app.dart';
import 'src/state/app_scope.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const VoiceSmsApp());
}

class VoiceSmsApp extends StatelessWidget {
  const VoiceSmsApp({super.key});

  @override
  Widget build(BuildContext context) {
    return ChangeNotifierProvider<AppScope>(
      create: (_) => AppScope(),
      child: const GatewayApp(),
    );
  }
}


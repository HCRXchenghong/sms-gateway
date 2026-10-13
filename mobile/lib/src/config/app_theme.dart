/// App theme. Follows the Material 3 look used by stock Android dialer and
/// messages apps: dense lists, a green call action, round keypad buttons.
library;

import 'package:flutter/material.dart';

class AppTheme {
  static const Color call = Color(0xFF12B76A);
  static const Color callDark = Color(0xFF0E9F5C);
  static const Color hangup = Color(0xFFE5484D);
  static const Color bubbleIn = Color(0xFFE9EBEF);
  static const Color bubbleOut = Color(0xFFC7F0D8);
  static const Color seed = Color(0xFF2F6BFF);

  static ThemeData light() {
    final scheme = ColorScheme.fromSeed(seedColor: seed, brightness: Brightness.light);
    return _base(scheme);
  }

  static ThemeData dark() {
    final scheme = ColorScheme.fromSeed(seedColor: seed, brightness: Brightness.dark);
    return _base(scheme);
  }

  static ThemeData _base(ColorScheme scheme) {
    return ThemeData(
      useMaterial3: true,
      colorScheme: scheme,
      scaffoldBackgroundColor: scheme.surface,
      appBarTheme: AppBarTheme(
        backgroundColor: scheme.surface,
        foregroundColor: scheme.onSurface,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          color: scheme.onSurface,
          fontSize: 20,
          fontWeight: FontWeight.w600,
        ),
      ),
      listTileTheme: const ListTileThemeData(dense: true),
      inputDecorationTheme: InputDecorationTheme(
        border: OutlineInputBorder(borderRadius: BorderRadius.circular(12)),
        filled: true,
        isDense: true,
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          minimumSize: const Size(0, 46),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        ),
      ),
      snackBarTheme: const SnackBarThemeData(behavior: SnackBarBehavior.floating),
      navigationBarTheme: NavigationBarThemeData(
        height: 64,
        labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
        indicatorColor: scheme.primaryContainer,
      ),
    );
  }
}


/// Logged-in shell with a bottom navigation bar mirroring a stock phone app:
/// dialer, messages, contacts, AI assistant and account.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../data/realtime_client.dart';
import '../state/app_scope.dart';
import 'ai_assistant_page.dart';
import 'contacts_page.dart';
import 'conversations_page.dart';
import 'dialer_page.dart';
import 'settings_page.dart';

class HomePage extends StatefulWidget {
  const HomePage({super.key, this.initialTab = 0});

  final int initialTab;

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  late int _index = widget.initialTab;
  Timer? _poll;

  static const _titles = ['拨号', '短信', '通讯录', 'AI 助手', '我的'];

  @override
  void initState() {
    super.initState();
    // Realtime covers most updates; a slow poll keeps the UI honest when the
    // socket is down or the server restarts.
    _poll = Timer.periodic(const Duration(seconds: 20), (_) {
      if (!mounted) return;
      final scope = context.read<AppScope>();
      if (!scope.isLoggedIn) return;
      unawaited(scope.calls.refresh());
      unawaited(scope.devices.refresh());
    });
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final pages = [
      const DialerPage(),
      const ConversationsPage(),
      const ContactsPage(),
      const AiAssistantPage(),
      const SettingsPage(),
    ];
    return Scaffold(
      appBar: AppBar(
        title: Text(_titles[_index]),
        actions: [
          _ConnectionDot(status: scope.connectionStatus),
          const _DevicePicker(),
          IconButton(
            tooltip: '刷新',
            icon: const Icon(Icons.refresh),
            onPressed: () {
              unawaited(scope.devices.refresh());
              unawaited(scope.calls.refresh());
              unawaited(scope.messages.refreshConversations());
            },
          ),
        ],
      ),
      body: IndexedStack(index: _index, children: pages),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (i) => setState(() => _index = i),
        destinations: [
          NavigationDestination(
            icon: Badge(
              isLabelVisible: scope.calls.hasActive,
              child: const Icon(Icons.dialpad_outlined),
            ),
            selectedIcon: const Icon(Icons.dialpad),
            label: '拨号',
          ),
          NavigationDestination(
            icon: Badge(
              isLabelVisible: scope.messages.unreadTotal > 0,
              label: Text(scope.messages.unreadTotal > 99
                  ? '99+'
                  : scope.messages.unreadTotal.toString()),
              child: const Icon(Icons.chat_bubble_outline),
            ),
            selectedIcon: const Icon(Icons.chat_bubble),
            label: '短信',
          ),
          const NavigationDestination(
            icon: Icon(Icons.contacts_outlined),
            selectedIcon: Icon(Icons.contacts),
            label: '通讯录',
          ),
          const NavigationDestination(
            icon: Icon(Icons.smart_toy_outlined),
            selectedIcon: Icon(Icons.smart_toy),
            label: 'AI 助手',
          ),
          const NavigationDestination(
            icon: Icon(Icons.person_outline),
            selectedIcon: Icon(Icons.person),
            label: '我的',
          ),
        ],
      ),
    );
  }
}

class _ConnectionDot extends StatelessWidget {
  const _ConnectionDot({required this.status});
  final RealtimeStatus status;

  @override
  Widget build(BuildContext context) {
    final color = switch (status) {
      RealtimeStatus.connected => Colors.green,
      RealtimeStatus.connecting || RealtimeStatus.reconnecting => Colors.orange,
      RealtimeStatus.disconnected => Colors.redAccent,
    };
    final label = switch (status) {
      RealtimeStatus.connected => '实时连接正常',
      RealtimeStatus.connecting => '正在连接',
      RealtimeStatus.reconnecting => '重连中',
      RealtimeStatus.disconnected => '实时连接已断开',
    };
    return Padding(
      padding: const EdgeInsets.only(right: 4),
      child: Tooltip(
        message: label,
        child: Container(
          width: 10,
          height: 10,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle),
        ),
      ),
    );
  }
}

/// Which A7670X module (phone number) the app dials and sends from.
class _DevicePicker extends StatelessWidget {
  const _DevicePicker();

  @override
  Widget build(BuildContext context) {
    final devices = context.watch<AppScope>().devices;
    final effective = devices.effective;
    if (effective == null) {
      return const Padding(
        padding: EdgeInsets.symmetric(horizontal: 12),
        child: Text('无设备', style: TextStyle(fontSize: 12)),
      );
    }
    return PopupMenuButton<int?>(
      tooltip: '选择发送设备',
      onSelected: (id) => context.read<AppScope>().selectDevice(id),
      itemBuilder: (context) => [
        const PopupMenuItem<int?>(value: null, child: Text('自动选择在线设备')),
        for (final d in devices.items)
          PopupMenuItem<int?>(
            value: d.id,
            child: Row(
              children: [
                Icon(
                  d.isOnline ? Icons.circle : Icons.circle_outlined,
                  size: 10,
                  color: d.isOnline ? Colors.green : Colors.grey,
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    d.displayName + (d.simNumber == null ? '' : '  ' + d.simNumber!),
                    overflow: TextOverflow.ellipsis,
                  ),
                ),
              ],
            ),
          ),
      ],
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.sim_card_outlined,
                size: 16, color: effective.isOnline ? Colors.green : Colors.grey),
            const SizedBox(width: 4),
            Text(effective.displayName, style: const TextStyle(fontSize: 13)),
            const Icon(Icons.arrow_drop_down, size: 18),
          ],
        ),
      ),
    );
  }
}


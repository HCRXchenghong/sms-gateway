/// Logged-in shell with a bottom navigation bar mirroring a stock phone app:
/// dialer, messages, contacts, the AI call queue and account.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../config/app_theme.dart';
import '../data/realtime_client.dart';
import '../state/app_scope.dart';
import 'ai_calls_page.dart';
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

  static const _titles = ['拨号', '短信', '通讯录', 'AI 代打', '我的'];

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
      const AiCallsPage(),
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
      bottomNavigationBar: _BottomBar(
        index: _index,
        activeCall: scope.calls.hasActive,
        unread: scope.messages.unreadTotal,
        onChanged: (i) => setState(() => _index = i),
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
    return PopupMenuButton<int>(
      tooltip: '选择发送设备',
      onSelected: (id) =>
          context.read<AppScope>().selectDevice(id == -1 ? null : id),
      itemBuilder: (context) => [
        const PopupMenuItem<int>(value: -1, child: Text('自动选择在线设备')),
        for (final d in devices.items)
          PopupMenuItem<int>(
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
                    d.displayName +
                        (d.isOnline ? '' : '（离线）') +
                        (d.simNumber == null ? '' : '  ' + d.simNumber!),
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
            Icon(
              Icons.sim_card_outlined,
              size: 16,
              color: effective.isOnline ? Colors.green : Colors.grey,
            ),
            const SizedBox(width: 4),
            Text(
              effective.displayName + (effective.isOnline ? '' : '（离线）'),
              style: const TextStyle(fontSize: 13),
            ),
            const Icon(Icons.arrow_drop_down, size: 18),
          ],
        ),
      ),
    );
  }
}

/// One entry in the stock-style bottom bar.
class _BarItem {
  const _BarItem(this.label, this.icon, {this.dot = false, this.count = 0});

  final String label;
  final IconData icon;
  final bool dot;
  final int count;
}

/// Stock phone-app bottom bar: thin outline icons over small labels, no
/// indicator pill, a hairline separating it from the content above.
class _BottomBar extends StatelessWidget {
  const _BottomBar({
    required this.index,
    required this.activeCall,
    required this.unread,
    required this.onChanged,
  });

  final int index;
  final bool activeCall;
  final int unread;
  final ValueChanged<int> onChanged;

  static const Color _idle = Color(0xFF8A8F98);

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final items = <_BarItem>[
      _BarItem('拨号', Icons.dialpad_outlined, dot: activeCall),
      _BarItem('短信', Icons.chat_bubble_outline, count: unread),
      const _BarItem('通讯录', Icons.contacts_outlined),
      const _BarItem('AI 代打', Icons.support_agent_outlined),
      const _BarItem('我的', Icons.person_outline),
    ];
    return DecoratedBox(
      decoration: BoxDecoration(
        color: scheme.surface,
        border: Border(
          top: BorderSide(color: scheme.outlineVariant.withValues(alpha: 0.55)),
        ),
      ),
      child: SafeArea(
        top: false,
        child: SizedBox(
          height: 58,
          child: Row(
            children: [
              for (var i = 0; i < items.length; i++)
                Expanded(
                  child: _BottomBarItem(
                    item: items[i],
                    selected: i == index,
                    onTap: () => onChanged(i),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _BottomBarItem extends StatelessWidget {
  const _BottomBarItem({
    required this.item,
    required this.selected,
    required this.onTap,
  });

  final _BarItem item;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = selected ? scheme.onSurface : _BottomBar._idle;
    Widget icon = Icon(item.icon, size: 24, color: color);
    if (item.dot) {
      icon = Badge(
        isLabelVisible: true,
        backgroundColor: AppTheme.call,
        child: icon,
      );
    } else if (item.count > 0) {
      icon = Badge(
        label: Text(item.count > 99 ? '99+' : item.count.toString()),
        backgroundColor: AppTheme.call,
        textColor: Colors.white,
        child: icon,
      );
    }
    return InkWell(
      onTap: onTap,
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          icon,
          const SizedBox(height: 3),
          Text(
            item.label,
            style: TextStyle(
              fontSize: 11,
              height: 1.1,
              color: color,
              fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
            ),
          ),
        ],
      ),
    );
  }
}

/// Dialer tab: a numeric keypad plus the call log, laid out like the stock
/// Android phone app.
library;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../config/app_theme.dart';
import '../data/models.dart';
import '../state/app_scope.dart';
import 'thread_page.dart';

class DialerPage extends StatefulWidget {
  const DialerPage({super.key, this.initialNumber});

  final String? initialNumber;

  @override
  State<DialerPage> createState() => _DialerPageState();
}

class _DialerPageState extends State<DialerPage> with SingleTickerProviderStateMixin {
  late final TabController _tabs;
  late final TextEditingController _number;

  @override
  void initState() {
    super.initState();
    _tabs = TabController(length: 2, vsync: this);
    _number = TextEditingController(text: widget.initialNumber ?? '');
    context.read<AppScope>().calls.refreshHistory();
  }

  @override
  void dispose() {
    _tabs.dispose();
    _number.dispose();
    super.dispose();
  }

  void _press(String digit, {String? letters}) {
    HapticFeedback.lightImpact();
    final text = _number.text + digit;
    if (text.length > 24) return;
    setState(() => _number.text = text);
    _number.selection = TextSelection.collapsed(offset: text.length);
  }

  void _backspace() {
    HapticFeedback.selectionClick();
    final text = _number.text;
    if (text.isEmpty) return;
    setState(() => _number.text = text.substring(0, text.length - 1));
  }

  Future<void> _call() async {
    final number = _number.text.trim();
    if (number.length < 2) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('请输入要拨打的号码')));
      return;
    }
    final scope = context.read<AppScope>();
    final device = scope.devices.effective;
    if (device == null || !device.isOnline) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(device == null
              ? '没有可用的 4G 模块'
              : '${device.displayName} 已离线，请切换在线设备')));
      return;
    }
    final session = await scope.dial(number);
    if (!mounted) return;
    if (session == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('拨号失败: ' + (scope.calls.error ?? '未知错误'))),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    return Column(
      children: [
        TabBar(
          controller: _tabs,
          tabs: const [
            Tab(text: '键盘'),
            Tab(text: '通话记录'),
          ],
        ),
        Expanded(
          child: TabBarView(
            controller: _tabs,
            children: [
              _KeypadView(
                controller: _number,
                onPress: _press,
                onBackspace: _backspace,
                onCall: _call,
                voiceEnabled: scope.calls.voiceEnabled,
                deviceLabel: scope.devices.effective?.displayName,
              ),
              const _CallLogView(),
            ],
          ),
        ),
      ],
    );
  }
}

class _KeypadView extends StatelessWidget {
  const _KeypadView({
    required this.controller,
    required this.onPress,
    required this.onBackspace,
    required this.onCall,
    required this.voiceEnabled,
    this.deviceLabel,
  });

  final TextEditingController controller;
  final void Function(String digit, {String? letters}) onPress;
  final VoidCallback onBackspace;
  final VoidCallback onCall;
  final bool voiceEnabled;
  final String? deviceLabel;

  static const _keys = [
    ('1', ''),
    ('2', 'ABC'),
    ('3', 'DEF'),
    ('4', 'GHI'),
    ('5', 'JKL'),
    ('6', 'MNO'),
    ('7', 'PQRS'),
    ('8', 'TUV'),
    ('9', 'WXYZ'),
    ('*', ''),
    ('0', '+'),
    ('#', ''),
  ];

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return SafeArea(
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(24, 12, 8, 4),
            child: Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: controller,
                    keyboardType: TextInputType.phone,
                    textAlign: TextAlign.center,
                    style: const TextStyle(fontSize: 30, fontWeight: FontWeight.w500),
                    decoration: const InputDecoration(
                      border: InputBorder.none,
                      filled: false,
                      hintText: '输入号码',
                    ),
                    onChanged: (_) {},
                  ),
                ),
                ValueListenableBuilder<TextEditingValue>(
                  valueListenable: controller,
                  builder: (context, value, _) => IconButton(
                    icon: const Icon(Icons.backspace_outlined),
                    onPressed: value.text.isEmpty ? null : onBackspace,
                    tooltip: '删除',
                  ),
                ),
              ],
            ),
          ),
          if (deviceLabel != null)
            Text('通过 ' + deviceLabel! + ' 拨出',
                style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)),
          const Spacer(),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24),
            child: GridView.count(
              crossAxisCount: 3,
              shrinkWrap: true,
              physics: const NeverScrollableScrollPhysics(),
              childAspectRatio: 1.45,
              crossAxisSpacing: 12,
              mainAxisSpacing: 8,
              children: [
                for (final k in _keys)
                  _KeyButton(
                    digit: k.$1,
                    letters: k.$2,
                    onTap: () => onPress(k.$1, letters: k.$2),
                    onLongPress: k.$1 == '0' ? () => onPress('+') : null,
                  ),
              ],
            ),
          ),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              const SizedBox(width: 72),
              SizedBox(
                width: 72,
                height: 72,
                child: FilledButton(
                  style: FilledButton.styleFrom(
                    backgroundColor: voiceEnabled ? AppTheme.call : Colors.grey,
                    foregroundColor: Colors.white,
                    padding: EdgeInsets.zero,
                    shape: const CircleBorder(),
                  ),
                  onPressed: voiceEnabled ? onCall : null,
                  child: const Icon(Icons.call, size: 32),
                ),
              ),
              const SizedBox(width: 72),
            ],
          ),
          const SizedBox(height: 16),
          if (!voiceEnabled)
            Text('服务端未开启语音功能', style: TextStyle(color: scheme.error, fontSize: 12)),
        ],
      ),
    );
  }
}

class _KeyButton extends StatelessWidget {
  const _KeyButton({
    required this.digit,
    required this.letters,
    required this.onTap,
    this.onLongPress,
  });

  final String digit;
  final String letters;
  final VoidCallback onTap;
  final VoidCallback? onLongPress;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Material(
      color: scheme.surfaceContainerHighest,
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTap: onTap,
        onLongPress: onLongPress,
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(digit,
                  style: const TextStyle(fontSize: 28, fontWeight: FontWeight.w400, height: 1.1)),
              if (letters.isNotEmpty)
                Text(letters,
                    style: TextStyle(
                        fontSize: 10, letterSpacing: 2, color: scheme.onSurfaceVariant)),
            ],
          ),
        ),
      ),
    );
  }
}

class _CallLogView extends StatelessWidget {
  const _CallLogView();

  static final _fmt = DateFormat('MM-dd HH:mm');

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final records = scope.calls.history;
    if (records.isEmpty) {
      return const Center(child: Text('暂无通话记录'));
    }
    return RefreshIndicator(
      onRefresh: () => scope.calls.refreshHistory(),
      child: ListView.separated(
        physics: const AlwaysScrollableScrollPhysics(),
        itemCount: records.length,
        separatorBuilder: (_, __) => const Divider(height: 1, indent: 64),
        itemBuilder: (context, i) {
          final r = records[i];
          final missed = r.answeredAt == null && r.endReason != 'local_hangup';
          final name = scope.contacts.nameFor(r.remote ?? '');
          return ListTile(
            leading: CircleAvatar(
              backgroundColor: missed
                  ? Theme.of(context).colorScheme.errorContainer
                  : Theme.of(context).colorScheme.surfaceContainerHighest,
              child: Icon(
                r.isInbound ? Icons.call_received : Icons.call_made,
                color: missed ? Theme.of(context).colorScheme.error : null,
              ),
            ),
            title: Text(
              name ?? (r.remote?.isEmpty == false ? r.remote! : '未知号码'),
              style: missed ? TextStyle(color: Theme.of(context).colorScheme.error) : null,
            ),
            subtitle: Text(_subtitle(r)),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (r.hasRecording)
                  const Tooltip(
                    message: '本通电话有录音',
                    child: Padding(
                      padding: EdgeInsets.only(right: 6),
                      child: Icon(Icons.mic, size: 14, color: Colors.redAccent),
                    ),
                  ),
                if (r.deviceCode != null)
                  Text(r.deviceCode!,
                      style: TextStyle(
                          fontSize: 11, color: Theme.of(context).colorScheme.onSurfaceVariant)),
                IconButton(
                  icon: const Icon(Icons.call, color: AppTheme.call),
                  tooltip: '回拨',
                  onPressed: r.remote == null
                      ? null
                      : () => _redial(context, scope, r.remote!),
                ),
              ],
            ),
            onTap: () => _showDetail(context, r),
          );
        },
      ),
    );
  }

  String _subtitle(CallRecord r) {
    final when = r.createdAt == null
        ? ''
        : _fmt.format(DateTime.fromMillisecondsSinceEpoch(r.createdAt! * 1000));
    final reason = _reasonLabel(r.endReason);
    final dur = r.duration > 0 ? '  ' + _duration(r.duration) : '';
    return [when, reason + dur].where((s) => s.isNotEmpty).join(' · ');
  }

  static String _reasonLabel(String? reason) => switch (reason ?? '') {
        'local_hangup' => '已挂断',
        'remote_hangup' => '对方挂断',
        'rejected' => '已拒接',
        'busy' => '对方忙',
        'no_answer' => '无应答',
        'no_carrier' => '未接通',
        'no_dialtone' => '无拨号音',
        'timeout' => '超时',
        'error' => '失败',
        _ => '',
      };

  static String _duration(int seconds) {
    final m = seconds ~/ 60;
    final s = seconds % 60;
    return m.toString().padLeft(2, '0') + ':' + s.toString().padLeft(2, '0');
  }

  void _redial(BuildContext context, AppScope scope, String number) {
    scope.dial(number);
  }

  void _showDetail(BuildContext context, CallRecord r) {
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      builder: (context) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 0, 20, 28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(r.remote ?? '未知号码', style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 12),
            _row(context, '方向', r.isInbound ? '来电' : '去电'),
            _row(context, '状态', r.state),
            _row(context, '结束原因', _reasonLabel(r.endReason)),
            _row(context, '通话时长', _duration(r.duration)),
            _row(context, '使用设备', r.deviceName ?? '-'),
            _row(context, 'SIM 号码', r.simNumber ?? '-'),
            _row(context, 'Call ID', r.callId),
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: FilledButton.icon(
                    icon: const Icon(Icons.call),
                    label: const Text('回拨'),
                    onPressed: r.remote == null
                        ? null
                        : () {
                            Navigator.pop(context);
                            context.read<AppScope>().dial(r.remote!);
                          },
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: OutlinedButton.icon(
                    icon: const Icon(Icons.chat_outlined),
                    label: const Text('发短信'),
                    onPressed: r.remote == null
                        ? null
                        : () {
                            Navigator.pop(context);
                            Navigator.of(context).push(MaterialPageRoute<void>(
                              builder: (_) => ThreadPage(peer: r.remote!),
                            ));
                          },
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _row(BuildContext context, String label, String? value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
                width: 84,
                child: Text(label,
                    style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant))),
            Expanded(child: Text(value?.isEmpty == true ? '-' : (value ?? '-'))),
          ],
        ),
      );
}


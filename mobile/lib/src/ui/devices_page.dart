/// Device list with live signal, plus a raw AT console for diagnosing
/// firmware differences on real hardware.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../data/models.dart';
import '../state/app_scope.dart';

class DevicesPage extends StatefulWidget {
  const DevicesPage({super.key});

  @override
  State<DevicesPage> createState() => _DevicesPageState();
}

class _DevicesPageState extends State<DevicesPage> {
  final Map<int, Map<String, dynamic>> _signals = {};

  @override
  void initState() {
    super.initState();
    _refresh();
  }

  Future<void> _refresh() async {
    final scope = context.read<AppScope>();
    await scope.devices.refresh();
    if (!mounted) return;
    for (final d in scope.devices.items) {
      try {
        final sig = await scope.api.deviceSignal(d.id);
        _signals[d.id] = sig;
      } catch (_) {
        // A device that dropped off the bus simply has no signal reading.
      }
    }
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final items = scope.devices.items;
    return Scaffold(
      appBar: AppBar(
        title: const Text('设备与 AT 调试'),
        actions: [
          IconButton(
              tooltip: '重新扫描 USB',
              icon: const Icon(Icons.usb_outlined),
              onPressed: () async {
                await scope.devices.rescan();
                await _refresh();
              }),
          IconButton(icon: const Icon(Icons.refresh), onPressed: _refresh),
        ],
      ),
      body: items.isEmpty
          ? Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  const Icon(Icons.usb_off, size: 48),
                  const SizedBox(height: 12),
                  Text(scope.devices.error ?? '没有发现 4G 模块'),
                  const SizedBox(height: 8),
                  const Padding(
                    padding: EdgeInsets.symmetric(horizontal: 32),
                    child: Text(
                      '确认模块已插好、SIM 卡支持语音、USB 供电充足。拔插后点右上角重新扫描即可，服务无需重启。',
                      textAlign: TextAlign.center,
                      style: TextStyle(fontSize: 12),
                    ),
                  ),
                ],
              ),
            )
          : ListView.separated(
              padding: const EdgeInsets.all(12),
              itemCount: items.length,
              separatorBuilder: (_, __) => const SizedBox(height: 10),
              itemBuilder: (context, i) => _DeviceCard(
                device: items[i],
                signal: _signals[items[i].id],
                selected: scope.devices.preferredId == items[i].id,
                onSelect: () => scope.selectDevice(items[i].id),
                onAtConsole: () => Navigator.of(context).push(MaterialPageRoute<void>(
                    builder: (_) => AtConsolePage(device: items[i]))),
              ),
            ),
    );
  }
}

class _DeviceCard extends StatelessWidget {
  const _DeviceCard({
    required this.device,
    required this.signal,
    required this.selected,
    required this.onSelect,
    required this.onAtConsole,
  });

  final DeviceInfo device;
  final Map<String, dynamic>? signal;
  final bool selected;
  final VoidCallback onSelect;
  final VoidCallback onAtConsole;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final csq = _csqOf(signal);
    return Card(
      margin: EdgeInsets.zero,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(14),
        side: BorderSide(
            color: selected ? scheme.primary : scheme.outlineVariant,
            width: selected ? 1.6 : 1),
      ),
      child: Padding(
        padding: const EdgeInsets.all(14),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(Icons.router_outlined,
                    color: device.isOnline ? scheme.primary : scheme.onSurfaceVariant),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(device.displayName,
                          style: const TextStyle(
                              fontWeight: FontWeight.w600, fontSize: 15)),
                      Text(
                        (device.model ?? '未知型号') +
                            (device.manufacturer == null ? '' : ' · ' + device.manufacturer!),
                        style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
                      ),
                    ],
                  ),
                ),
                _StatusChip(online: device.isOnline, status: device.status),
              ],
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 16,
              runSpacing: 6,
              children: [
                _kv(context, 'SIM 号码', device.simNumber ?? '-'),
                _kv(context, 'ICCID', _tail(device.iccid)),
                _kv(context, 'IMEI', _tail(device.imei)),
                _kv(context, 'AT 口', device.atPort ?? '-'),
                _kv(context, '信号', csq == null ? '-' : csq.toString() + ' / 31'),
              ],
            ),
            if (csq != null) ...[
              const SizedBox(height: 8),
              LinearProgressIndicator(
                value: (csq / 31).clamp(0.0, 1.0),
                minHeight: 4,
                borderRadius: BorderRadius.circular(2),
              ),
            ],
            if (device.lastError != null) ...[
              const SizedBox(height: 8),
              Text('最近错误: ' + device.lastError!,
                  style: TextStyle(fontSize: 11, color: scheme.error)),
            ],
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: OutlinedButton.icon(
                    icon: Icon(selected ? Icons.check_circle : Icons.circle_outlined, size: 18),
                    label: Text(selected ? '当前使用' : '设为发送设备'),
                    onPressed: onSelect,
                  ),
                ),
                const SizedBox(width: 10),
                OutlinedButton.icon(
                  icon: const Icon(Icons.terminal, size: 18),
                  label: const Text('AT 调试'),
                  onPressed: device.isOnline ? onAtConsole : null,
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  static int? _csqOf(Map<String, dynamic>? signal) {
    if (signal == null) return null;
    final raw = signal['csq'] ?? signal['signal'] ?? signal['rssi'];
    if (raw is num) return raw.toInt();
    if (raw is String) return int.tryParse(raw);
    return null;
  }

  static String _tail(String? value) {
    if (value == null || value.isEmpty) return '-';
    return value.length > 8 ? '…' + value.substring(value.length - 8) : value;
  }

  Widget _kv(BuildContext context, String k, String v) => SizedBox(
        width: 150,
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
                width: 62,
                child: Text(k,
                    style: TextStyle(
                        fontSize: 11,
                        color: Theme.of(context).colorScheme.onSurfaceVariant))),
            Expanded(
                child: Text(v,
                    style: const TextStyle(fontSize: 11), overflow: TextOverflow.ellipsis)),
          ],
        ),
      );
}

class _StatusChip extends StatelessWidget {
  const _StatusChip({required this.online, required this.status});
  final bool online;
  final String status;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: online ? scheme.primaryContainer : scheme.surfaceContainerHighest,
        borderRadius: BorderRadius.circular(10),
      ),
      child: Text(online ? '在线' : status,
          style: TextStyle(
              fontSize: 11,
              color: online ? scheme.onPrimaryContainer : scheme.onSurfaceVariant)),
    );
  }
}

/// Raw AT console. Useful when a firmware batch behaves differently: type the
/// command, see exactly what the module returns.
class AtConsolePage extends StatefulWidget {
  const AtConsolePage({super.key, required this.device});
  final DeviceInfo device;

  @override
  State<AtConsolePage> createState() => _AtConsolePageState();
}

class _AtConsolePageState extends State<AtConsolePage> {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  final List<String> _log = [];
  bool _busy = false;

  static const _presets = [
    'ATI',
    'AT+CPIN?',
    'AT+CSQ',
    'AT+CREG?',
    'AT+CEREG?',
    'AT+CNUM',
    'AT+CLCC',
    'AT+CTTS=?',
    'AT+CDTAM=1',
    'AT+CSDVC=?',
    'AT+CLVL?',
  ];

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _send(String command) async {
    final cmd = command.trim();
    if (cmd.isEmpty || _busy) return;
    setState(() {
      _busy = true;
      _log.add('> ' + cmd);
    });
    _input.clear();
    try {
      final result = await context
          .read<AppScope>()
          .api
          .sendRawAt(widget.device.id, cmd);
      final text = result is Map ? (result['response'] ?? result['result'] ?? result).toString() : result.toString();
      setState(() => _log.add(text));
    } catch (e) {
      setState(() => _log.add('! ' + e.toString()));
    } finally {
      if (mounted) {
        setState(() => _busy = false);
        _scrollDown();
      }
    }
  }

  void _scrollDown() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(
        title: Text('AT 调试 · ' + widget.device.displayName),
        actions: [
          IconButton(
              tooltip: '清空',
              icon: const Icon(Icons.delete_outline),
              onPressed: () => setState(_log.clear)),
        ],
      ),
      body: Column(
        children: [
          SizedBox(
            height: 44,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
              children: [
                for (final p in _presets)
                  Padding(
                    padding: const EdgeInsets.only(right: 6),
                    child: ActionChip(
                      label: Text(p, style: const TextStyle(fontSize: 11)),
                      onPressed: () => _send(p),
                    ),
                  ),
              ],
            ),
          ),
          Expanded(
            child: Container(
              width: double.infinity,
              margin: const EdgeInsets.symmetric(horizontal: 10),
              padding: const EdgeInsets.all(10),
              decoration: BoxDecoration(
                color: scheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(10),
              ),
              child: _log.isEmpty
                  ? Text('输入 AT 指令后回车发送。例如 AT+CSQ 查询信号，AT+CTTS=? 探测语音合成能力。',
                      style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant))
                  : ListView.builder(
                      controller: _scroll,
                      itemCount: _log.length,
                      itemBuilder: (context, i) => Text(
                        _log[i],
                        style: TextStyle(
                          fontSize: 12,
                          fontFamily: 'monospace',
                          color: _log[i].startsWith('>')
                              ? scheme.primary
                              : (_log[i].startsWith('!') ? scheme.error : scheme.onSurface),
                        ),
                      ),
                    ),
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.all(10),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _input,
                      autocorrect: false,
                      textInputAction: TextInputAction.send,
                      decoration: const InputDecoration(hintText: 'AT+CSQ'),
                      onSubmitted: _send,
                    ),
                  ),
                  const SizedBox(width: 8),
                  FilledButton(
                    onPressed: _busy ? null : () => _send(_input.text),
                    child: const Text('发送'),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}


/// Full screen call UI shown for any live call.
///
/// Mirrors the stock dialer: caller identity and elapsed time at the top, a
/// toggleable DTMF keypad and call controls below. TTS broadcast and the AI
/// reply sit behind a sheet because they are gateway specific.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';

import '../config/app_theme.dart';
import '../data/models.dart';
import '../state/app_scope.dart';

class InCallPage extends StatefulWidget {
  const InCallPage({super.key, required this.callId});

  final String callId;

  @override
  State<InCallPage> createState() => _InCallPageState();
}

class _InCallPageState extends State<InCallPage> {
  Timer? _ticker;
  int _elapsed = 0;
  bool _showKeypad = false;
  bool _muted = false;
  final TextEditingController _tts = TextEditingController();

  @override
  void initState() {
    super.initState();
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) => _tick());
    _tick();
  }

  @override
  void dispose() {
    _ticker?.cancel();
    _tts.dispose();
    super.dispose();
  }

  CallSession? _find(AppScope scope) {
    for (final c in scope.calls.active) {
      if (c.callId == widget.callId) return c;
    }
    // Fall back to the foreground call if this id already rolled off.
    return scope.calls.foreground;
  }

  void _tick() {
    final scope = context.read<AppScope>();
    final call = _find(scope);
    if (call == null) return;
    final started = call.answeredAt;
    if (started == null) return;
    final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    final value = (now - started).clamp(0, 1 << 30);
    if (value != _elapsed && mounted) setState(() => _elapsed = value);
  }

  Future<void> _hangup(AppScope scope, CallSession call) async {
    await scope.calls.hangup(call.deviceId);
    if (mounted) Navigator.of(context).maybePop();
  }

  Future<void> _answer(AppScope scope, CallSession call) async {
    final ok = await scope.calls.answer(call.deviceId);
    if (!mounted) return;
    if (!ok) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('接听失败: ' + (scope.calls.error ?? '未知错误'))),
      );
    }
  }

  Future<void> _dtmf(AppScope scope, CallSession call, String digit) async {
    HapticFeedback.selectionClick();
    await scope.calls.sendDtmf(call.deviceId, digit);
  }

  Future<void> _speak(AppScope scope, CallSession call) async {
    final text = _tts.text.trim();
    if (text.isEmpty) return;
    final ok = await scope.calls.speak(call.deviceId, text);
    if (!mounted) return;
    if (ok) {
      _tts.clear();
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('已播报: ' + text)));
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('播报失败: ' + (scope.calls.error ?? '模块可能不支持 AT+CTTS'))),
      );
    }
  }

  Future<void> _aiReply(AppScope scope, CallSession call) async {
    final ok = await scope.ai.send('请用一句话问候来电者并询问来意。');
    if (!mounted) return;
    final reply = ok;
    if (reply == null) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('AI 不可用: ' + (scope.ai.error ?? '未配置'))));
      return;
    }
    await scope.calls.speak(call.deviceId, reply);
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final call = _find(scope);
    if (call == null) {
      // The call ended before this frame; let the router pop us.
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final scheme = Theme.of(context).colorScheme;
    final name = scope.contacts.nameFor(call.remote ?? '') ?? call.remote ?? '未知号码';
    final device = scope.devices.byId(call.deviceId);

    return Scaffold(
      backgroundColor: scheme.surface,
      body: SafeArea(
        child: Column(
          children: [
            const SizedBox(height: 40),
            CircleAvatar(
              radius: 44,
              child: Text(name.substring(0, 1).toUpperCase(),
                  style: const TextStyle(fontSize: 32)),
            ),
            const SizedBox(height: 16),
            Text(name, style: Theme.of(context).textTheme.headlineSmall),
            const SizedBox(height: 6),
            Text(_statusText(call),
                style: TextStyle(fontSize: 15, color: scheme.onSurfaceVariant)),
            if (call.state == CallState.active || call.state == CallState.held) ...[
              const SizedBox(height: 4),
              Text(_formatDuration(_elapsed),
                  style: const TextStyle(fontSize: 20, fontVariations: [])),
            ],
            if (device != null) ...[
              const SizedBox(height: 6),
              Text('经由 ' + device.displayName +
                  (device.simNumber == null ? '' : ' · ' + device.simNumber!),
                  style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)),
            ],
            if (scope.calls.notice != null) ...[
              const SizedBox(height: 10),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                decoration: BoxDecoration(
                  color: scheme.secondaryContainer,
                  borderRadius: BorderRadius.circular(20),
                ),
                child: Text(scope.calls.notice!,
                    style: TextStyle(color: scheme.onSecondaryContainer, fontSize: 13)),
              ),
            ],
            const Spacer(),
            if (_showKeypad)
              _DtmfPad(onDigit: (d) => _dtmf(scope, call, d))
            else
              _Controls(
                muted: _muted,
                call: call,
                onToggleKeypad: () => setState(() => _showKeypad = true),
                onMute: () async {
                  setState(() => _muted = !_muted);
                  // Mute is applied on the module through AT+CMUT.
                  await scope.api.post(
                    '/app/devices/' + call.deviceId.toString() + '/at',
                    body: {'command': 'AT+CMUT=' + (_muted ? '1' : '0')},
                  );
                },
                onTts: () => _openTtsSheet(scope, call),
                onAi: call.state == CallState.active ? () => _aiReply(scope, call) : null,
              ),
            const SizedBox(height: 24),
            if (call.state == CallState.incoming)
              Row(
                mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                children: [
                  _BigAction(
                    icon: Icons.call_end,
                    color: AppTheme.hangup,
                    label: '拒接',
                    onTap: () => _hangup(scope, call),
                  ),
                  _BigAction(
                    icon: Icons.call,
                    color: AppTheme.call,
                    label: '接听',
                    onTap: () => _answer(scope, call),
                  ),
                ],
              )
            else
              _BigAction(
                icon: Icons.call_end,
                color: AppTheme.hangup,
                label: call.state == CallState.active ? '挂断' : '取消',
                onTap: () => _hangup(scope, call),
              ),
            const SizedBox(height: 28),
          ],
        ),
      ),
    );
  }

  void _openTtsSheet(AppScope scope, CallSession call) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (sheetContext) => Padding(
        padding: EdgeInsets.fromLTRB(
            20, 0, 20, MediaQuery.of(sheetContext).viewInsets.bottom + 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text('通话中语音播报', style: Theme.of(sheetContext).textTheme.titleMedium),
            const SizedBox(height: 6),
            const Text('文字通过模块的 AT+CTTS 合成后从通话音频播出，对方可直接听到。',
                style: TextStyle(fontSize: 12)),
            const SizedBox(height: 12),
            TextField(
              controller: _tts,
              autofocus: true,
              minLines: 2,
              maxLines: 4,
              maxLength: 512,
              decoration: const InputDecoration(hintText: '输入要播报的内容'),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final preset in const [
                  '您好，这里是自动语音通知。',
                  '您的验证码是 123456。',
                  '请稍等，正在为您转接人工。',
                ])
                  ActionChip(
                    label: Text(preset, style: const TextStyle(fontSize: 12)),
                    onPressed: () => _tts.text = preset,
                  ),
              ],
            ),
            const SizedBox(height: 16),
            FilledButton.icon(
              icon: const Icon(Icons.record_voice_over_outlined),
              label: const Text('开始播报'),
              onPressed: () {
                Navigator.pop(sheetContext);
                _speak(scope, call);
              },
            ),
            const SizedBox(height: 8),
            OutlinedButton.icon(
              icon: const Icon(Icons.stop_circle_outlined),
              label: const Text('停止播报'),
              onPressed: () {
                Navigator.pop(sheetContext);
                scope.calls.stopSpeak(call.deviceId);
              },
            ),
          ],
        ),
      ),
    );
  }

  static String _statusText(CallSession call) => switch (call.state) {
        CallState.dialing => '正在拨号…',
        CallState.alerting => '对方振铃中…',
        CallState.incoming => '来电',
        CallState.active => '通话中',
        CallState.held => '已保持',
        CallState.ended => '通话结束',
        _ => call.state,
      };

  static String _formatDuration(int seconds) {
    final h = seconds ~/ 3600;
    final m = (seconds % 3600) ~/ 60;
    final s = seconds % 60;
    String two(int v) => v.toString().padLeft(2, '0');
    return h > 0 ? two(h) + ':' + two(m) + ':' + two(s) : two(m) + ':' + two(s);
  }
}

class _Controls extends StatelessWidget {
  const _Controls({
    required this.muted,
    required this.call,
    required this.onToggleKeypad,
    required this.onMute,
    required this.onTts,
    this.onAi,
  });

  final bool muted;
  final CallSession call;
  final VoidCallback onToggleKeypad;
  final VoidCallback onMute;
  final VoidCallback onTts;
  final VoidCallback? onAi;

  @override
  Widget build(BuildContext context) {
    final enabled = call.state == CallState.active || call.state == CallState.held;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 28),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceEvenly,
        children: [
          _SmallAction(
            icon: muted ? Icons.mic_off : Icons.mic,
            label: '静音',
            active: muted,
            enabled: enabled,
            onTap: onMute,
          ),
          _SmallAction(
            icon: Icons.dialpad,
            label: '键盘',
            enabled: enabled,
            onTap: onToggleKeypad,
          ),
          _SmallAction(
            icon: Icons.record_voice_over_outlined,
            label: '播报',
            enabled: enabled,
            onTap: onTts,
          ),
          _SmallAction(
            icon: Icons.smart_toy_outlined,
            label: 'AI 应答',
            enabled: enabled && onAi != null,
            onTap: onAi,
          ),
        ],
      ),
    );
  }
}

class _SmallAction extends StatelessWidget {
  const _SmallAction({
    required this.icon,
    required this.label,
    required this.onTap,
    this.active = false,
    this.enabled = true,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final bool active;
  final bool enabled;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          width: 60,
          height: 60,
          child: Material(
            color: active
                ? scheme.primary
                : (enabled ? scheme.surfaceContainerHighest : scheme.surfaceContainer),
            shape: const CircleBorder(),
            child: InkWell(
              customBorder: const CircleBorder(),
              onTap: enabled ? onTap : null,
              child: Icon(icon,
                  color: active
                      ? scheme.onPrimary
                      : (enabled ? scheme.onSurface : scheme.onSurface.withValues(alpha: 0.3))),
            ),
          ),
        ),
        const SizedBox(height: 6),
        Text(label,
            style: TextStyle(
                fontSize: 11,
                color: enabled ? scheme.onSurfaceVariant : scheme.onSurfaceVariant.withValues(alpha: 0.4))),
      ],
    );
  }
}

class _BigAction extends StatelessWidget {
  const _BigAction({
    required this.icon,
    required this.color,
    required this.label,
    required this.onTap,
  });

  final IconData icon;
  final Color color;
  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        SizedBox(
          width: 72,
          height: 72,
          child: FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: color,
              foregroundColor: Colors.white,
              padding: EdgeInsets.zero,
              shape: const CircleBorder(),
            ),
            onPressed: onTap,
            child: Icon(icon, size: 32),
          ),
        ),
        const SizedBox(height: 6),
        Text(label, style: const TextStyle(fontSize: 12)),
      ],
    );
  }
}

class _DtmfPad extends StatelessWidget {
  const _DtmfPad({required this.onDigit});
  final ValueChanged<String> onDigit;

  static const _keys = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '*', '0', '#'];

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 36),
      child: GridView.count(
        crossAxisCount: 3,
        shrinkWrap: true,
        physics: const NeverScrollableScrollPhysics(),
        childAspectRatio: 1.5,
        crossAxisSpacing: 10,
        mainAxisSpacing: 6,
        children: [
          for (final d in _keys)
            Material(
              color: Theme.of(context).colorScheme.surfaceContainerHighest,
              shape: const CircleBorder(),
              child: InkWell(
                customBorder: const CircleBorder(),
                onTap: () => onDigit(d),
                child: Center(
                    child: Text(d, style: const TextStyle(fontSize: 24))),
              ),
            ),
        ],
      ),
    );
  }
}


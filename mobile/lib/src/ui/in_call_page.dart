/// Full screen call UI shown for any live call.
///
/// Mirrors the stock dialer: caller identity and elapsed time at the top, a
/// toggleable DTMF keypad and call controls below. The gateway extras — TTS
/// broadcast, live translation and the AI taking the call over — sit behind
/// sheets because they are not part of a stock phone.
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
    final ai = context.read<AppScope>().aiCalls;
    unawaited(ai.refreshFlow(widget.callId));
    unawaited(ai.refreshTranscript(widget.callId));
  }

  @override
  void dispose() {
    _ticker?.cancel();
    _tts.dispose();
    super.dispose();
  }

  CallSession? _find(AppScope scope) {
    return scope.calls.registry[widget.callId];
  }

  void _tick() {
    if (!mounted) return;
    final scope = context.read<AppScope>();
    final call = _find(scope);
    if (call == null || call.isEnded) {
      _ticker?.cancel();
      return;
    }
    final started = call.answeredAt;
    if (started == null) return;
    final now = DateTime.now().millisecondsSinceEpoch ~/ 1000;
    final value = (now - started).clamp(0, 1 << 30);
    if (value != _elapsed) setState(() => _elapsed = value);
  }

  Future<void> _hangup(AppScope scope, CallSession call) async {
    final ok = await scope.calls.hangup(call.deviceId);
    if (!ok && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('挂断失败: ' + (scope.calls.error ?? '未知错误'))),
      );
    }
  }

  Future<void> _answer(AppScope scope, CallSession call) async {
    final ok = await scope.calls.answer(call.deviceId);
    if (!mounted) return;
    if (!ok) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('接听失败: ' + (scope.calls.error ?? '未知错误'))),
      );
      return;
    }
    setState(() {});
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

  /// Hand the conversation to the AI. It needs a purpose so it knows what a
  /// successful call looks like.
  Future<void> _attachAgent(AppScope scope, CallSession call) async {
    final draft = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (_) => const _AgentDialog(),
    );
    if (draft == null || !mounted) return;
    final ai = scope.aiCalls;
    final ok = await ai.attachAgent(
      widget.callId,
      purpose: draft['purpose'] as String,
      context: draft['context'] as String?,
      peerLanguage: draft['peerLanguage'] as String?,
      maxSeconds: draft['maxSeconds'] as int?,
    );
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ok ? 'AI 已接管这通电话' : 'AI 接管失败: ' + (ai.error ?? '未知错误')),
    ));
  }

  /// Take the conversation back. The call stays up; the AI stops speaking.
  Future<void> _handover(AppScope scope) async {
    final ai = scope.aiCalls;
    final ok = await ai.handover(widget.callId);
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(ok ? '已接管回来，AI 停止说话' : '接管失败: ' + (ai.error ?? '未知错误')),
    ));
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

  void _openFlowSheet() {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _FlowSheet(callId: widget.callId),
    );
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final call = _find(scope);
    if (call == null || call.isEnded) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted) Navigator.of(context).maybePop();
      });
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final scheme = Theme.of(context).colorScheme;
    final name = scope.contacts.nameFor(call.remote ?? '') ?? call.remote ?? '未知号码';
    final device = scope.devices.byId(call.deviceId);
    final ai = scope.aiCalls;
    final mode = ai.translationModeFor(widget.callId);
    final agentRunning = ai.agentRunningFor(widget.callId);

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
            if (agentRunning) ...[
              const SizedBox(height: 10),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                decoration: BoxDecoration(
                  color: scheme.tertiaryContainer,
                  borderRadius: BorderRadius.circular(20),
                ),
                child: Text('AI 正在替你说这通电话',
                    style: TextStyle(color: scheme.onTertiaryContainer, fontSize: 13)),
              ),
            ],
            const Spacer(),
            if (call.state == CallState.active && mode != 'off')
              _SubtitleStrip(callId: widget.callId),
            const SizedBox(height: 8),
            if (_showKeypad)
              _DtmfPad(onDigit: (d) => _dtmf(scope, call, d))
            else
              _Controls(
                muted: _muted,
                call: call,
                agentRunning: agentRunning,
                translationOn: mode != 'off',
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
                onAgent: call.state == CallState.active
                    ? (agentRunning
                        ? () => _handover(scope)
                        : () => _attachAgent(scope, call))
                    : null,
                onFlow: call.state == CallState.active ? _openFlowSheet : null,
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

/// The last few subtitle lines, shown just above the controls while live
/// translation is on so the operator reads the far end without opening a sheet.
class _SubtitleStrip extends StatelessWidget {
  const _SubtitleStrip({required this.callId});

  final String callId;

  @override
  Widget build(BuildContext context) {
    final ai = context.watch<AppScope>().aiCalls;
    final lines = ai.subtitlesFor(callId);
    if (lines.isEmpty) {
      return const Padding(
        padding: EdgeInsets.symmetric(horizontal: 28),
        child: Align(
          alignment: Alignment.centerLeft,
          child: Text('正在听对方说话…', style: TextStyle(fontSize: 12)),
        ),
      );
    }
    final recent = lines.length > 3 ? lines.sublist(lines.length - 3) : lines;
    return ConstrainedBox(
      constraints: const BoxConstraints(maxHeight: 140),
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(horizontal: 28),
        children: [for (final item in recent) _SubtitleLine(item: item)],
      ),
    );
  }
}

/// One subtitle line. Translation entries carry both the original and the
/// rendered text; plain transcript lines carry the spoken text.
class _SubtitleLine extends StatelessWidget {
  const _SubtitleLine({required this.item});

  final Map<String, dynamic> item;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final entry = item['entry'];
    if (entry is! Map) return const SizedBox.shrink();
    final map = Map<String, dynamic>.from(entry);
    if (item['kind'] == 'translation') {
      final speaker = map['speaker'] == 'me' ? '我' : '对方';
      final source = (map['source_text'] ?? '').toString();
      final target = (map['target_text'] ?? '').toString();
      final from = (map['source_language'] ?? '').toString();
      final to = (map['target_language'] ?? '').toString();
      return Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              speaker + '（' + from + '→' + to + '）：' + source,
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
            ),
            Text(
              target,
              style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
            ),
          ],
        ),
      );
    }
    final role = (map['role'] ?? '').toString();
    final content = (map['content'] ?? '').toString();
    final original = (map['original'] ?? '').toString();
    final label = switch (role) {
      'peer' => '对方',
      'me' => '我',
      'agent' => 'AI',
      _ => '系统',
    };
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (original.isNotEmpty && original != content)
            Text(
              label + '：' + original,
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
            ),
          Text(label + '：' + content, style: const TextStyle(fontSize: 14)),
        ],
      ),
    );
  }
}

class _Controls extends StatelessWidget {
  const _Controls({
    required this.muted,
    required this.call,
    required this.agentRunning,
    required this.translationOn,
    required this.onToggleKeypad,
    required this.onMute,
    required this.onTts,
    this.onAgent,
    this.onFlow,
  });

  final bool muted;
  final CallSession call;
  final bool agentRunning;
  final bool translationOn;
  final VoidCallback onToggleKeypad;
  final VoidCallback onMute;
  final VoidCallback onTts;
  final VoidCallback? onAgent;
  final VoidCallback? onFlow;

  @override
  Widget build(BuildContext context) {
    final enabled = call.state == CallState.active || call.state == CallState.held;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 20),
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
            icon: agentRunning ? Icons.person_outline : Icons.support_agent_outlined,
            label: agentRunning ? '接管回来' : 'AI 接管',
            active: agentRunning,
            enabled: enabled && onAgent != null,
            onTap: onAgent,
          ),
          _SmallAction(
            icon: Icons.translate,
            label: '翻译',
            active: translationOn,
            enabled: enabled && onFlow != null,
            onTap: onFlow,
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
          width: 56,
          height: 56,
          child: Material(
            color: active
                ? scheme.primary
                : (enabled ? scheme.surfaceContainerHighest : scheme.surfaceContainer),
            shape: const CircleBorder(),
            child: InkWell(
              customBorder: const CircleBorder(),
              onTap: enabled ? onTap : null,
              child: Icon(icon,
                  size: 22,
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

/// Live translation, what to say next and the minutes, in one sheet.
///
/// 字幕 only renders the far end into text; 语音 also speaks the translation
/// into the operator's ear. Either way what the operator types is translated
/// into the peer's language before it goes into the call.
class _FlowSheet extends StatefulWidget {
  const _FlowSheet({required this.callId});

  final String callId;

  @override
  State<_FlowSheet> createState() => _FlowSheetState();
}

class _FlowSheetState extends State<_FlowSheet> {
  final _say = TextEditingController();
  bool _sending = false;

  @override
  void dispose() {
    _say.dispose();
    super.dispose();
  }

  Future<void> _setMode(String mode) async {
    final ai = context.read<AppScope>().aiCalls;
    final ok = await ai.setTranslation(widget.callId, mode: mode);
    if (!mounted) return;
    if (!ok) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ai.error ?? '切换失败')));
    }
  }

  Future<void> _sayText() async {
    final text = _say.text.trim();
    if (text.isEmpty) return;
    setState(() => _sending = true);
    final ai = context.read<AppScope>().aiCalls;
    final ok = await ai.say(widget.callId, text);
    if (!mounted) return;
    setState(() => _sending = false);
    if (ok) {
      _say.clear();
    } else {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ai.error ?? '发送失败')));
    }
  }

  Future<void> _minutes() async {
    final ai = context.read<AppScope>().aiCalls;
    final result = await ai.summary(widget.callId);
    if (!mounted) return;
    if (result == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(ai.error ?? '这通电话没有可整理的内容')),
      );
      return;
    }
    final minutes = result['summary'];
    if (!mounted) return;
    await showDialog<void>(
      context: context,
      builder: (_) => _MinutesDialog(
        minutes: minutes is Map ? Map<String, dynamic>.from(minutes) : const {},
      ),
    );
  }

  static String _lang(Object? value) {
    final text = (value ?? '').toString();
    return text.isEmpty ? '自动' : text;
  }

  @override
  Widget build(BuildContext context) {
    final ai = context.watch<AppScope>().aiCalls;
    final scheme = Theme.of(context).colorScheme;
    final flow = ai.flowFor(widget.callId);
    final translation = flow['translation'];
    final mode = ai.translationModeFor(widget.callId);
    final note = ai.translationNoteFor(widget.callId);
    final agentRunning = ai.agentRunningFor(widget.callId);
    final subtitles = ai.subtitlesFor(widget.callId);
    return Padding(
      padding: EdgeInsets.fromLTRB(
          20, 0, 20, MediaQuery.of(context).viewInsets.bottom + 24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('通话助手', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 4),
          Text(
            '实时翻译把对方的话转成你看得懂的字幕或语音；你输入的话会翻成对方的语言说出去。',
            style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
          ),
          const SizedBox(height: 12),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'off', label: Text('关')),
              ButtonSegment(value: 'subtitle', label: Text('字幕')),
              ButtonSegment(value: 'voice', label: Text('语音')),
            ],
            selected: {mode},
            onSelectionChanged: (values) => _setMode(values.first),
          ),
          if (translation is Map)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                '我的语言：' + _lang(translation['my_language']) +
                    '　对方语言：' + _lang(translation['peer_language']),
                style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
              ),
            ),
          if (note != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                '翻译说明：' + note,
                style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
              ),
            ),
          const SizedBox(height: 12),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 200),
            child: subtitles.isEmpty
                ? Align(
                    alignment: Alignment.centerLeft,
                    child: Text(
                      '还没有字幕。',
                      style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
                    ),
                  )
                : ListView(
                    shrinkWrap: true,
                    children: [for (final item in subtitles) _SubtitleLine(item: item)],
                  ),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _say,
            minLines: 1,
            maxLines: 3,
            decoration: const InputDecoration(
              hintText: '输入要说的话，网关翻译后说给对方',
            ),
          ),
          const SizedBox(height: 8),
          Row(
            children: [
              Expanded(
                child: FilledButton.icon(
                  onPressed: _sending ? null : _sayText,
                  icon: const Icon(Icons.campaign_outlined, size: 18),
                  label: const Text('说给对方'),
                ),
              ),
              const SizedBox(width: 8),
              OutlinedButton.icon(
                onPressed: _minutes,
                icon: const Icon(Icons.summarize_outlined, size: 18),
                label: const Text('纪要'),
              ),
            ],
          ),
          if (agentRunning)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                'AI 正在替你说这通电话，点「接管回来」即可收回。',
                style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
              ),
            ),
        ],
      ),
    );
  }
}

/// Asks what the AI should achieve on this call before it takes over.
class _AgentDialog extends StatefulWidget {
  const _AgentDialog();

  @override
  State<_AgentDialog> createState() => _AgentDialogState();
}

class _AgentDialogState extends State<_AgentDialog> {
  final _purpose = TextEditingController();
  final _context = TextEditingController();
  final _peerLanguage = TextEditingController();
  double _minutes = 5;
  bool _limitEnabled = true;
  String? _purposeError;

  @override
  void dispose() {
    _purpose.dispose();
    _context.dispose();
    _peerLanguage.dispose();
    super.dispose();
  }

  void _submit() {
    final purpose = _purpose.text.trim();
    if (purpose.length < 2) {
      setState(() => _purposeError = '写清楚要让 AI 谈成什么');
      return;
    }
    final background = _context.text.trim();
    final language = _peerLanguage.text.trim();
    Navigator.pop(context, <String, dynamic>{
      'purpose': purpose,
      'context': background.isEmpty ? null : background,
      'peerLanguage': language.isEmpty ? null : language,
      'maxSeconds': _limitEnabled ? (_minutes * 60).round() : null,
    });
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('让 AI 接管这通电话'),
      content: SizedBox(
        width: 420,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'AI 会按这个目的跟对方谈，谈成或谈不成都以通话内容为准。你随时可以接管回来。',
                style: TextStyle(fontSize: 12),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _purpose,
                maxLines: 2,
                maxLength: 500,
                decoration: InputDecoration(
                  labelText: '要让 AI 谈成什么',
                  errorText: _purposeError,
                  border: const OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _context,
                maxLines: 2,
                maxLength: 2000,
                decoration: const InputDecoration(
                  labelText: '背景资料（可选）',
                  border: OutlineInputBorder(),
                ),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _peerLanguage,
                decoration: const InputDecoration(
                  labelText: '对方语言（可选）',
                  hintText: '例如 en、ja、ru；留空则由网关判断',
                  border: OutlineInputBorder(),
                ),
              ),
              Row(
                children: [
                  Switch(
                    value: _limitEnabled,
                    onChanged: (value) => setState(() => _limitEnabled = value),
                  ),
                  Expanded(
                    child: Text(
                      _limitEnabled
                          ? 'AI 最多说 ' + _minutes.round().toString() + ' 分钟'
                          : '不额外限制（用网关默认上限）',
                      style: const TextStyle(fontSize: 13),
                    ),
                  ),
                ],
              ),
              if (_limitEnabled)
                Slider(
                  value: _minutes,
                  min: 1,
                  max: 30,
                  divisions: 29,
                  label: _minutes.round().toString() + ' 分钟',
                  onChanged: (value) => setState(() => _minutes = value),
                ),
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton.icon(
          onPressed: _submit,
          icon: const Icon(Icons.support_agent_outlined, size: 18),
          label: const Text('交给 AI'),
        ),
      ],
    );
  }
}

/// The minutes for this call, straight from the summary model.
class _MinutesDialog extends StatelessWidget {
  const _MinutesDialog({required this.minutes});

  final Map<String, dynamic> minutes;

  List<String> _strings(String key) {
    final raw = minutes[key];
    if (raw is! List) return const [];
    return raw.whereType<Object>().map((e) => e.toString()).toList();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final headline = (minutes['summary'] ?? '').toString();
    final facts = _strings('key_facts');
    final agreements = _strings('agreements');
    final unconfirmed = _strings('unconfirmed');
    final evidence = (minutes['purpose_evidence'] ?? '').toString();
    final achieved = minutes['purpose_achieved'];
    return AlertDialog(
      title: const Text('通话纪要'),
      content: SizedBox(
        width: 440,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (headline.isNotEmpty) Text(headline),
              if (achieved != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    achieved == true
                        ? '目的已达成'
                        : achieved == false
                            ? '目的未达成'
                            : '无法判断目的是否达成',
                    style: TextStyle(
                      fontSize: 13,
                      color: achieved == true ? Colors.green : scheme.onSurfaceVariant,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              if (evidence.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 4),
                  child: Text(
                    '依据：' + evidence,
                    style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
                  ),
                ),
              for (final block in <List<Object>>[
                ['关键事实', facts],
                ['约定事项', agreements],
                ['未确认', unconfirmed],
              ])
                if ((block[1] as List<String>).isNotEmpty) ...[
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Text(
                      block[0].toString(),
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                  ),
                  for (final line in block[1] as List<String>)
                    Padding(
                      padding: const EdgeInsets.only(top: 2),
                      child: Text('· ' + line, style: const TextStyle(fontSize: 13)),
                    ),
                ],
            ],
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('关闭'),
        ),
      ],
    );
  }
}

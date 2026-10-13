/// AI assistant tab: chat with the configured LLM, preview TTS, and check
/// which provider each channel resolved to.
///
/// ASR stays on the server side. During a call the gateway records from the
/// module's USB sound card, so the phone never needs microphone access.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/app_scope.dart';

class AiAssistantPage extends StatefulWidget {
  const AiAssistantPage({super.key});

  @override
  State<AiAssistantPage> createState() => _AiAssistantPageState();
}

class _AiAssistantPageState extends State<AiAssistantPage> {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    context.read<AppScope>().ai.refresh();
  }

  @override
  void dispose() {
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _scrollDown() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.jumpTo(_scroll.position.maxScrollExtent);
    });
  }

  Future<void> _send() async {
    final text = _input.text.trim();
    if (text.isEmpty) return;
    _input.clear();
    _scrollDown();
    final reply = await context.read<AppScope>().ai.send(text);
    _scrollDown();
    if (!mounted) return;
    if (reply == null) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text('AI 调用失败: ' +
              (context.read<AppScope>().ai.error ?? '未配置供应商'))));
    }
  }

  Future<void> _speak(String text) async {
    final scope = context.read<AppScope>();
    final device = scope.devices.effective;
    if (device == null) {
      _toast('没有在线设备，无法播报');
      return;
    }
    final call = scope.calls.forDevice(device.id);
    if (call != null && !call.isEnded) {
      // Broadcast into the live call through the module's own TTS.
      final ok = await scope.calls.speak(device.id, text);
      _toast(ok ? '已在通话中播报' : '播报失败');
      return;
    }
    final ok = await scope.audio.playPrompt('dial_connecting');
    _toast(ok ? '已请求服务器播放' : '播放失败');
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final ai = scope.ai;
    final turns = ai.history;
    final scheme = Theme.of(context).colorScheme;

    return Column(
      children: [
        _StatusStrip(ai: ai),
        Expanded(
          child: turns.isEmpty
              ? const _EmptyHint()
              : ListView.builder(
                  controller: _scroll,
                  padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                  itemCount: turns.length,
                  itemBuilder: (context, i) {
                    final t = turns[i];
                    final mine = t.role == 'user';
                    return Align(
                      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
                      child: Container(
                        margin: const EdgeInsets.symmetric(vertical: 4),
                        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 9),
                        constraints: BoxConstraints(
                            maxWidth: MediaQuery.of(context).size.width * 0.78),
                        decoration: BoxDecoration(
                          color: mine ? scheme.primaryContainer : scheme.surfaceContainerHighest,
                          borderRadius: BorderRadius.circular(14),
                        ),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.end,
                          children: [
                            Text(t.content, style: const TextStyle(height: 1.35)),
                            if (!mine)
                              Align(
                                alignment: Alignment.centerLeft,
                                child: IconButton(
                                  visualDensity: VisualDensity.compact,
                                  iconSize: 16,
                                  padding: EdgeInsets.zero,
                                  tooltip: '播报',
                                  icon: const Icon(Icons.volume_up_outlined),
                                  onPressed: () => _speak(t.content),
                                ),
                              ),
                          ],
                        ),
                      ),
                    );
                  },
                ),
        ),
        if (ai.sending)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 6),
            child: LinearProgressIndicator(minHeight: 2),
          ),
        SafeArea(
          top: false,
          child: Padding(
            padding: const EdgeInsets.fromLTRB(10, 4, 10, 8),
            child: Row(
              children: [
                IconButton(
                  tooltip: '清空对话',
                  icon: const Icon(Icons.delete_sweep_outlined),
                  onPressed: turns.isEmpty ? null : ai.clearHistory,
                ),
                Expanded(
                  child: TextField(
                    controller: _input,
                    minLines: 1,
                    maxLines: 4,
                    textInputAction: TextInputAction.send,
                    decoration: const InputDecoration(hintText: '和 AI 说点什么'),
                    onSubmitted: (_) => _send(),
                  ),
                ),
                const SizedBox(width: 8),
                SizedBox(
                  width: 46,
                  height: 46,
                  child: FilledButton(
                    style: FilledButton.styleFrom(
                        padding: EdgeInsets.zero, shape: const CircleBorder()),
                    onPressed: ai.sending ? null : _send,
                    child: const Icon(Icons.send, size: 20),
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

class _StatusStrip extends StatelessWidget {
  const _StatusStrip({required this.ai});
  final dynamic ai;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final loaded = ai.loaded as bool;
    return Container(
      width: double.infinity,
      color: loaded ? scheme.surfaceContainer : scheme.errorContainer,
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
      child: Row(
        children: [
          Icon(
            loaded ? Icons.check_circle_outline : Icons.error_outline,
            size: 16,
            color: loaded ? scheme.primary : scheme.onErrorContainer,
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              '对话供应商: ' + ai.provider.toString() +
                  (loaded ? '' : '（未就绪，请在网页后台配置 API Key）'),
              style: TextStyle(
                  fontSize: 12,
                  color: loaded ? scheme.onSurfaceVariant : scheme.onErrorContainer),
            ),
          ),
          Text('TTS: ' + ai.ttsEngine.toString(),
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

class _EmptyHint extends StatelessWidget {
  const _EmptyHint();

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.smart_toy_outlined,
                size: 52, color: Theme.of(context).colorScheme.onSurfaceVariant),
            const SizedBox(height: 12),
            const Text('AI 助手', style: TextStyle(fontSize: 16)),
            const SizedBox(height: 8),
            Text(
              '这里连的是网关后台配置的 Chat 接口。通话中点「AI 应答」会把回复直接合成播报给对方，'
              '语音转文字由服务器从模块声卡采集，手机端不需要麦克风权限。',
              textAlign: TextAlign.center,
              style: TextStyle(
                  fontSize: 12, color: Theme.of(context).colorScheme.onSurfaceVariant),
            ),
          ],
        ),
      ),
    );
  }
}


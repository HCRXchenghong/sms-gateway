/// AI channel configuration: chat, ASR and TTS providers plus the two
/// automation switches (auto answer a call, auto reply to SMS).
///
/// The form is built from the server's editable key list so a new setting
/// appears here without an app release.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/app_scope.dart';

class AiSettingsPage extends StatefulWidget {
  const AiSettingsPage({super.key});

  @override
  State<AiSettingsPage> createState() => _AiSettingsPageState();
}

class _AiSettingsPageState extends State<AiSettingsPage> {
  final Map<String, TextEditingController> _text = {};
  final Map<String, bool> _flags = {};
  bool _dirty = false;
  bool _loaded = false;

  static const _secretKeys = {'ai.chat.api_key', 'ai.asr.api_key', 'ai.tts.api_key'};

  @override
  void initState() {
    super.initState();
    _init();
  }

  Future<void> _init() async {
    final scope = context.read<AppScope>();
    await scope.settings.refresh();
    await scope.ai.refresh();
    if (!mounted) return;
    for (final e in scope.settings.editable) {
      final value = scope.settings.values[e];
      if (value is bool) {
        _flags[e] = value;
      } else if (value != null) {
        _text[e] = TextEditingController(text: value.toString());
      }
    }
    setState(() => _loaded = true);
  }

  @override
  void dispose() {
    for (final c in _text.values) {
      c.dispose();
    }
    super.dispose();
  }

  void _markDirty() {
    if (_dirty) return;
    setState(() => _dirty = true);
  }

  Future<void> _save() async {
    final scope = context.read<AppScope>();
    final patch = <String, dynamic>{};
    _flags.forEach((k, v) => patch[k] = v);
    _text.forEach((k, c) {
      final original = scope.settings.values[k];
      final current = c.text;
      // Only send what changed; API keys are never echoed back by the server,
      // so an untouched secret field must not overwrite the stored value.
      if (_secretKeys.contains(k) && current.isEmpty) return;
      if (original != null && original.toString() == current) return;
      if (original is num) {
        patch[k] = num.tryParse(current) ?? original;
      } else {
        patch[k] = current;
      }
    });
    final ok = await scope.settings.save(patch);
    if (!mounted) return;
    setState(() => _dirty = false);
    await scope.ai.refresh();
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(ok ? '已保存，AI 配置热加载生效' : '保存失败: ' + (scope.settings.error ?? ''))));
  }

  Future<void> _test() async {
    final scope = context.read<AppScope>();
    final reply = await scope.ai.send('你好，请回复“连接正常”四个字。');
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(reply == null
          ? '对话失败: ' + (scope.ai.error ?? '未配置')
          : '对话成功: ' + reply),
      duration: const Duration(seconds: 4),
    ));
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    if (!_loaded) {
      return Scaffold(
        appBar: AppBar(title: const Text('AI 语音与对话')),
        body: const Center(child: CircularProgressIndicator()),
      );
    }
    return Scaffold(
      appBar: AppBar(
        title: const Text('AI 语音与对话'),
        actions: [
          TextButton.icon(
            onPressed: _test,
            icon: const Icon(Icons.bolt_outlined, size: 18),
            label: const Text('测试对话'),
          ),
        ],
      ),
      floatingActionButton: _dirty
          ? FloatingActionButton.extended(
              onPressed: _save,
              icon: const Icon(Icons.save_outlined),
              label: const Text('保存'),
            )
          : null,
      body: ListView(
        padding: const EdgeInsets.only(bottom: 96),
        children: [
          _Card(
            title: '总开关',
            icon: Icons.power_settings_new,
            children: [
              _switchTile('ai.enabled', '启用 AI 能力',
                  '关闭后通话中的 AI 应答与短信自动回复都不会触发'),
              _switchTile('ai.auto_answer_call', '来电自动接听并 AI 应答',
                  '模块收到 RING 后自动 ATA，再用 Chat 结果做 TTS 播报'),
              _switchTile('ai.auto_reply_sms', '收到短信自动回复',
                  '用 Chat 生成回复内容并通过模块发回给发件人'),
              _textTile('ai.provider', '供应商策略',
                  hint: 'auto / mock / openai / custom'),
            ],
          ),
          _Card(
            title: '对话 Chat',
            icon: Icons.chat_bubble_outline,
            children: [
              _textTile('ai.chat.base_url', '接口地址', hint: 'https://api.openai.com/v1'),
              _textTile('ai.chat.model', '模型', hint: 'gpt-4o-mini'),
              _secretTile('ai.chat.api_key', 'API Key'),
              _multilineTile('ai.chat.system_prompt', '系统提示词'),
            ],
          ),
          _Card(
            title: '语音转文字 ASR',
            icon: Icons.record_voice_over_outlined,
            children: [
              _textTile('ai.asr.base_url', '接口地址'),
              _textTile('ai.asr.model', '模型', hint: 'whisper-1'),
              _secretTile('ai.asr.api_key', 'API Key'),
            ],
          ),
          _Card(
            title: '文字转语音 TTS',
            icon: Icons.graphic_eq_outlined,
            children: [
              _textTile('ai.tts.engine', '合成方式',
                  hint: 'module = 用 A7670X 内置 AT+CTTS，cloud = 用云端接口'),
              _textTile('ai.tts.base_url', '接口地址'),
              _textTile('ai.tts.model', '模型', hint: 'tts-1'),
              _textTile('ai.tts.voice', '音色', hint: 'alloy / echo / nova'),
              _textTile('ai.tts.format', '音频格式', hint: 'wav / mp3 / pcm'),
              _secretTile('ai.tts.api_key', 'API Key'),
            ],
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Text(
              '当前对话供应商解析为「' +
                  scope.ai.provider +
                  '」，' +
                  (scope.ai.loaded ? '已就绪' : '尚未就绪') +
                  '。TTS 引擎为 ' +
                  scope.ai.ttsEngine +
                  '。选择 module 时不需要云端 Key，直接用模块内置语音合成，'
                  '缺点是音质一般但零成本、零延迟。',
              style: TextStyle(
                  fontSize: 12, color: Theme.of(context).colorScheme.onSurfaceVariant),
            ),
          ),
        ],
      ),
    );
  }

  Widget _switchTile(String key, String title, String subtitle) {
    final value = _flags[key] ?? false;
    return SwitchListTile(
      value: value,
      title: Text(title),
      subtitle: Text(subtitle, style: const TextStyle(fontSize: 12)),
      onChanged: _editable(key)
          ? (v) {
              setState(() => _flags[key] = v);
              _markDirty();
            }
          : null,
    );
  }

  Widget _textTile(String key, String label, {String? hint}) => _field(
        key: key,
        label: label,
        hint: hint ?? '',
        obscure: false,
        maxLines: 1,
      );

  Widget _secretTile(String key, String label) => _field(
        key: key,
        label: label + '（留空表示不修改）',
        hint: 'sk-...',
        obscure: true,
        maxLines: 1,
      );

  Widget _multilineTile(String key, String label) => _field(
        key: key,
        label: label,
        hint: '',
        obscure: false,
        maxLines: 4,
      );

  Widget _field({
    required String key,
    required String label,
    required String hint,
    required bool obscure,
    required int maxLines,
  }) {
    final controller = _text[key];
    if (controller == null) {
      return ListTile(
        dense: true,
        title: Text(label),
        subtitle: const Text('该配置项不可编辑'),
        trailing: const Icon(Icons.lock_outline, size: 16),
      );
    }
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 6, 16, 6),
      child: TextField(
        controller: controller,
        obscureText: obscure,
        maxLines: obscure ? 1 : maxLines,
        enabled: _editable(key),
        decoration: InputDecoration(
          labelText: label,
          hintText: hint.isEmpty ? null : hint,
        ),
        onChanged: (_) => _markDirty(),
      ),
    );
  }

  bool _editable(String key) =>
      context.read<AppScope>().settings.editable.contains(key);
}

class _Card extends StatelessWidget {
  const _Card({required this.title, required this.icon, required this.children});
  final String title;
  final IconData icon;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 10, 12, 4),
      decoration: BoxDecoration(
        color: scheme.surfaceContainer,
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 4),
            child: Row(
              children: [
                Icon(icon, size: 18, color: scheme.primary),
                const SizedBox(width: 8),
                Text(title,
                    style: const TextStyle(fontWeight: FontWeight.w600, fontSize: 15)),
              ],
            ),
          ),
          ...children,
          const SizedBox(height: 8),
        ],
      ),
    );
  }
}


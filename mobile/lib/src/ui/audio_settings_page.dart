/// Sound card selection, output volume and the built in prompt tones.
///
/// The gateway plays prompts on the server, either through the chosen ALSA
/// device or into the call itself via the module. This page picks the device
/// and previews each tone.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../data/models.dart';
import '../state/app_scope.dart';

class AudioSettingsPage extends StatefulWidget {
  const AudioSettingsPage({super.key});

  @override
  State<AudioSettingsPage> createState() => _AudioSettingsPageState();
}

class _AudioSettingsPageState extends State<AudioSettingsPage> {
  @override
  void initState() {
    super.initState();
    context.read<AppScope>().audio.refresh();
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final audio = context.watch<AppScope>().audio;
    final state = audio.state;

    return Scaffold(
      appBar: AppBar(
        title: const Text('声卡与提示音'),
        actions: [
          IconButton(
              icon: const Icon(Icons.refresh), onPressed: () => audio.refresh()),
        ],
      ),
      body: state == null
          ? Center(
              child: audio.busy
                  ? const CircularProgressIndicator()
                  : Text(audio.error ?? '无法读取声卡列表'))
          : ListView(
              padding: const EdgeInsets.only(bottom: 32),
              children: [
                _Header(
                  title: '音频后端',
                  value: state.backend.toUpperCase(),
                  hint: '服务器上的音频子系统。ALSA 直连声卡延迟最低，PulseAudio / PipeWire 兼容性更好。',
                ),
                _DeviceGroup(
                  title: '输出声卡',
                  icon: Icons.volume_up_outlined,
                  devices: state.outputs,
                  selectedId: state.outputDevice,
                  emptyLabel: '系统默认',
                  onSelected: (id) async {
                    final ok = await audio.setOutput(id);
                    _toast(ok ? '输出声卡已切换' : '切换失败: ' + (audio.error ?? ''));
                  },
                ),
                _DeviceGroup(
                  title: '输入声卡',
                  icon: Icons.mic_none,
                  devices: state.inputs,
                  selectedId: state.inputDevice,
                  emptyLabel: '系统默认',
                  onSelected: (id) async {
                    final ok = await audio.setInput(id);
                    _toast(ok ? '输入声卡已切换' : '切换失败: ' + (audio.error ?? ''));
                  },
                ),
                const _SectionLabel('提示音音量'),
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Row(
                    children: [
                      const Icon(Icons.volume_down_outlined, size: 20),
                      Expanded(
                        child: Slider(
                          value: state.volume.clamp(0.0, 1.0),
                          divisions: 20,
                          label: (state.volume * 100).round().toString() + '%',
                          onChanged: (v) => audio.setVolume(v),
                        ),
                      ),
                      const Icon(Icons.volume_up_outlined, size: 20),
                      SizedBox(
                          width: 44,
                          child: Text((state.volume * 100).round().toString() + '%',
                              textAlign: TextAlign.end)),
                    ],
                  ),
                ),
                const _SectionLabel('提示音'),
                const Padding(
                  padding: EdgeInsets.symmetric(horizontal: 16),
                  child: Text(
                    '点播放会在服务器的输出声卡上试听。通话中的提示音由呼叫状态机自动触发，'
                    '例如拨号后播「正在接通，请稍候」，对方占线播「您拨打的电话正忙」。',
                    style: TextStyle(fontSize: 12),
                  ),
                ),
                const SizedBox(height: 6),
                if (audio.prompts.isEmpty)
                  const Padding(
                    padding: EdgeInsets.all(16),
                    child: Text('没有可用的提示音'),
                  )
                else
                  for (final p in audio.prompts)
                    _PromptTile(
                      prompt: p,
                      playing: audio.playingPrompt == p.id,
                      onPlay: () async {
                        final ok = await audio.playPrompt(p.id);
                        _toast(ok ? '已请求播放' : '播放失败: ' + (audio.error ?? ''));
                      },
                    ),
              ],
            ),
    );
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.title, required this.value, required this.hint});
  final String title;
  final String value;
  final String hint;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      margin: const EdgeInsets.fromLTRB(16, 12, 16, 4),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: scheme.surfaceContainer,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
              const Spacer(),
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                decoration: BoxDecoration(
                  color: scheme.primaryContainer,
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text(value,
                    style: TextStyle(fontSize: 12, color: scheme.onPrimaryContainer)),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(hint,
              style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)),
        ],
      ),
    );
  }
}

class _DeviceGroup extends StatelessWidget {
  const _DeviceGroup({
    required this.title,
    required this.icon,
    required this.devices,
    required this.selectedId,
    required this.emptyLabel,
    required this.onSelected,
  });

  final String title;
  final IconData icon;
  final List<AudioDevice> devices;
  final String selectedId;
  final String emptyLabel;
  final ValueChanged<String> onSelected;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _SectionLabel(title),
        if (devices.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
            child: Text('服务器没有枚举到可用设备',
                style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 12)),
          )
        else
          for (final d in devices)
            Builder(builder: (context) {
              // An empty selection means "use the server default".
              final isSelected =
                  selectedId.isEmpty ? d.isDefault : selectedId == d.id;
              return ListTile(
                dense: true,
                onTap: () => onSelected(d.id),
                leading: Icon(
                  isSelected
                      ? Icons.radio_button_checked
                      : Icons.radio_button_unchecked,
                  color: isSelected ? scheme.primary : scheme.onSurfaceVariant,
                ),
                title: Row(
                children: [
                  Icon(icon, size: 16),
                  const SizedBox(width: 8),
                  Expanded(child: Text(d.name, overflow: TextOverflow.ellipsis)),
                  if (d.isDefault)
                    Container(
                      margin: const EdgeInsets.only(left: 6),
                      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
                      decoration: BoxDecoration(
                        color: scheme.secondaryContainer,
                        borderRadius: BorderRadius.circular(8),
                      ),
                      child: Text('默认',
                          style: TextStyle(
                              fontSize: 10, color: scheme.onSecondaryContainer)),
                    ),
                ],
              ),
              subtitle: Text(
                d.id +
                    (d.description == null || d.description!.isEmpty
                        ? ''
                        : ' · ' + d.description!) +
                    (d.state == null ? '' : ' · ' + d.state!),
                style: const TextStyle(fontSize: 11),
              ),
              );
            }),
        const SizedBox(height: 4),
      ],
    );
  }
}

class _SectionLabel extends StatelessWidget {
  const _SectionLabel(this.text);
  final String text;

  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 18, 16, 6),
        child: Text(text.toUpperCase(),
            style: TextStyle(
                fontSize: 11,
                letterSpacing: 1,
                fontWeight: FontWeight.w600,
                color: Theme.of(context).colorScheme.primary)),
      );
}

class _PromptTile extends StatelessWidget {
  const _PromptTile({
    required this.prompt,
    required this.playing,
    required this.onPlay,
  });

  final PromptTone prompt;
  final bool playing;
  final VoidCallback onPlay;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return ListTile(
      dense: true,
      leading: Icon(_iconFor(prompt.id), color: scheme.primary),
      title: Text(prompt.text),
      subtitle: Text(
        prompt.id +
            (prompt.tone == null ? '' : ' · ' + prompt.tone!) +
            (prompt.description == null ? '' : '\n' + prompt.description!),
        style: const TextStyle(fontSize: 11),
      ),
      isThreeLine: prompt.description != null,
      trailing: IconButton(
        tooltip: playing ? '播放中' : '试听',
        icon: playing
            ? const SizedBox(
                width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2))
            : const Icon(Icons.play_circle_outline),
        onPressed: playing ? null : onPlay,
      ),
    );
  }

  static IconData _iconFor(String id) => switch (id) {
        'dial_connecting' || 'ringback' => Icons.phone_in_talk_outlined,
        'incoming_call' => Icons.phone_callback_outlined,
        'call_busy' => Icons.phone_disabled_outlined,
        'call_no_answer' => Icons.phone_forwarded_outlined,
        'call_invalid_number' => Icons.warning_amber_outlined,
        'call_ended' => Icons.call_end_outlined,
        _ => Icons.music_note_outlined,
      };
}


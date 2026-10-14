/// One SMS conversation, rendered as chat bubbles like the stock messages app.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../config/app_theme.dart';
import '../data/models.dart';
import '../state/app_scope.dart';
import '../state/controllers.dart';

class ThreadPage extends StatefulWidget {
  const ThreadPage({super.key, required this.peer, this.peerKey, this.title});

  final String peer;
  final String? peerKey;
  final String? title;

  @override
  State<ThreadPage> createState() => _ThreadPageState();
}

class _ThreadPageState extends State<ThreadPage> {
  final TextEditingController _input = TextEditingController();
  final ScrollController _scroll = ScrollController();
  Timer? _draftTimer;
  bool _sending = false;
  late final MessagesController _messages;

  String get _peer => widget.peer;

  @override
  void initState() {
    super.initState();
    _messages = context.read<AppScope>().messages;
    _load();
    // Restore any draft saved on the server for this peer.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      final existing = _findConversation()?.draft;
      if (existing != null && existing.isNotEmpty) _input.text = existing;
    });
  }

  Conversation? _findConversation() {
    final key = widget.peerKey ?? _peer;
    for (final c in context.read<AppScope>().messages.conversations) {
      if (c.peerKey == key || c.peer == _peer) return c;
    }
    return null;
  }

  Future<void> _load() async {
    await context.read<AppScope>().messages.openThread(
      _peer,
      peerKey: widget.peerKey ?? _peer,
    );
    if (mounted) _scrollToBottom();
  }

  void _scrollToBottom() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!_scroll.hasClients) return;
      _scroll.animateTo(
        _scroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 200),
        curve: Curves.easeOut,
      );
    });
  }

  void _scheduleDraftSave() {
    _draftTimer?.cancel();
    _draftTimer = Timer(const Duration(milliseconds: 800), () {
      if (!mounted) return;
      final text = _input.text.trim();
      context.read<AppScope>().messages.saveDraft(
        _peer,
        text.isEmpty ? null : text,
      );
    });
  }

  Future<void> _send() async {
    final text = _input.text.trim();
    if (text.isEmpty || _sending) return;
    final scope = context.read<AppScope>();
    final device = scope.devices.effective;
    if (device == null || !device.isOnline) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            device == null ? '没有可用设备' : '${device.displayName} 已离线，请切换在线设备',
          ),
        ),
      );
      return;
    }
    setState(() => _sending = true);
    final ok = await scope.messages.send(
      to: _peer,
      content: text,
      deviceId: scope.devices.effective?.id,
    );
    if (!mounted) return;
    setState(() => _sending = false);
    if (ok) {
      _input.clear();
      await scope.messages.openThread(_peer, peerKey: widget.peerKey ?? _peer);
      _scrollToBottom();
    } else {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('发送失败: ' + (scope.messages.error ?? '未知错误'))),
      );
    }
  }

  Future<void> _callPeer() async {
    final scope = context.read<AppScope>();
    final session = await scope.dial(_peer);
    if (!mounted) return;
    if (session == null) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('拨号失败: ' + (scope.calls.error ?? '未知错误'))),
      );
    }
  }

  @override
  void dispose() {
    _messages.closeThread();
    _draftTimer?.cancel();
    _input.dispose();
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final key = widget.peerKey ?? _peer;
    final items = scope.messages.thread(key);
    final contactName = scope.contacts.nameFor(_peer);
    final title = contactName ?? widget.title ?? _peer;

    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: const TextStyle(fontSize: 17)),
            if (contactName != null)
              Text(
                _peer,
                style: TextStyle(
                  fontSize: 12,
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
          ],
        ),
        actions: [
          if (scope.calls.voiceEnabled)
            IconButton(
              tooltip: '拨打',
              icon: const Icon(Icons.call, color: AppTheme.call),
              onPressed: _callPeer,
            ),
          PopupMenuButton<String>(
            onSelected: (v) async {
              if (v == 'save') await _saveContact();
              if (v == 'refresh') await _load();
            },
            itemBuilder: (context) => const [
              PopupMenuItem(value: 'save', child: Text('存为联系人')),
              PopupMenuItem(value: 'refresh', child: Text('刷新')),
            ],
          ),
        ],
      ),
      body: Column(
        children: [
          Expanded(
            child: items.isEmpty
                ? const Center(child: Text('还没有消息，发一条试试'))
                : ListView.builder(
                    controller: _scroll,
                    padding: const EdgeInsets.symmetric(
                      horizontal: 12,
                      vertical: 8,
                    ),
                    itemCount: items.length,
                    itemBuilder: (context, i) => _Bubble(message: items[i]),
                  ),
          ),
          _Composer(
            controller: _input,
            sending: _sending,
            onChanged: (_) => _scheduleDraftSave(),
            onSend: _send,
          ),
        ],
      ),
    );
  }

  Future<void> _saveContact() async {
    final scope = context.read<AppScope>();
    final controller = TextEditingController();
    final name = await showDialog<String>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('新建联系人'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(labelText: '姓名'),
          onSubmitted: (v) => Navigator.pop(dialogContext, v),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, controller.text),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    final trimmed = (name ?? '').trim();
    if (trimmed.isEmpty) return;
    final ok = await scope.contacts.save(
      Contact(id: 0, name: trimmed, number: _peer),
    );
    if (!mounted) return;
    ScaffoldMessenger.of(context)
        .showSnackBar(SnackBar(content: Text(ok ? '已保存到通讯录' : '保存失败')));
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({required this.message});
  final SmsMessage message;

  static final _time = DateFormat('HH:mm');

  @override
  Widget build(BuildContext context) {
    final outgoing = !message.isInbound;
    final scheme = Theme.of(context).colorScheme;
    final isDark = scheme.brightness == Brightness.dark;
    final bg = outgoing
        ? Color.alphaBlend(
            AppTheme.bubbleOut.withValues(alpha: 0.55),
            scheme.surface,
          )
        : (isDark ? scheme.surfaceContainerHighest : AppTheme.bubbleIn);
    final dt = DateTime.fromMillisecondsSinceEpoch(message.at * 1000);
    return Align(
      alignment: outgoing ? Alignment.centerRight : Alignment.centerLeft,
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 3),
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.74,
        ),
        decoration: BoxDecoration(
          color: bg,
          borderRadius: BorderRadius.only(
            topLeft: const Radius.circular(16),
            topRight: const Radius.circular(16),
            bottomLeft: Radius.circular(outgoing ? 16 : 4),
            bottomRight: Radius.circular(outgoing ? 4 : 16),
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Text(
              message.content,
              style: const TextStyle(fontSize: 15, height: 1.35),
            ),
            const SizedBox(height: 2),
            Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  _time.format(dt),
                  style: TextStyle(
                    fontSize: 10,
                    color: scheme.onSurfaceVariant,
                  ),
                ),
                if (message.isPending) ...[
                  const SizedBox(width: 4),
                  Icon(
                    Icons.hourglass_empty,
                    size: 11,
                    color: scheme.onSurfaceVariant,
                  ),
                ],
                if (message.failed) ...[
                  const SizedBox(width: 4),
                  Icon(Icons.error_outline, size: 12, color: scheme.error),
                ],
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _Composer extends StatelessWidget {
  const _Composer({
    required this.controller,
    required this.sending,
    required this.onChanged,
    required this.onSend,
  });

  final TextEditingController controller;
  final bool sending;
  final ValueChanged<String> onChanged;
  final VoidCallback onSend;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(10, 4, 10, 8),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            Expanded(
              child: TextField(
                controller: controller,
                onChanged: onChanged,
                minLines: 1,
                maxLines: 5,
                textInputAction: TextInputAction.newline,
                keyboardType: TextInputType.multiline,
                decoration: const InputDecoration(
                  hintText: '输入短信内容',
                  contentPadding: EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 10,
                  ),
                ),
                onSubmitted: (_) => onSend(),
              ),
            ),
            const SizedBox(width: 8),
            SizedBox(
              width: 46,
              height: 46,
              child: FilledButton(
                style: FilledButton.styleFrom(
                  padding: EdgeInsets.zero,
                  shape: const CircleBorder(),
                ),
                onPressed: sending ? null : onSend,
                child: sending
                    ? const SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Icon(Icons.send, size: 20),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

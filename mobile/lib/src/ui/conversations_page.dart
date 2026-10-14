/// Messages tab: conversation list plus compose entry point.
library;

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../state/app_scope.dart';
import '../data/models.dart';
import 'thread_page.dart';

class ConversationsPage extends StatefulWidget {
  const ConversationsPage({super.key});

  @override
  State<ConversationsPage> createState() => _ConversationsPageState();
}

class _ConversationsPageState extends State<ConversationsPage> {
  final TextEditingController _search = TextEditingController();
  final Set<String> _selected = {};
  bool _selecting = false;
  bool _working = false;

  @override
  void initState() {
    super.initState();
    context.read<AppScope>().messages.refreshConversations();
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  Future<void> _openCompose() async {
    final number = await showDialog<String>(
      context: context,
      builder: (context) => const _NewMessageDialog(),
    );
    if (number != null && number.trim().isNotEmpty && mounted) {
      await Navigator.of(context).push(
        MaterialPageRoute<void>(
          builder: (_) => ThreadPage(peer: number.trim()),
        ),
      );
    }
  }

  void _toggle(String key) => setState(() {
    if (!_selected.add(key)) _selected.remove(key);
  });

  Future<void> _mark(Conversation c) async {
    setState(() => _working = true);
    final messages = context.read<AppScope>().messages;
    final ok = await messages.markRead(c.peerKey, read: c.unread > 0);
    if (!mounted) return;
    setState(() => _working = false);
    if (!ok)
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(messages.error ?? '修改失败')));
  }

  Future<void> _delete(List<String> peers, {bool readOnly = false}) async {
    final accepted = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text(readOnly ? '删除已读会话？' : '删除 ${peers.length} 个会话？'),
        content: Text(
          readOnly
              ? '删除网关中所有已读会话的短信记录。未读会话、草稿和正在发送的短信会保留。App 与网页会同步删除，无法恢复。'
              : '删除这些会话中的全部短信。App 与网页会同步删除，无法恢复。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('删除'),
          ),
        ],
      ),
    );
    if (accepted != true || !mounted) return;
    setState(() => _working = true);
    final messages = context.read<AppScope>().messages;
    final ok = await messages.deleteThreads(peers, readOnly: readOnly);
    if (!mounted) return;
    setState(() {
      _working = false;
      if (ok) {
        _selected.clear();
        _selecting = false;
      }
    });
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(ok ? '已删除' : messages.error ?? '删除失败')),
    );
  }

  Future<void> _actions(Conversation c) async {
    final action = await showModalBottomSheet<String>(
      context: context,
      builder: (ctx) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              title: Text(
                c.title,
                style: const TextStyle(fontWeight: FontWeight.bold),
              ),
            ),
            ListTile(
              leading: const Icon(Icons.mark_email_unread_outlined),
              title: Text(c.unread > 0 ? '标为已读' : '标为未读'),
              onTap: () => Navigator.pop(ctx, 'read'),
            ),
            ListTile(
              leading: const Icon(Icons.delete_outline),
              title: const Text('删除会话'),
              onTap: () => Navigator.pop(ctx, 'delete'),
            ),
            ListTile(
              leading: const Icon(Icons.checklist),
              title: const Text('批量选择'),
              onTap: () => Navigator.pop(ctx, 'select'),
            ),
          ],
        ),
      ),
    );
    if (!mounted) return;
    if (action == 'read') await _mark(c);
    if (action == 'delete') await _delete([c.peerKey]);
    if (action == 'select')
      setState(() {
        _selecting = true;
        _selected.add(c.peerKey);
      });
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final items = scope.messages.conversations;
    final selected = items
        .where((c) => _selected.contains(c.peerKey))
        .map((c) => c.peerKey)
        .toList();
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: TextField(
            controller: _search,
            decoration: InputDecoration(
              hintText: '搜索号码或联系人',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: _search.text.isEmpty
                  ? null
                  : IconButton(
                      icon: const Icon(Icons.close),
                      onPressed: () {
                        _search.clear();
                        setState(() {});
                        scope.messages.refreshConversations();
                      },
                    ),
            ),
            onChanged: (v) {
              setState(() {
                _selected.clear();
              });
              scope.messages.refreshConversations(q: v);
            },
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12),
          child: Row(
            children: [
              if (_selecting) ...[
                IconButton(
                  tooltip: '取消选择',
                  onPressed: _working
                      ? null
                      : () => setState(() {
                          _selecting = false;
                          _selected.clear();
                        }),
                  icon: const Icon(Icons.close),
                ),
                Text('已选 ${selected.length}'),
                TextButton(
                  onPressed: _working
                      ? null
                      : () => setState(() {
                          if (selected.length == items.length) {
                            _selected.clear();
                          } else {
                            _selected.addAll(items.map((c) => c.peerKey));
                          }
                        }),
                  child: Text(selected.length == items.length ? '取消全选' : '全选'),
                ),
                const Spacer(),
                IconButton(
                  tooltip: '删除所选',
                  onPressed: _working || selected.isEmpty
                      ? null
                      : () => _delete(selected),
                  icon: const Icon(Icons.delete_outline),
                ),
              ] else ...[
                TextButton.icon(
                  onPressed: _working ? null : _openCompose,
                  icon: const Icon(Icons.edit_outlined),
                  label: const Text('写短信'),
                ),
                const Spacer(),
                PopupMenuButton<String>(
                  tooltip: '短信管理',
                  enabled: !_working,
                  onSelected: (v) {
                    if (v == 'select') setState(() => _selecting = true);
                    if (v == 'read') _delete([], readOnly: true);
                  },
                  itemBuilder: (_) => const [
                    PopupMenuItem(value: 'select', child: Text('批量删除')),
                    PopupMenuItem(value: 'read', child: Text('删除已读')),
                  ],
                ),
              ],
            ],
          ),
        ),
        if (_working || scope.messages.busy) const LinearProgressIndicator(),
        if (scope.messages.error != null)
          Padding(
            padding: const EdgeInsets.all(8),
            child: Text(
              scope.messages.error!,
              style: TextStyle(color: Theme.of(context).colorScheme.error),
            ),
          ),
        Expanded(
          child: items.isEmpty
              ? _EmptyState(
                  searching: _search.text.isNotEmpty,
                  onCompose: _openCompose,
                )
              : RefreshIndicator(
                  onRefresh: () =>
                      scope.messages.refreshConversations(q: _search.text),
                  child: ListView.separated(
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: items.length,
                    separatorBuilder: (_, __) =>
                        const Divider(height: 1, indent: 72),
                    itemBuilder: (context, i) {
                      final c = items[i];
                      return ListTile(
                        key: ValueKey('conversation-${c.peerKey}'),
                        selected: _selecting && _selected.contains(c.peerKey),
                        leading: _selecting
                            ? Checkbox(
                                value: _selected.contains(c.peerKey),
                                onChanged: _working
                                    ? null
                                    : (_) => _toggle(c.peerKey),
                              )
                            : Badge(
                                isLabelVisible: c.unread > 0,
                                label: Text(
                                  c.unread > 99 ? '99+' : '${c.unread}',
                                ),
                                child: CircleAvatar(
                                  radius: 24,
                                  child: Text(
                                    c.title.isEmpty
                                        ? '?'
                                        : c.title.substring(0, 1),
                                  ),
                                ),
                              ),
                        title: Text(
                          c.title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontWeight: c.unread > 0
                                ? FontWeight.w600
                                : FontWeight.normal,
                          ),
                        ),
                        subtitle: Text(
                          c.draft?.isNotEmpty == true
                              ? '[草稿] ${c.draft}'
                              : c.lastContent,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                        trailing: Text(
                          _when(c.lastAt),
                          style: TextStyle(
                            fontSize: 11,
                            color: Theme.of(context)
                                .colorScheme
                                .onSurfaceVariant,
                          ),
                        ),
                        onLongPress: _working ? null : () => _actions(c),
                        onTap: _working
                            ? null
                            : () async {
                                if (_selecting) {
                                  _toggle(c.peerKey);
                                  return;
                                }
                                await Navigator.of(context).push(
                                  MaterialPageRoute<void>(
                                    builder: (_) => ThreadPage(
                                      peer: c.peer,
                                      peerKey: c.peerKey,
                                      title: c.title,
                                    ),
                                  ),
                                );
                              },
                      );
                    },
                  ),
                ),
        ),
      ],
    );
  }

  static String _when(int epoch) {
    if (epoch <= 0) return '';
    final dt = DateTime.fromMillisecondsSinceEpoch(epoch * 1000);
    final now = DateTime.now();
    if (dt.year == now.year && dt.month == now.month && dt.day == now.day)
      return DateFormat('HH:mm').format(dt);
    return DateFormat(dt.year == now.year ? 'MM-dd' : 'yyyy-MM-dd').format(dt);
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.searching, required this.onCompose});
  final bool searching;
  final VoidCallback onCompose;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.forum_outlined,
            size: 56,
            color: Theme.of(context).colorScheme.onSurfaceVariant,
          ),
          const SizedBox(height: 12),
          Text(searching ? '没有匹配的会话' : '还没有短信'),
          const SizedBox(height: 16),
          if (!searching)
            FilledButton.icon(
              onPressed: onCompose,
              icon: const Icon(Icons.edit_outlined),
              label: const Text('写短信'),
            ),
        ],
      ),
    );
  }
}

class _NewMessageDialog extends StatefulWidget {
  const _NewMessageDialog();

  @override
  State<_NewMessageDialog> createState() => _NewMessageDialogState();
}

class _NewMessageDialogState extends State<_NewMessageDialog> {
  final _controller = TextEditingController();

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final favorites = scope.contacts.favorites;
    return AlertDialog(
      title: const Text('收件号码'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _controller,
            autofocus: true,
            keyboardType: TextInputType.phone,
            decoration: const InputDecoration(hintText: '+8613800138000'),
            onSubmitted: (v) => Navigator.pop(context, v),
          ),
          if (favorites.isNotEmpty) ...[
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: Text(
                '常用联系人',
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onSurfaceVariant,
                ),
              ),
            ),
            const SizedBox(height: 4),
            Wrap(
              spacing: 8,
              children: [
                for (final c in favorites.take(6))
                  ActionChip(
                    label: Text(c.name),
                    onPressed: () => Navigator.pop(context, c.number),
                  ),
              ],
            ),
          ],
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton(
          onPressed: () => Navigator.pop(context, _controller.text),
          child: const Text('下一步'),
        ),
      ],
    );
  }
}

/// Small helper so other tabs can jump straight into a thread.
class ComposeEntry {
  static Future<void> open(BuildContext context, String number) => Navigator.of(
    context,
  ).push(MaterialPageRoute<void>(builder: (_) => ThreadPage(peer: number)));
}

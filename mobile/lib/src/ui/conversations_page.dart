/// Messages tab: conversation list plus compose entry point.
library;

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../state/app_scope.dart';
import 'thread_page.dart';

class ConversationsPage extends StatefulWidget {
  const ConversationsPage({super.key});

  @override
  State<ConversationsPage> createState() => _ConversationsPageState();
}

class _ConversationsPageState extends State<ConversationsPage> {
  final TextEditingController _search = TextEditingController();
  bool _searching = false;

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
      await Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => ThreadPage(peer: number.trim()),
      ));
    }
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final items = scope.messages.conversations;
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: TextField(
            controller: _search,
            decoration: InputDecoration(
              hintText: '搜索号码或内容',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: _search.text.isNotEmpty
                  ? IconButton(
                      icon: const Icon(Icons.close),
                      onPressed: () {
                        _search.clear();
                        setState(() => _searching = false);
                        scope.messages.refreshConversations();
                      },
                    )
                  : null,
            ),
            onChanged: (v) {
              setState(() {});
              scope.messages.refreshConversations(q: v.isEmpty ? null : v);
            },
            onSubmitted: (v) => setState(() => _searching = v.isNotEmpty),
          ),
        ),
        Expanded(
          child: items.isEmpty
              ? _EmptyState(searching: _searching, onCompose: _openCompose)
              : RefreshIndicator(
                  onRefresh: () => scope.messages.refreshConversations(
                      q: _search.text.isEmpty ? null : _search.text),
                  child: ListView.separated(
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: items.length,
                    separatorBuilder: (_, __) => const Divider(height: 1, indent: 72),
                    itemBuilder: (context, i) {
                      final c = items[i];
                      return ListTile(
                        leading: Stack(
                          children: [
                            CircleAvatar(
                              radius: 24,
                              child: Text(c.title.isEmpty ? '?' : c.title.substring(0, 1).toUpperCase()),
                            ),
                            if (c.unread > 0)
                              Positioned(
                                right: 0,
                                top: 0,
                                child: Container(
                                  padding: const EdgeInsets.all(4),
                                  decoration: BoxDecoration(
                                    color: Theme.of(context).colorScheme.primary,
                                    shape: BoxShape.circle,
                                  ),
                                  child: Text(
                                    c.unread > 9 ? '9+' : c.unread.toString(),
                                    style: const TextStyle(color: Colors.white, fontSize: 9),
                                  ),
                                ),
                              ),
                          ],
                        ),
                        title: Text(c.title,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                                fontWeight:
                                    c.unread > 0 ? FontWeight.w600 : FontWeight.normal)),
                        subtitle: Text(
                          (c.draft != null && c.draft!.isNotEmpty ? '[草稿] ' + c.draft! : c.lastContent),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                        ),
                        trailing: Text(
                          _when(c.lastAt),
                          style: TextStyle(
                              fontSize: 11, color: Theme.of(context).colorScheme.onSurfaceVariant),
                        ),
                        onTap: () => Navigator.of(context).push(MaterialPageRoute<void>(
                          builder: (_) => ThreadPage(peer: c.peer, peerKey: c.peerKey, title: c.title),
                        )),
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
    final sameDay = dt.year == now.year && dt.month == now.month && dt.day == now.day;
    if (sameDay) return DateFormat('HH:mm').format(dt);
    if (dt.year == now.year) return DateFormat('MM-dd').format(dt);
    return DateFormat('yyyy-MM-dd').format(dt);
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
          Icon(Icons.forum_outlined,
              size: 56, color: Theme.of(context).colorScheme.onSurfaceVariant),
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
                child: Text('常用联系人',
                    style: TextStyle(color: Theme.of(context).colorScheme.onSurfaceVariant))),
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
        TextButton(onPressed: () => Navigator.pop(context), child: const Text('取消')),
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
  static Future<void> open(BuildContext context, String number) =>
      Navigator.of(context).push(MaterialPageRoute<void>(
        builder: (_) => ThreadPage(peer: number),
      ));
}


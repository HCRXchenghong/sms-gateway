/// Contacts tab with search, grouping and an editor.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../config/app_theme.dart';
import '../data/models.dart';
import '../state/app_scope.dart';
import 'thread_page.dart';

class ContactsPage extends StatefulWidget {
  const ContactsPage({super.key});

  @override
  State<ContactsPage> createState() => _ContactsPageState();
}

class _ContactsPageState extends State<ContactsPage> {
  final TextEditingController _search = TextEditingController();

  @override
  void initState() {
    super.initState();
    context.read<AppScope>().contacts.refresh();
  }

  @override
  void dispose() {
    _search.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final all = scope.contacts.items;
    final q = _search.text.trim().toLowerCase();
    final items = q.isEmpty
        ? all
        : all
            .where((c) =>
                c.name.toLowerCase().contains(q) || c.number.contains(q))
            .toList();
    items.sort((a, b) {
      if (a.favorite != b.favorite) return a.favorite ? -1 : 1;
      return a.name.compareTo(b.name);
    });

    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
          child: TextField(
            controller: _search,
            decoration: InputDecoration(
              hintText: '搜索姓名或号码',
              prefixIcon: const Icon(Icons.search),
              suffixIcon: _search.text.isNotEmpty
                  ? IconButton(
                      icon: const Icon(Icons.close),
                      onPressed: () {
                        _search.clear();
                        setState(() {});
                      },
                    )
                  : null,
            ),
            onChanged: (_) => setState(() {}),
          ),
        ),
        Expanded(
          child: items.isEmpty
              ? Center(
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(Icons.person_add_alt,
                          size: 52,
                          color: Theme.of(context).colorScheme.onSurfaceVariant),
                      const SizedBox(height: 12),
                      const Text('通讯录还是空的'),
                      const SizedBox(height: 16),
                      FilledButton.icon(
                        icon: const Icon(Icons.add),
                        label: const Text('新建联系人'),
                        onPressed: () => _edit(context, null),
                      ),
                    ],
                  ),
                )
              : RefreshIndicator(
                  onRefresh: () => scope.contacts.refresh(),
                  child: ListView.separated(
                    physics: const AlwaysScrollableScrollPhysics(),
                    itemCount: items.length,
                    separatorBuilder: (_, __) => const Divider(height: 1, indent: 72),
                    itemBuilder: (context, i) {
                      final c = items[i];
                      return ListTile(
                        leading: CircleAvatar(
                          backgroundColor: c.favorite
                              ? AppTheme.call.withValues(alpha: 0.18)
                              : null,
                          child: Text(c.initial),
                        ),
                        title: Text(c.name),
                        subtitle: Text(c.number +
                            (c.groupName == null || c.groupName!.isEmpty
                                ? ''
                                : ' · ' + c.groupName!)),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            IconButton(
                              tooltip: '发短信',
                              icon: const Icon(Icons.chat_bubble_outline),
                              onPressed: () => Navigator.of(context)
                                  .push(MaterialPageRoute<void>(
                                builder: (_) =>
                                    ThreadPage(peer: c.number, title: c.name),
                              )),
                            ),
                            if (scope.calls.voiceEnabled)
                              IconButton(
                                tooltip: '拨打',
                                icon: const Icon(Icons.call, color: AppTheme.call),
                                onPressed: () => scope.dial(c.number),
                              ),
                          ],
                        ),
                        onTap: () => _edit(context, c),
                      );
                    },
                  ),
                ),
        ),
      ],
    );
  }

  Future<void> _edit(BuildContext context, Contact? existing) async {
    final scope = context.read<AppScope>();
    final saved = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _ContactEditor(contact: existing),
    );
    if (saved == true) {
      await scope.contacts.refresh();
      if (context.mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('已保存')));
      }
    }
  }
}

class _ContactEditor extends StatefulWidget {
  const _ContactEditor({this.contact});
  final Contact? contact;

  @override
  State<_ContactEditor> createState() => _ContactEditorState();
}

class _ContactEditorState extends State<_ContactEditor> {
  late final TextEditingController _name =
      TextEditingController(text: widget.contact?.name ?? '');
  late final TextEditingController _number =
      TextEditingController(text: widget.contact?.number ?? '');
  late final TextEditingController _group =
      TextEditingController(text: widget.contact?.groupName ?? '');
  late final TextEditingController _note =
      TextEditingController(text: widget.contact?.note ?? '');
  late bool _favorite = widget.contact?.favorite ?? false;
  bool _saving = false;

  @override
  void dispose() {
    _name.dispose();
    _number.dispose();
    _group.dispose();
    _note.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (_name.text.trim().isEmpty || _number.text.trim().length < 2) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('姓名和号码都要填写')));
      return;
    }
    setState(() => _saving = true);
    final ok = await context.read<AppScope>().contacts.save(Contact(
          id: widget.contact?.id ?? 0,
          name: _name.text.trim(),
          number: _number.text.trim(),
          groupName: _group.text.trim().isEmpty ? null : _group.text.trim(),
          note: _note.text.trim().isEmpty ? null : _note.text.trim(),
          favorite: _favorite,
        ));
    if (!mounted) return;
    if (ok) {
      Navigator.pop(context, true);
    } else {
      setState(() => _saving = false);
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text('保存失败: ' +
              (context.read<AppScope>().contacts.error ?? '未知错误'))));
    }
  }

  Future<void> _delete() async {
    final id = widget.contact?.id;
    if (id == null) return;
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('删除联系人'),
        content: Text('确定删除「' + widget.contact!.name + '」吗？'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消')),
          FilledButton(
              onPressed: () => Navigator.pop(context, true),
              child: const Text('删除')),
        ],
      ),
    );
    if (confirmed != true || !mounted) return;
    await context.read<AppScope>().contacts.remove(id);
    if (mounted) Navigator.pop(context, true);
  }

  @override
  Widget build(BuildContext context) {
    final isEdit = widget.contact != null;
    return Padding(
      padding: EdgeInsets.fromLTRB(
          20, 0, 20, MediaQuery.of(context).viewInsets.bottom + 24),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(isEdit ? '编辑联系人' : '新建联系人',
              style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 16),
          TextField(
              controller: _name,
              decoration: const InputDecoration(labelText: '姓名'),
              textInputAction: TextInputAction.next),
          const SizedBox(height: 12),
          TextField(
              controller: _number,
              keyboardType: TextInputType.phone,
              decoration: const InputDecoration(labelText: '号码', hintText: '+8613800138000'),
              textInputAction: TextInputAction.next),
          const SizedBox(height: 12),
          TextField(
              controller: _group,
              decoration: const InputDecoration(labelText: '分组（可选）'),
              textInputAction: TextInputAction.next),
          const SizedBox(height: 12),
          TextField(
              controller: _note,
              maxLines: 2,
              decoration: const InputDecoration(labelText: '备注（可选）')),
          const SizedBox(height: 8),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: _favorite,
            onChanged: (v) => setState(() => _favorite = v),
            title: const Text('设为常用联系人'),
          ),
          const SizedBox(height: 8),
          FilledButton(
            onPressed: _saving ? null : _save,
            child: _saving
                ? const SizedBox(
                    height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2))
                : const Text('保存'),
          ),
          if (isEdit) ...[
            const SizedBox(height: 8),
            OutlinedButton.icon(
              style: OutlinedButton.styleFrom(foregroundColor: AppTheme.hangup),
              icon: const Icon(Icons.delete_outline),
              label: const Text('删除联系人'),
              onPressed: _delete,
            ),
          ],
        ],
      ),
    );
  }
}


/// Admin only: create accounts, change roles, enable or disable, reset
/// passwords and delete. Mirrors the web console's account management.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../data/models.dart';
import '../state/app_scope.dart';

class AccountsPage extends StatefulWidget {
  const AccountsPage({super.key});

  @override
  State<AccountsPage> createState() => _AccountsPageState();
}

class _AccountsPageState extends State<AccountsPage> {
  @override
  void initState() {
    super.initState();
    context.read<AppScope>().users.refresh();
  }

  void _toast(String message) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final users = scope.users.items;
    return Scaffold(
      appBar: AppBar(
        title: const Text('账号管理'),
        actions: [
          IconButton(
            icon: const Icon(Icons.refresh),
            onPressed: () => scope.users.refresh(),
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _create(scope),
        icon: const Icon(Icons.person_add_alt),
        label: const Text('新建账号'),
      ),
      body: users.isEmpty
          ? Center(
              child: scope.users.busy
                  ? const CircularProgressIndicator()
                  : const Text('没有账号'))
          : ListView.separated(
              padding: const EdgeInsets.only(bottom: 88),
              itemCount: users.length,
              separatorBuilder: (_, __) => const Divider(height: 1, indent: 68),
              itemBuilder: (context, i) {
                final u = UserProfile.fromJson(users[i]);
                final isSelf = scope.user?.id == u.id;
                return ListTile(
                  leading: CircleAvatar(
                    child: Text(u.label.substring(0, 1).toUpperCase()),
                  ),
                  title: Text(u.label + (isSelf ? '（我）' : '')),
                  subtitle: Text(u.username +
                      ' · ' +
                      (u.isAdmin ? '管理员' : '坐席') +
                      (u.totpEnabled ? ' · 已开启双因子' : '') +
                      (u.mustChangePassword ? ' · 需改密' : '')),
                  trailing: Switch(
                    value: u.isActive,
                    onChanged: isSelf
                        ? null
                        : (v) async {
                            final ok = await scope.users.update(u.id, isActive: v);
                            _toast(ok ? (v ? '已启用' : '已停用') : '操作失败');
                          },
                  ),
                  onTap: () => _actions(scope, u, isSelf),
                );
              },
            ),
    );
  }

  Future<void> _create(AppScope scope) async {
    final username = TextEditingController();
    final password = TextEditingController();
    final displayName = TextEditingController();
    String role = 'operator';
    final saved = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => StatefulBuilder(
        builder: (context, setState) => AlertDialog(
          title: const Text('新建账号'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                    controller: username,
                    decoration: const InputDecoration(labelText: '登录名'),
                    autocorrect: false),
                const SizedBox(height: 12),
                TextField(
                    controller: displayName,
                    decoration: const InputDecoration(labelText: '显示名（可选）')),
                const SizedBox(height: 12),
                TextField(
                    controller: password,
                    obscureText: true,
                    decoration: const InputDecoration(labelText: '初始密码')),
                const SizedBox(height: 12),
                DropdownButtonFormField<String>(
                  value: role,
                  decoration: const InputDecoration(labelText: '角色'),
                  items: const [
                    DropdownMenuItem(value: 'operator', child: Text('坐席（收发短信与拨号）')),
                    DropdownMenuItem(value: 'admin', child: Text('管理员（可管理账号）')),
                  ],
                  onChanged: (v) => setState(() => role = v ?? 'operator'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(dialogContext, false),
                child: const Text('取消')),
            FilledButton(
                onPressed: () => Navigator.pop(dialogContext, true),
                child: const Text('创建')),
          ],
        ),
      ),
    );
    if (saved != true) return;
    if (username.text.trim().isEmpty || password.text.isEmpty) {
      _toast('登录名和密码不能为空');
      return;
    }
    final ok = await scope.users.create(
      username: username.text.trim(),
      password: password.text,
      role: role,
      displayName: displayName.text.trim().isEmpty ? null : displayName.text.trim(),
    );
    _toast(ok ? '账号已创建' : '创建失败: ' + (scope.users.error ?? ''));
  }

  Future<void> _actions(AppScope scope, UserProfile u, bool isSelf) async {
    final choice = await showModalBottomSheet<String>(
      context: context,
      showDragHandle: true,
      builder: (sheetContext) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(u.label, style: Theme.of(sheetContext).textTheme.titleMedium),
            ),
            ListTile(
              leading: const Icon(Icons.badge_outlined),
              title: const Text('切换角色'),
              subtitle: Text(u.isAdmin ? '当前是管理员' : '当前是坐席'),
              onTap: () => Navigator.pop(sheetContext, 'role'),
            ),
            ListTile(
              leading: const Icon(Icons.key_outlined),
              title: const Text('重置密码'),
              onTap: () => Navigator.pop(sheetContext, 'password'),
            ),
            if (!isSelf)
              ListTile(
                leading: const Icon(Icons.delete_outline, color: Colors.redAccent),
                title: const Text('删除账号', style: TextStyle(color: Colors.redAccent)),
                onTap: () => Navigator.pop(sheetContext, 'delete'),
              ),
          ],
        ),
      ),
    );
    if (choice == null) return;
    switch (choice) {
      case 'role':
        final next = u.isAdmin ? 'operator' : 'admin';
        final ok = await scope.users.update(u.id, role: next);
        _toast(ok ? '角色已改为 ' + (next == 'admin' ? '管理员' : '坐席') : '操作失败');
        break;
      case 'password':
        final controller = TextEditingController();
        final ok = await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            title: const Text('重置密码'),
            content: TextField(
                controller: controller,
                obscureText: true,
                autofocus: true,
                decoration: const InputDecoration(labelText: '新密码')),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(dialogContext, false),
                  child: const Text('取消')),
              FilledButton(
                  onPressed: () => Navigator.pop(dialogContext, true),
                  child: const Text('确定')),
            ],
          ),
        );
        if (ok != true || controller.text.isEmpty) return;
        final done = await scope.users.resetPassword(u.id, controller.text);
        _toast(done ? '密码已重置' : '重置失败: ' + (scope.users.error ?? ''));
        break;
      case 'delete':
        final confirmed = await showDialog<bool>(
          context: context,
          builder: (dialogContext) => AlertDialog(
            title: const Text('删除账号'),
            content: Text('确定删除「' + u.username + '」吗？该操作不可撤销。'),
            actions: [
              TextButton(
                  onPressed: () => Navigator.pop(dialogContext, false),
                  child: const Text('取消')),
              FilledButton(
                  onPressed: () => Navigator.pop(dialogContext, true),
                  child: const Text('删除')),
            ],
          ),
        );
        if (confirmed != true) return;
        final removed = await scope.users.remove(u.id);
        _toast(removed ? '已删除' : '删除失败: ' + (scope.users.error ?? ''));
        break;
    }
  }
}


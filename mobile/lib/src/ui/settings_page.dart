/// Account tab: who is logged in, what the gateway reports, and the entry
/// points for sound card, AI, device and account administration.
library;

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../state/app_scope.dart';
import 'accounts_page.dart';
import 'ai_settings_page.dart';
import 'audio_settings_page.dart';
import 'devices_page.dart';

class SettingsPage extends StatelessWidget {
  const SettingsPage({super.key});

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final user = scope.user;
    final scheme = Theme.of(context).colorScheme;

    return ListView(
      padding: const EdgeInsets.only(bottom: 32),
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 8),
          child: Row(
            children: [
              CircleAvatar(radius: 26, child: Text((user?.label ?? '?').substring(0, 1).toUpperCase())),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(user?.label ?? '未登录',
                        style: Theme.of(context).textTheme.titleMedium),
                    Text((user?.username ?? '') + ' · ' + (user?.isAdmin == true ? '管理员' : '坐席'),
                        style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant)),
                  ],
                ),
              ),
              IconButton(
                tooltip: '刷新',
                icon: const Icon(Icons.refresh),
                onPressed: () => scope.restore(),
              ),
            ],
          ),
        ),
        const Divider(height: 1),
        _Section(title: '网关', children: [
          _NavTile(
            icon: Icons.memory_outlined,
            title: '设备与 AT 调试',
            subtitle: scope.devices.items.isEmpty
                ? '尚未发现模块'
                : scope.devices.online.length.toString() +
                    ' / ' +
                    scope.devices.items.length.toString() +
                    ' 在线',
            onTap: () => _push(context, const DevicesPage()),
          ),
          _NavTile(
            icon: Icons.graphic_eq_outlined,
            title: '声卡与提示音',
            subtitle: _audioSubtitle(scope),
            onTap: () => _push(context, const AudioSettingsPage()),
          ),
          _NavTile(
            icon: Icons.smart_toy_outlined,
            title: 'AI 语音与对话',
            subtitle: '对话供应商 ' + scope.ai.provider + (scope.ai.loaded ? '' : '（未就绪）'),
            onTap: () => _push(context, const AiSettingsPage()),
          ),
        ]),
        if (scope.caps.admin)
          _Section(title: '管理', children: [
            _NavTile(
              icon: Icons.group_outlined,
              title: '账号管理',
              subtitle: '新建账号、改角色、重置密码、停用',
              onTap: () => _push(context, const AccountsPage()),
            ),
            _NavTile(
              icon: Icons.insights_outlined,
              title: '运行统计',
              subtitle: _statsSubtitle(scope),
              onTap: () => _showStats(context, scope),
            ),
          ]),
        _Section(title: '账号', children: [
          _NavTile(
            icon: Icons.lock_outline,
            title: '修改密码',
            onTap: () => _changePassword(context, scope),
          ),
          _NavTile(
            icon: Icons.info_outline,
            title: '关于',
            subtitle: (scope.api.baseUrl ?? '') ,
            onTap: () => _showAbout(context, scope),
          ),
          _NavTile(
            icon: Icons.logout,
            title: '退出登录',
            destructive: true,
            onTap: () => _confirmLogout(context, scope),
          ),
        ]),
      ],
    );
  }

  String _audioSubtitle(AppScope scope) {
    final audio = scope.audio.state;
    if (audio == null) return '未加载';
    final out = audio.outputs
        .where((d) => d.id == audio.outputDevice)
        .map((d) => d.name)
        .firstOrNull;
    return '输出 ' + (out ?? '系统默认') + ' · ' + audio.outputs.length.toString() + ' 个声卡';
  }

  String _statsSubtitle(AppScope scope) {
    final s = scope.settings.stats;
    if (s.isEmpty) return '点击查看短信与通话统计';
    return '短信 ' +
        (s['sms_inbox'] ?? 0).toString() +
        ' 收 / ' +
        (s['sms_outbox'] ?? 0).toString() +
        ' 发 · 通话 ' +
        ((s['calls'] as Map?)?['total'] ?? 0).toString();
  }

  void _push(BuildContext context, Widget page) =>
      Navigator.of(context).push(MaterialPageRoute<void>(builder: (_) => page));

  Future<void> _changePassword(BuildContext context, AppScope scope) async {
    final current = TextEditingController();
    final next = TextEditingController();
    final confirm = TextEditingController();
    final ok = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('修改密码'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                  controller: current,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: '当前密码')),
              const SizedBox(height: 12),
              TextField(
                  controller: next,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: '新密码')),
              const SizedBox(height: 12),
              TextField(
                  controller: confirm,
                  obscureText: true,
                  decoration: const InputDecoration(labelText: '确认新密码')),
            ],
          ),
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: const Text('取消')),
          FilledButton(
              onPressed: () => Navigator.pop(dialogContext, true),
              child: const Text('提交')),
        ],
      ),
    );
    if (ok != true || !context.mounted) return;
    if (next.text.isEmpty || next.text != confirm.text) {
      _toast(context, '两次输入的新密码不一致');
      return;
    }
    try {
      await scope.api.changeMyPassword(current.text, next.text);
      _toast(context, '密码已修改');
    } catch (e) {
      _toast(context, '修改失败: ' + e.toString());
    }
  }

  Future<void> _showStats(BuildContext context, AppScope scope) async {
    await scope.settings.refreshStats();
    if (!context.mounted) return;
    final s = scope.settings.stats;
    final calls = (s['calls'] as Map?) ?? const {};
    await showDialog<void>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('运行统计'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            _stat(dialogContext, '设备在线',
                (s['devices_online'] ?? 0).toString() + ' / ' + (s['devices_total'] ?? 0).toString()),
            _stat(dialogContext, '收件短信', (s['sms_inbox'] ?? 0).toString()),
            _stat(dialogContext, '发件短信', (s['sms_outbox'] ?? 0).toString()),
            _stat(dialogContext, '账号数', (s['users_total'] ?? 0).toString()),
            const Divider(height: 20),
            _stat(dialogContext, '近 7 天通话', (calls['total'] ?? 0).toString()),
            _stat(dialogContext, '接通率', ((calls['answer_rate'] ?? 0) as num).toStringAsFixed(1) + '%'),
            _stat(dialogContext, '通话总时长', (calls['talk_seconds'] ?? 0).toString() + ' 秒'),
          ],
        ),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(dialogContext), child: const Text('关闭')),
        ],
      ),
    );
  }

  Widget _stat(BuildContext context, String label, String value) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(
          children: [
            SizedBox(width: 110, child: Text(label)),
            Text(value, style: const TextStyle(fontWeight: FontWeight.w600)),
          ],
        ),
      );

  void _showAbout(BuildContext context, AppScope scope) {
    showAboutDialog(
      context: context,
      applicationName: 'VoiceSMS Gateway',
      applicationVersion: '1.0.0',
      applicationIcon: const Icon(Icons.cell_tower, size: 40),
      children: [
        const SizedBox(height: 8),
        Text('服务器: ' + (scope.api.baseUrl ?? '未配置')),
        const SizedBox(height: 4),
        const Text('硬件: SIMCom A7670X / A7670G 4G USB 模块'),
        const SizedBox(height: 4),
        const Text('语音通话、短信收发、AI 语音助手与多账号管理。'),
      ],
    );
  }

  Future<void> _confirmLogout(BuildContext context, AppScope scope) async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('退出登录'),
        content: const Text('退出后需要重新输入服务器地址与账号密码。'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(dialogContext, false),
              child: const Text('取消')),
          FilledButton(
              onPressed: () => Navigator.pop(dialogContext, true),
              child: const Text('退出')),
        ],
      ),
    );
    if (ok == true) await scope.logout();
  }

  void _toast(BuildContext context, String message) {
    if (!context.mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }
}

class _Section extends StatelessWidget {
  const _Section({required this.title, required this.children});
  final String title;
  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 18, 16, 6),
          child: Text(title.toUpperCase(),
              style: TextStyle(
                  fontSize: 11,
                  letterSpacing: 1,
                  fontWeight: FontWeight.w600,
                  color: Theme.of(context).colorScheme.primary)),
        ),
        ...children,
      ],
    );
  }
}

class _NavTile extends StatelessWidget {
  const _NavTile({
    required this.icon,
    required this.title,
    this.subtitle,
    this.onTap,
    this.destructive = false,
  });

  final IconData icon;
  final String title;
  final String? subtitle;
  final VoidCallback? onTap;
  final bool destructive;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final color = destructive ? scheme.error : scheme.onSurface;
    return ListTile(
      leading: Icon(icon, color: color),
      title: Text(title, style: TextStyle(color: color)),
      subtitle: subtitle == null ? null : Text(subtitle!),
      trailing: onTap == null ? null : const Icon(Icons.chevron_right, size: 20),
      onTap: onTap,
    );
  }
}


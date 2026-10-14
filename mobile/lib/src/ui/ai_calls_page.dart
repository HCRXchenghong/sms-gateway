/// AI 代打 tab: place a call with a stated purpose and read back the result.
///
/// A task is one phone call the gateway runs itself, either dialling out or
/// waiting armed for an inbound call. Task events arrive on the socket, so a
/// queued task turns into 通话中 and then 已完成 without pulling to refresh.
library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:provider/provider.dart';

import '../state/app_scope.dart';

const Map<String, String> _statusLabels = {
  'queued': '排队中',
  'dialing': '拨号中',
  'in_call': '通话中',
  'finished': '已完成',
  'failed': '失败',
  'cancelled': '已取消',
  'no_answer': '无人接听',
  'rejected': '已拒接',
  'busy': '占线',
};

const Set<String> _terminalStatuses = {
  'finished',
  'failed',
  'cancelled',
  'no_answer',
  'rejected',
  'busy',
};

bool _isTerminal(String status) => _terminalStatuses.contains(status);

String _statusLabel(String status) => _statusLabels[status] ?? status;

IconData _statusIcon(String status) => switch (status) {
  'queued' => Icons.schedule,
  'dialing' => Icons.phone_in_talk_outlined,
  'in_call' => Icons.phone_in_talk,
  'finished' => Icons.check_circle_outline,
  'failed' => Icons.error_outline,
  'cancelled' => Icons.cancel_outlined,
  'no_answer' => Icons.phone_missed_outlined,
  'rejected' => Icons.phone_disabled_outlined,
  'busy' => Icons.remove_circle_outline,
  _ => Icons.help_outline,
};

Color _statusColor(String status, ColorScheme scheme) => switch (status) {
  'in_call' || 'dialing' => Colors.green,
  'queued' => scheme.primary,
  'finished' => scheme.tertiary,
  'failed' || 'rejected' => scheme.error,
  'no_answer' || 'busy' => Colors.orange.shade700,
  _ => scheme.onSurfaceVariant,
};

String _text(Map<String, dynamic> map, String key) {
  final value = map[key];
  if (value == null) return '';
  return value.toString();
}

String _when(int? epoch) {
  if (epoch == null || epoch <= 0) return '—';
  return DateFormat('MM-dd HH:mm')
      .format(DateTime.fromMillisecondsSinceEpoch(epoch * 1000));
}

int? _asInt(Object? value) => value is num ? value.toInt() : null;

class AiCallsPage extends StatefulWidget {
  const AiCallsPage({super.key});

  @override
  State<AiCallsPage> createState() => _AiCallsPageState();
}

class _AiCallsPageState extends State<AiCallsPage> {
  /// 'all', 'live', 'done' or 'failed' — the bucket shown in the list.
  String _filter = 'all';

  @override
  void initState() {
    super.initState();
    final ai = context.read<AppScope>().aiCalls;
    unawaited(ai.refreshTasks());
    unawaited(ai.refreshState());
  }

  List<Map<String, dynamic>> _visible(List<Map<String, dynamic>> tasks) {
    switch (_filter) {
      case 'live':
        return tasks.where((t) => !_isTerminal(_text(t, 'status'))).toList();
      case 'done':
        return tasks
            .where((t) =>
                _text(t, 'status') == 'finished' ||
                _text(t, 'status') == 'cancelled')
            .toList();
      case 'failed':
        return tasks
            .where((t) =>
                _text(t, 'status') == 'failed' ||
                _text(t, 'status') == 'no_answer' ||
                _text(t, 'status') == 'rejected' ||
                _text(t, 'status') == 'busy')
            .toList();
      default:
        return tasks;
    }
  }

  Future<void> _create() async {
    final draft = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (_) => const _CreateTaskDialog(),
    );
    if (draft == null || !mounted) return;
    final ai = context.read<AppScope>().aiCalls;
    final result = await ai.createTask(
      purpose: draft['purpose'] as String,
      number: draft['number'] as String?,
      direction: draft['direction'] as String,
      context: draft['context'] as String?,
      peerLanguage: draft['peerLanguage'] as String?,
      maxSeconds: draft['maxSeconds'] as int?,
    );
    if (!mounted) return;
    if (result == null) {
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(ai.error ?? '任务创建失败')));
      return;
    }
    final task = result['task'];
    final id = task is Map ? task['id'] : null;
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(
      content: Text(result['idempotent'] == true
          ? '这个请求已经提交过，返回原来的任务'
          : '已创建任务' + (id == null ? '' : ' #' + id.toString())),
    ));
    if (task is Map) await _openDetail(Map<String, dynamic>.from(task));
  }

  Future<void> _openDetail(Map<String, dynamic> task) =>
      showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        useSafeArea: true,
        builder: (_) => _TaskDetailSheet(taskId: _asInt(task['id']) ?? 0),
      );

  /// The gateway cannot hear a call when no capture device is attached, so an
  /// AI task would run deaf. Say so before the operator relies on it.
  String? _captureWarning(Map<String, dynamic> state) {
    final capture = state['capture'];
    if (capture is! List || capture.isEmpty) return null;
    var broken = 0;
    for (final entry in capture) {
      if (entry is Map && (entry['error'] ?? '').toString().isNotEmpty) broken++;
    }
    if (broken == 0) return null;
    return broken.toString() + ' 个采集设备异常，AI 代打和实时翻译会听不到对方说话';
  }

  @override
  Widget build(BuildContext context) {
    final scope = context.watch<AppScope>();
    final ai = scope.aiCalls;
    final tasks = _visible(ai.tasks);
    final warning = _captureWarning(ai.state);
    return Scaffold(
      floatingActionButton: FloatingActionButton.extended(
        onPressed: ai.busy ? null : _create,
        icon: const Icon(Icons.add_call, size: 20),
        label: const Text('新建'),
      ),
      body: Column(
      children: [
        if (warning != null)
          MaterialBanner(
            backgroundColor: Theme.of(context).colorScheme.errorContainer,
            leading: const Icon(Icons.mic_off_outlined),
            content: Text(warning),
            actions: [
              TextButton(
                onPressed: () => unawaited(ai.refreshState()),
                child: const Text('重新检测'),
              ),
            ],
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(12, 8, 12, 0),
          child: Row(
            children: [
              Expanded(
                child: SingleChildScrollView(
                  scrollDirection: Axis.horizontal,
                  child: Row(
                    children: [
                      _FilterChip(
                        label: '全部',
                        selected: _filter == 'all',
                        onTap: () => setState(() => _filter = 'all'),
                      ),
                      _FilterChip(
                        label: '进行中',
                        selected: _filter == 'live',
                        onTap: () => setState(() => _filter = 'live'),
                      ),
                      _FilterChip(
                        label: '已结束',
                        selected: _filter == 'done',
                        onTap: () => setState(() => _filter = 'done'),
                      ),
                      _FilterChip(
                        label: '未接通',
                        selected: _filter == 'failed',
                        onTap: () => setState(() => _filter = 'failed'),
                      ),
                    ],
                  ),
                ),
              ),
            ],
          ),
        ),
        Expanded(
          child: ai.busy && !ai.tasksLoaded
              ? const Center(child: CircularProgressIndicator())
              : tasks.isEmpty
                  ? _EmptyState(filter: _filter, onCreate: _create)
                  : RefreshIndicator(
                      onRefresh: ai.refreshTasks,
                      child: ListView.separated(
                        padding: const EdgeInsets.only(bottom: 24),
                        itemCount: tasks.length,
                        separatorBuilder: (_, _) => const Divider(height: 1),
                        itemBuilder: (context, index) =>
                            _TaskTile(task: tasks[index], onTap: _openDetail),
                      ),
                    ),
        ),
      ],
      ),
    );
  }
}

class _FilterChip extends StatelessWidget {
  const _FilterChip({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        label: Text(label),
        selected: selected,
        showCheckmark: false,
        onSelected: (_) => onTap(),
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.filter, required this.onCreate});

  final String filter;
  final VoidCallback onCreate;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final filtered = filter != 'all';
    return ListView(
      children: [
        const SizedBox(height: 96),
        Center(
          child: Icon(Icons.support_agent, size: 56, color: scheme.outlineVariant),
        ),
        const SizedBox(height: 12),
        Center(
          child: Text(
            filtered ? '这一类还没有任务' : '还没有 AI 代打任务',
            style: TextStyle(fontSize: 16, color: scheme.onSurfaceVariant),
          ),
        ),
        if (!filtered) ...[
          const SizedBox(height: 8),
          Center(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 40),
              child: Text(
                '写下这通电话要谈成的事，网关自己拨号、自己谈，谈完把纪要交回来。'
                '也可以建成待接入任务，等对方打进来时由 AI 先接。',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 13, color: scheme.onSurfaceVariant),
              ),
            ),
          ),
          const SizedBox(height: 20),
          Center(
            child: FilledButton.icon(
              onPressed: onCreate,
              icon: const Icon(Icons.add_call, size: 18),
              label: const Text('新建 AI 通话'),
            ),
          ),
        ],
      ],
    );
  }
}

class _TaskTile extends StatelessWidget {
  const _TaskTile({required this.task, required this.onTap});

  final Map<String, dynamic> task;
  final void Function(Map<String, dynamic>) onTap;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final status = _text(task, 'status');
    final color = _statusColor(status, scheme);
    final purpose = _text(task, 'purpose');
    final number = _text(task, 'number');
    final inbound = _text(task, 'direction') == 'inbound';
    final achieved = task['achieved'];
    return ListTile(
      onTap: () => onTap(task),
      leading: CircleAvatar(
        radius: 20,
        backgroundColor: color.withValues(alpha: 0.12),
        child: Icon(_statusIcon(status), color: color, size: 20),
      ),
      title: Text(
        purpose.isEmpty ? '（未填写目的）' : purpose,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
      ),
      subtitle: Padding(
        padding: const EdgeInsets.only(top: 4),
        child: Wrap(
          spacing: 6,
          runSpacing: 4,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            Text(
              _statusLabel(status),
              style: TextStyle(color: color, fontWeight: FontWeight.w600),
            ),
            if (number.isNotEmpty) Text(number),
            Text(inbound ? '待接入' : '外呼'),
            if (achieved == true)
              const Text('目的已达成', style: TextStyle(color: Colors.green)),
            if (achieved == false)
              Text('目的未达成', style: TextStyle(color: scheme.error)),
            Text(_when(_asInt(task['created_at']))),
          ],
        ),
      ),
      trailing: const Icon(Icons.chevron_right),
    );
  }
}

/// One task in full: status, the gateway's verdict, the minutes and the words
/// actually spoken. Reloads on open so a stale list entry still shows the
/// current state.
class _TaskDetailSheet extends StatefulWidget {
  const _TaskDetailSheet({required this.taskId});

  final int taskId;

  @override
  State<_TaskDetailSheet> createState() => _TaskDetailSheetState();
}

class _TaskDetailSheetState extends State<_TaskDetailSheet> {
  Map<String, dynamic>? _task;
  List<Map<String, dynamic>> _transcript = const [];
  bool _loading = true;
  String? _error;
  bool _working = false;

  @override
  void initState() {
    super.initState();
    unawaited(_load());
  }

  Future<void> _load() async {
    final scope = context.read<AppScope>();
    try {
      final detail = await scope.api.aiCallTask(widget.taskId);
      final task = detail['task'];
      final flow = detail['flow'];
      final lines = <Map<String, dynamic>>[];
      if (flow is Map && flow['transcript'] is List) {
        for (final entry in flow['transcript'] as List) {
          if (entry is Map) lines.add(Map<String, dynamic>.from(entry));
        }
      }
      if (!mounted) return;
      setState(() {
        _task = task is Map ? Map<String, dynamic>.from(task) : null;
        _transcript = lines;
        _loading = false;
        _error = null;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = e.toString();
      });
    }
    unawaited(scope.aiCalls.refreshTasks());
  }

  Future<void> _cancel() async {
    setState(() => _working = true);
    final ai = context.read<AppScope>().aiCalls;
    final ok = await ai.cancelTask(widget.taskId);
    if (!mounted) return;
    setState(() => _working = false);
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(ok ? '已取消，线路已挂断' : ai.error ?? '取消失败')),
    );
    if (ok) await _load();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.72,
      minChildSize: 0.4,
      maxChildSize: 0.95,
      builder: (context, controller) {
        if (_loading) return const Center(child: CircularProgressIndicator());
        final task = _task;
        if (task == null) return Center(child: Text(_error ?? '任务不存在'));
        final status = _text(task, 'status');
        final color = _statusColor(status, scheme);
        final summary = task['summary'];
        final lines = _transcript;
        return ListView(
          controller: controller,
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 32),
          children: [
            Row(
              children: [
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                  decoration: BoxDecoration(
                    color: color.withValues(alpha: 0.12),
                    borderRadius: BorderRadius.circular(20),
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(_statusIcon(status), size: 14, color: color),
                      const SizedBox(width: 4),
                      Text(
                        _statusLabel(status),
                        style: TextStyle(
                          color: color,
                          fontWeight: FontWeight.w600,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ),
                const Spacer(),
                IconButton(
                  tooltip: '重新读取',
                  icon: const Icon(Icons.refresh, size: 18),
                  onPressed: _loading ? null : _load,
                ),
                if (!_isTerminal(status))
                  OutlinedButton.icon(
                    onPressed: _working ? null : _cancel,
                    icon: const Icon(Icons.stop_circle_outlined, size: 18),
                    label: const Text('取消任务'),
                  ),
              ],
            ),
            const SizedBox(height: 14),
            Text(
              _text(task, 'purpose'),
              style:
                  const TextStyle(fontSize: 17, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 12),
            _InfoGrid(task: task),
            if (_text(task, 'error').isNotEmpty) ...[
              const SizedBox(height: 12),
              _Callout(
                icon: Icons.error_outline,
                color: scheme.error,
                text: _text(task, 'error'),
              ),
            ],
            if (_text(task, 'result').isNotEmpty) ...[
              const SizedBox(height: 14),
              const _SectionTitle('网关的结论'),
              Text(_text(task, 'result'), style: const TextStyle(fontSize: 13)),
            ],
            if (summary is Map) ...[
              const SizedBox(height: 16),
              _Minutes(summary: Map<String, dynamic>.from(summary)),
            ],
            const SizedBox(height: 16),
            _SectionTitle('通话内容（' + lines.length.toString() + ' 句）'),
            if (lines.isEmpty)
              Text(
                '这通电话没有可显示的内容。',
                style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 13),
              )
            else
              for (final line in lines) _TranscriptLine(entry: line),
          ],
        );
      },
    );
  }
}

class _InfoGrid extends StatelessWidget {
  const _InfoGrid({required this.task});

  final Map<String, dynamic> task;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final maxSeconds = _asInt(task['max_seconds']);
    final number = _text(task, 'number');
    final rows = <List<String>>[
      ['号码', number.isEmpty ? '—（等对方打进来）' : number],
      ['方向', _text(task, 'direction') == 'inbound' ? '待接入' : '外呼'],
      ['创建', _when(_asInt(task['created_at']))],
      ['开始', _when(_asInt(task['started_at']))],
      ['结束', _when(_asInt(task['finished_at']))],
      if (maxSeconds != null) ['通话上限', (maxSeconds ~/ 60).toString() + ' 分钟'],
      if (_text(task, 'peer_language').isNotEmpty)
        ['对方语言', _text(task, 'peer_language')],
      if (_text(task, 'created_by').isNotEmpty) ['提交人', _text(task, 'created_by')],
      if (_text(task, 'reference').isNotEmpty)
        ['请求标识', _text(task, 'reference')],
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final row in rows)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 3),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(
                  width: 72,
                  child: Text(
                    row[0],
                    style:
                        TextStyle(color: scheme.onSurfaceVariant, fontSize: 13),
                  ),
                ),
                Expanded(
                  child: Text(row[1], style: const TextStyle(fontSize: 13)),
                ),
              ],
            ),
          ),
        if (_text(task, 'context').isNotEmpty) ...[
          const SizedBox(height: 6),
          Text(
            '背景',
            style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 13),
          ),
          Text(_text(task, 'context'), style: const TextStyle(fontSize: 13)),
        ],
      ],
    );
  }
}

/// The minutes the summary model produced. Anything it could not support stays
/// in 未确认 instead of being promoted into the facts.
class _Minutes extends StatelessWidget {
  const _Minutes({required this.summary});

  final Map<String, dynamic> summary;

  List<String> _strings(String key) {
    final raw = summary[key];
    if (raw is! List) return const [];
    return raw.whereType<Object>().map((e) => e.toString()).toList();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final headline = _text(summary, 'summary');
    final facts = _strings('key_facts');
    final agreements = _strings('agreements');
    final unconfirmed = _strings('unconfirmed');
    final evidence = _text(summary, 'purpose_evidence');
    final achieved = summary['purpose_achieved'];
    final todos = summary['todos'];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const _SectionTitle('通话纪要'),
        if (headline.isNotEmpty)
          Text(headline, style: const TextStyle(fontSize: 13)),
        if (achieved != null) ...[
          const SizedBox(height: 8),
          Row(
            children: [
              Icon(
                achieved == true
                    ? Icons.verified_outlined
                    : Icons.help_outline,
                size: 16,
                color: achieved == true ? Colors.green : scheme.onSurfaceVariant,
              ),
              const SizedBox(width: 6),
              Text(
                achieved == true
                    ? '目的已达成'
                    : achieved == false
                        ? '目的未达成'
                        : '无法判断目的是否达成',
                style: const TextStyle(fontSize: 13),
              ),
            ],
          ),
        ],
        if (evidence.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              '依据：' + evidence,
              style: TextStyle(color: scheme.onSurfaceVariant, fontSize: 12),
            ),
          ),
        if (facts.isNotEmpty) _BulletList(title: '关键事实', items: facts),
        if (agreements.isNotEmpty)
          _BulletList(title: '约定事项', items: agreements),
        if (todos is List && todos.isNotEmpty) ...[
          const SizedBox(height: 8),
          const Text('待办', style: TextStyle(fontWeight: FontWeight.w600)),
          for (final todo in todos) _TodoRow(todo: todo),
        ],
        if (unconfirmed.isNotEmpty)
          _BulletList(title: '未确认', items: unconfirmed, muted: true),
      ],
    );
  }
}

class _BulletList extends StatelessWidget {
  const _BulletList({
    required this.title,
    required this.items,
    this.muted = false,
  });

  final String title;
  final List<String> items;
  final bool muted;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: const TextStyle(fontWeight: FontWeight.w600)),
          for (final item in items)
            Padding(
              padding: const EdgeInsets.only(top: 3),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('· ', style: TextStyle(color: scheme.onSurfaceVariant)),
                  Expanded(
                    child: Text(
                      item,
                      style: TextStyle(
                        fontSize: 13,
                        color: muted ? scheme.onSurfaceVariant : null,
                      ),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

class _TodoRow extends StatelessWidget {
  const _TodoRow({required this.todo});

  final Object? todo;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    var task = '';
    var owner = '';
    var due = '';
    final raw = todo;
    if (raw is Map) {
      final map = Map<String, dynamic>.from(raw);
      task = _text(map, 'task');
      owner = _text(map, 'owner');
      due = _text(map, 'due');
    } else if (raw != null) {
      task = raw.toString();
    }
    final extras = <String>[
      if (owner.isNotEmpty) owner,
      if (due.isNotEmpty) due,
    ];
    return Padding(
      padding: const EdgeInsets.only(top: 3),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.check_box_outline_blank, size: 15, color: scheme.outline),
          const SizedBox(width: 6),
          Expanded(
            child: Text.rich(
              TextSpan(
                text: task,
                children: [
                  if (extras.isNotEmpty)
                    TextSpan(
                      text: '  ' + extras.join(' · '),
                      style: TextStyle(
                        fontSize: 12,
                        color: scheme.onSurfaceVariant,
                      ),
                    ),
                ],
              ),
              style: const TextStyle(fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}

class _TranscriptLine extends StatelessWidget {
  const _TranscriptLine({required this.entry});

  final Map<String, dynamic> entry;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final content = _text(entry, 'content');
    if (content.isEmpty) return const SizedBox.shrink();
    final (label, color, icon) = switch (_text(entry, 'role')) {
      'peer' => ('对方', scheme.primary, Icons.person_outline),
      'agent' => ('AI', scheme.tertiary, Icons.smart_toy_outlined),
      'me' => ('我方', Colors.green.shade700, Icons.mic_none),
      _ => ('系统', scheme.onSurfaceVariant, Icons.info_outline),
    };
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 15, color: color),
          const SizedBox(width: 6),
          SizedBox(
            width: 34,
            child: Text(
              label,
              style: TextStyle(
                fontSize: 12,
                color: color,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          Expanded(child: Text(content, style: const TextStyle(fontSize: 13))),
        ],
      ),
    );
  }
}

class _SectionTitle extends StatelessWidget {
  const _SectionTitle(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 6),
      child: Text(
        text,
        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w700),
      ),
    );
  }
}

class _Callout extends StatelessWidget {
  const _Callout({
    required this.icon,
    required this.color,
    required this.text,
  });

  final IconData icon;
  final Color color;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 16, color: color),
          const SizedBox(width: 8),
          Expanded(
            child: Text(text, style: TextStyle(fontSize: 13, color: color)),
          ),
        ],
      ),
    );
  }
}

/// Collects the purpose and the call details. 外呼 needs a number; 待接入 arms
/// the gateway so it answers the next inbound call with this purpose.
class _CreateTaskDialog extends StatefulWidget {
  const _CreateTaskDialog();

  @override
  State<_CreateTaskDialog> createState() => _CreateTaskDialogState();
}

class _CreateTaskDialogState extends State<_CreateTaskDialog> {
  final _purpose = TextEditingController();
  final _number = TextEditingController();
  final _context = TextEditingController();
  final _peerLanguage = TextEditingController();
  String _direction = 'outbound';
  double _minutes = 5;
  bool _limitEnabled = true;
  bool _submitting = false;
  String? _purposeError;
  String? _numberError;

  /// Same shape the server accepts, so a bad number is caught before the
  /// round trip.
  static final _phoneRe = RegExp(r'^\+?[0-9*#,]{2,24}$');

  @override
  void dispose() {
    _purpose.dispose();
    _number.dispose();
    _context.dispose();
    _peerLanguage.dispose();
    super.dispose();
  }

  bool _validate() {
    final purpose = _purpose.text.trim();
    final number = _number.text.trim();
    String? purposeError;
    String? numberError;
    if (purpose.length < 2) {
      purposeError = '写清楚这通电话要谈成什么';
    } else if (purpose.length > 1000) {
      purposeError = '目的最多 1000 字';
    }
    if (_direction == 'outbound') {
      if (number.isEmpty) {
        numberError = '外呼任务需要填写号码';
      } else if (!_phoneRe.hasMatch(number)) {
        numberError = '号码格式不正确';
      }
    }
    setState(() {
      _purposeError = purposeError;
      _numberError = numberError;
    });
    return purposeError == null && numberError == null;
  }

  void _finish() {
    final background = _context.text.trim();
    final language = _peerLanguage.text.trim();
    Navigator.pop(context, <String, dynamic>{
      'purpose': _purpose.text.trim(),
      'direction': _direction,
      'number': _direction == 'outbound' ? _number.text.trim() : null,
      'context': background.isEmpty ? null : background,
      'peerLanguage': language.isEmpty ? null : language,
      'maxSeconds': _limitEnabled ? (_minutes * 60).round() : null,
    });
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    final inbound = _direction == 'inbound';
    return AlertDialog(
      title: const Text('新建 AI 通话'),
      content: SizedBox(
        width: 460,
        child: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(
                    value: 'outbound',
                    label: Text('外呼'),
                    icon: Icon(Icons.call_made, size: 16),
                  ),
                  ButtonSegment(
                    value: 'inbound',
                    label: Text('待接入'),
                    icon: Icon(Icons.call_received, size: 16),
                  ),
                ],
                selected: {_direction},
                onSelectionChanged: (values) =>
                    setState(() => _direction = values.first),
              ),
              Padding(
                padding: const EdgeInsets.only(top: 6, bottom: 10),
                child: Text(
                  inbound
                      ? '任务先挂着，对方打进来时由 AI 先接，按这个目的谈。'
                      : '网关自己拨出这通电话，谈完把纪要交回来。',
                  style: TextStyle(fontSize: 12, color: scheme.onSurfaceVariant),
                ),
              ),
              TextField(
                controller: _purpose,
                maxLines: 3,
                maxLength: 1000,
                decoration: InputDecoration(
                  labelText: '这通电话要谈成什么',
                  hintText: '例如：跟房东确认下月租金是否维持 3200，并约好维修时间',
                  errorText: _purposeError,
                  border: const OutlineInputBorder(),
                ),
              ),
              if (!inbound)
                TextField(
                  controller: _number,
                  keyboardType: TextInputType.phone,
                  decoration: InputDecoration(
                    labelText: '对方号码',
                    errorText: _numberError,
                    border: const OutlineInputBorder(),
                  ),
                ),
              const SizedBox(height: 12),
              TextField(
                controller: _context,
                maxLines: 3,
                maxLength: 4000,
                decoration: const InputDecoration(
                  labelText: '背景资料（可选）',
                  hintText: '订单号、之前谈定的内容、可以让步的底线',
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
              const SizedBox(height: 8),
              Row(
                children: [
                  Switch(
                    value: _limitEnabled,
                    onChanged: (value) => setState(() => _limitEnabled = value),
                  ),
                  Expanded(
                    child: Text(
                      _limitEnabled
                          ? '通话上限 ' + _minutes.round().toString() + ' 分钟'
                          : '不额外限制通话时长（用网关默认上限）',
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
          onPressed: _submitting ? null : () => Navigator.pop(context),
          child: const Text('取消'),
        ),
        FilledButton.icon(
          onPressed: _submitting ? null : () {
            if (!_validate()) return;
            setState(() => _submitting = true);
            _finish();
          },
          icon: const Icon(Icons.add_call, size: 18),
          label: Text(inbound ? '创建待接入' : '创建并拨号'),
        ),
      ],
    );
  }
}

/// ChangeNotifier controllers, one per feature area.
///
/// Each controller owns its own loading and error state so a slow endpoint
/// cannot blank out the rest of the UI. They are created once by AppScope and
/// shared through provider.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../data/api_client.dart';
import '../data/models.dart';
import 'call_session_registry.dart';
import '../data/realtime_client.dart';

/// Base class holding the busy/error flags every controller needs.
abstract class BaseController extends ChangeNotifier {
  BaseController(this.api);

  @protected
  final ApiClient api;

  bool _busy = false;
  String? _error;

  bool get busy => _busy;
  String? get error => _error;

  void clearError() {
    if (_error == null) return;
    _error = null;
    notifyListeners();
  }

  @protected
  Future<T?> guard<T>(Future<T> Function() body, {bool setBusy = true}) async {
    if (setBusy) {
      _busy = true;
      _error = null;
      notifyListeners();
    }
    try {
      final result = await body();
      _error = null;
      return result;
    } on ApiException catch (e) {
      _error = e.message;
      return null;
    } catch (e) {
      _error = e.toString();
      return null;
    } finally {
      if (setBusy) {
        _busy = false;
        notifyListeners();
      }
    }
  }

  @protected
  void fail(Object e) {
    _error = e is ApiException ? e.message : e.toString();
    notifyListeners();
  }
}

// --------------------------------------------------------------------------
// Devices
// --------------------------------------------------------------------------

class DevicesController extends BaseController {
  DevicesController(super.api);

  List<DeviceInfo> _items = [];
  int? _preferredId;

  List<DeviceInfo> get items => _items;
  int? get preferredId => _preferredId;

  List<DeviceInfo> get online => _items.where((d) => d.isOnline).toList();

  DeviceInfo? byId(int? id) {
    if (id == null) return null;
    for (final d in _items) {
      if (d.id == id) return d;
    }
    return null;
  }

  /// The device used when the user has not picked one explicitly.
  DeviceInfo? get effective {
    final picked = byId(_preferredId);
    if (picked != null) return picked;
    return online.isNotEmpty
        ? online.first
        : (_items.isNotEmpty ? _items.first : null);
  }

  String labelFor(int? id) =>
      byId(id)?.displayName ?? '设备' + (id ?? 0).toString();

  void setPreferred(int? id) {
    _preferredId = id;
    notifyListeners();
  }

  Future<void> refresh() => guard(() async {
    _items = await api.devices();
    if (_preferredId != null && byId(_preferredId) == null) _preferredId = null;
    notifyListeners();
    return null;
  });

  Future<void> rescan() => guard(() async {
    await api.post('/app/devices/scan');
    _items = await api.devices();
    notifyListeners();
    return null;
  });
}

// --------------------------------------------------------------------------
// Calls
// --------------------------------------------------------------------------

class CallsController extends BaseController {
  CallsController(super.api);

  final CallSessionRegistry registry = CallSessionRegistry();
  List<CallSession> _active = [];
  List<CallRecord> _history = [];
  bool _voiceEnabled = true;
  int _maxConcurrent = 4;

  /// Set while a prompt such as "正在接通，请稍候" should be shown.
  String? _notice;
  String? get notice => _notice;

  List<CallSession> get active => _active;
  List<CallRecord> get history => _history;
  bool get voiceEnabled => _voiceEnabled;
  int get maxConcurrent => _maxConcurrent;
  bool get hasActive => _active.isNotEmpty;

  CallSession? forDevice(int? deviceId) {
    if (deviceId == null) return null;
    for (final c in _active) {
      if (c.deviceId == deviceId) return c;
    }
    return null;
  }

  /// The call the in-call screen should show: newest live call.
  CallSession? get foreground {
    for (final c in _active.reversed) {
      if (!c.isEnded) return c;
    }
    return _active.isNotEmpty ? _active.last : null;
  }

  void setNotice(String? text) {
    if (_notice == text) return;
    _notice = text;
    notifyListeners();
  }

  Future<void> refresh() => guard(() async {
    final state = await api.voiceState();
    _voiceEnabled = state['voice_enabled'] != false;
    _maxConcurrent = (state['max_concurrent'] as num?)?.toInt() ?? 4;
    final sessions = api.parseActive(state['active']);
    for (final s in sessions) {
      registry.update(s);
    }
    _active = sessions;
    notifyListeners();
    return null;
  });

  Future<void> refreshHistory({int limit = 50}) => guard(() async {
    _history = await api.callHistory(limit: limit);
    notifyListeners();
    return null;
  }, setBusy: false);

  void unavailableDevice(String? label) =>
      fail(label == null ? '没有可用设备' : '$label 已离线，请切换在线设备');

  Future<CallSession?> dial({required String number, int? deviceId}) =>
      guard(() async {
        final session = await api.dial(number: number, deviceId: deviceId);
        _upsert(session);
        notifyListeners();
        return session;
      });

  Future<bool> answer(int deviceId) async {
    final ok = await guard(() => api.answer(deviceId));
    await refresh();
    final live = forDevice(deviceId);
    if (live != null && live.state == CallState.active && live.answeredAt == null) {
      _upsert(live.copyWith(
        state: CallState.active,
        answeredAt: DateTime.now().millisecondsSinceEpoch ~/ 1000,
      ));
      notifyListeners();
    }
    return ok != null;
  }

  Future<bool> hangup(int deviceId) async {
    final ok = await guard(() => api.hangup(deviceId));
    await refresh();
    return ok != null;
  }

  Future<bool> reject(int deviceId) => hangup(deviceId);

  Future<bool> sendDtmf(int deviceId, String digits) async {
    final ok = await guard(() => api.dtmf(deviceId, digits), setBusy: false);
    return ok != null;
  }

  Future<bool> speak(int deviceId, String text, {int? mode}) async {
    final ok = await guard(
      () => api.speak(deviceId, text, mode: mode),
      setBusy: false,
    );
    return ok != null;
  }

  Future<bool> stopSpeak(int deviceId) async {
    final ok = await guard(() => api.stopSpeak(deviceId), setBusy: false);
    return ok != null;
  }

  void _upsert(CallSession session) {
    registry.update(session);
    final idx = _active.indexWhere((c) => c.callId == session.callId);
    if (idx >= 0) {
      _active[idx] = session;
    } else {
      _active.add(session);
    }
  }

  /// Apply a realtime call.* event.
  void applyEvent(RealtimeEvent event) {
    switch (event.event) {
      case 'call.ring':
      case 'call.incoming':
      case 'call.state':
        final payload = event.data['session'] ?? event.data;
        if (payload is Map) {
          _upsert(CallSession.fromJson(Map<String, dynamic>.from(payload)));
          notifyListeners();
        }
        break;
      case 'call.ended':
        final payload = event.data;
        final session = CallSession.fromJson(
          Map<String, dynamic>.from(payload),
        );
        _upsert(session);
        // Drop finished calls so the UI returns to the dialer.
        _active.removeWhere((c) => c.callId == session.callId);
        _notice = null;
        notifyListeners();
        unawaited(refreshHistory());
        break;
      case 'call.prompt':
        final prompt = event.data['prompt']?.toString();
        final state = event.data['state']?.toString();
        _notice = describePrompt(prompt, state);
        notifyListeners();
        break;
      case 'call.recording':
        unawaited(refreshHistory());
        break;
      case 'call.tts':
        notifyListeners();
        break;
    }
  }

  static String? describePrompt(String? promptId, String? state) {
    if (promptId == null || promptId.isEmpty) return null;
    switch (promptId) {
      case 'dial_connecting':
        return '正在接通，请稍候';
      case 'ringback':
        return '请稍候，对方正在接听';
      case 'incoming_call':
        return '您有来电，请及时接听';
      case 'call_busy':
        return '您拨打的电话正忙，请稍后再拨';
      case 'call_no_answer':
        return '您拨打的电话暂时无人接听，请稍后再拨';
      case 'call_invalid_number':
        return '您拨打的号码是空号，请核对后再拨';
      case 'call_ended':
        return '通话已结束';
      default:
        return state;
    }
  }
}

// --------------------------------------------------------------------------
// Messages
// --------------------------------------------------------------------------

class MessagesController extends BaseController {
  MessagesController(super.api);

  List<Conversation> _conversations = [];
  int _unreadTotal = 0;
  String? _activePeer;
  String? _activeKey;
  String? _query;
  int _listRequest = 0;
  final Map<String, List<SmsMessage>> _threads = {};

  List<Conversation> get conversations => _conversations;
  int get unreadTotal => _unreadTotal;

  List<SmsMessage> thread(String peerKey) => _threads[peerKey] ?? const [];

  Future<void> refreshConversations({String? q, bool keepQuery = false}) =>
      guard(() async {
        if (!keepQuery) _query = q;
        final request = ++_listRequest;
        final data = await api.get(
          '/app/conversations',
          query: {'limit': 500, if (_query != null) 'q': _query},
        );
        if (request != _listRequest) return null;
        _conversations = (data['items'] as List? ?? [])
            .map(
              (e) => Conversation.fromJson(Map<String, dynamic>.from(e as Map)),
            )
            .toList();
        _unreadTotal =
            (data['unread_total'] as num?)?.toInt() ??
            _conversations.fold<int>(0, (sum, c) => sum + c.unread);
        notifyListeners();
        return null;
      });

  Future<void> openThread(String peer, {String? peerKey}) async {
    _activePeer = peer;
    _activeKey = peerKey ?? peer;
    await _reloadThread(peer, _activeKey!, markRead: true);
    await refreshConversations(keepQuery: true);
  }

  Future<void> _reloadThread(
    String peer,
    String key, {
    bool markRead = false,
  }) => guard(() async {
    _threads[key] = await api.thread(peer, markRead: markRead);
    notifyListeners();
    return null;
  }, setBusy: false);

  void closeThread() {
    _activePeer = null;
    _activeKey = null;
  }

  Future<bool> markRead(String peer, {required bool read}) async {
    final result = await guard(() => api.markConversation(peer, read: read));
    if (result == null) return false;
    await refreshConversations(keepQuery: true);
    return true;
  }

  Future<bool> deleteThreads(
    List<String> peers, {
    bool readOnly = false,
  }) async {
    final result = await guard(
      () => readOnly
          ? api.deleteReadConversations()
          : api.deleteConversations(peers),
    );
    if (result == null) return false;
    _threads.clear();
    await refreshConversations(keepQuery: true);
    return true;
  }

  Future<bool> send({
    required String to,
    required String content,
    int? deviceId,
  }) async {
    final result = await guard(
      () => api.sendSms(to: to, content: content, deviceId: deviceId),
      setBusy: false,
    );
    if (result != null) {
      await refreshConversations();
      return true;
    }
    return false;
  }

  Future<void> saveDraft(String peer, String? draft) async {
    try {
      await api.saveDraft(peer, draft);
    } catch (_) {
      // Drafts are best effort; never surface a failure for them.
    }
  }

  void applyEvent(RealtimeEvent event) {
    if (!event.isSms) return;
    unawaited(refreshConversations(keepQuery: true));
    final peer = _activePeer;
    final key = _activeKey;
    if (peer == null || key == null) return;
    // Only the visible thread may become read. Cached/closed threads must
    // never consume a later unread marker during a websocket refresh.
    final isArrival =
        event.event == 'sms.received' || event.event == 'sms.sent';
    unawaited(_reloadThread(peer, key, markRead: isArrival));
  }

  Future<Map<String, dynamic>> search(String q) async {
    final result = await guard(() => api.search(q));
    return result ?? <String, dynamic>{'messages': [], 'contacts': []};
  }
}

// --------------------------------------------------------------------------
// Contacts
// --------------------------------------------------------------------------

class ContactsController extends BaseController {
  ContactsController(super.api);

  List<Contact> _items = [];
  List<String> _groups = [];

  List<Contact> get items => _items;
  List<String> get groups => _groups;
  List<Contact> get favorites => _items.where((c) => c.favorite).toList();

  String? nameFor(String number) {
    final needle = _digits(number);
    if (needle.isEmpty) return null;
    for (final c in _items) {
      final d = _digits(c.number);
      if (d.isNotEmpty &&
          (d == needle || d.endsWith(needle) || needle.endsWith(d))) {
        return c.name;
      }
    }
    return null;
  }

  static String _digits(String s) => s.replaceAll(RegExp(r'[^0-9]'), '');

  Future<void> refresh({String? q}) => guard(() async {
    final page = await api.contacts(q: q);
    _items = page.items;
    _groups = page.groups;
    notifyListeners();
    return null;
  });

  Future<bool> save(Contact contact) async {
    final ok = await guard(() async {
      if (contact.id == 0) {
        await api.createContact(contact);
      } else {
        await api.updateContact(contact.id, contact);
      }
      return true;
    });
    if (ok == true) await refresh();
    return ok == true;
  }

  Future<bool> remove(int id) async {
    final ok = await guard(() async {
      await api.deleteContact(id);
      return true;
    });
    if (ok == true) await refresh();
    return ok == true;
  }
}

// --------------------------------------------------------------------------
// Audio
// --------------------------------------------------------------------------

class AudioController extends BaseController {
  AudioController(super.api);

  AudioState? _state;
  List<PromptTone> _prompts = [];
  String? _playingPrompt;

  AudioState? get state => _state;
  List<PromptTone> get prompts => _prompts;
  String? get playingPrompt => _playingPrompt;

  Future<void> refresh() => guard(() async {
    _state = await api.audioDevices();
    _prompts = await api.prompts();
    notifyListeners();
    return null;
  });

  Future<bool> setOutput(String device) async {
    final next = await guard(() => api.setOutput(device));
    if (next != null) {
      _state = next;
      notifyListeners();
      return true;
    }
    return false;
  }

  Future<bool> setInput(String device) async {
    final next = await guard(() => api.setInput(device));
    if (next != null) {
      _state = next;
      notifyListeners();
      return true;
    }
    return false;
  }

  Future<bool> setVolume(double volume) async {
    final next = await guard(() => api.setVolume(volume), setBusy: false);
    if (next != null) {
      _state = next;
      notifyListeners();
      return true;
    }
    return false;
  }

  Future<bool> playPrompt(String promptId) async {
    _playingPrompt = promptId;
    notifyListeners();
    final ok = await guard(() => api.playPrompt(promptId), setBusy: false);
    _playingPrompt = null;
    notifyListeners();
    return ok != null;
  }
}

// --------------------------------------------------------------------------
// AI
// --------------------------------------------------------------------------

class AiController extends BaseController {
  AiController(super.api);

  Map<String, dynamic> _status = const {};
  final List<ChatTurn> _history = [];
  bool _sending = false;

  Map<String, dynamic> get status => _status;
  List<ChatTurn> get history => List.unmodifiable(_history);
  bool get sending => _sending;

  String get provider => (_status['provider'] ?? 'none').toString();
  bool get loaded => _status['loaded'] == true;
  String get ttsEngine => (_status['tts_engine'] ?? 'module').toString();

  Future<void> refresh() => guard(() async {
    _status = await api.aiStatus();
    notifyListeners();
    return null;
  });

  void clearHistory() {
    _history.clear();
    notifyListeners();
  }

  Future<String?> send(String message) async {
    if (message.trim().isEmpty) return null;
    _sending = true;
    _history.add(ChatTurn(role: 'user', content: message));
    notifyListeners();
    final reply = await guard(
      () => api.chat(message: message, history: _history),
      setBusy: false,
    );
    _sending = false;
    if (reply == null) {
      _history.removeLast();
      notifyListeners();
      return null;
    }
    _history.add(ChatTurn(role: 'assistant', content: reply.reply));
    notifyListeners();
    return reply.reply;
  }
}

// --------------------------------------------------------------------------
// AI calls: live translation, the call agent and AI call tasks
// --------------------------------------------------------------------------

/// Everything the gateway does on top of a live call, plus the AI call queue.
///
/// The server pushes transcript lines and translations over the socket, so the
/// in-call subtitles update without polling; the task list refreshes when a
/// task event arrives.
class AiCallsController extends BaseController {
  AiCallsController(super.api);

  Map<String, dynamic> _state = const {};
  final Map<String, Map<String, dynamic>> _flows = {};
  final Map<String, List<Map<String, dynamic>>> _transcripts = {};
  final Map<String, List<Map<String, dynamic>>> _subtitles = {};
  List<Map<String, dynamic>> _tasks = const [];
  bool _tasksLoaded = false;

  Map<String, dynamic> get state => _state;
  List<Map<String, dynamic>> get tasks => _tasks;
  bool get tasksLoaded => _tasksLoaded;

  Map<String, dynamic> flowFor(String callId) => _flows[callId] ?? const {};

  List<Map<String, dynamic>> transcriptFor(String callId) =>
      _transcripts[callId] ?? const [];

  /// Subtitle lines for the in-call screen, in arrival order.
  List<Map<String, dynamic>> subtitlesFor(String callId) =>
      _subtitles[callId] ?? const [];

  String translationModeFor(String callId) {
    final translation = flowFor(callId)['translation'];
    if (translation is Map) return (translation['mode'] ?? 'off').toString();
    return 'off';
  }

  String? translationNoteFor(String callId) {
    final note = flowFor(callId)['translation_note'];
    return note is String && note.isNotEmpty ? note : null;
  }

  bool agentRunningFor(String callId) {
    final agent = flowFor(callId)['agent'];
    if (agent is! Map) return false;
    return agent['finished'] != true && agent['pending'] != true;
  }

  bool handoverFor(String callId) => flowFor(callId)['handover'] == true;

  Map<String, dynamic>? summaryFor(String callId) {
    final summary = flowFor(callId)['summary'];
    return summary is Map ? Map<String, dynamic>.from(summary) : null;
  }

  Future<void> refreshState() => guard(() async {
    _state = await api.flowState();
    notifyListeners();
    return null;
  });

  Future<void> refreshFlow(String callId) => guard(() async {
    try {
      _flows[callId] = await api.callFlow(callId);
    } on ApiException catch (e) {
      // A call the server no longer tracks is not an error worth showing.
      if (e.statusCode != 404) rethrow;
    }
    notifyListeners();
    return null;
  }, setBusy: false);

  Future<void> refreshTranscript(String callId) => guard(() async {
    _transcripts[callId] = await api.callTranscript(callId);
    notifyListeners();
    return null;
  }, setBusy: false);

  Future<void> refreshTasks({String? status}) => guard(() async {
    _tasks = await api.aiCallTasks(status: status);
    _tasksLoaded = true;
    notifyListeners();
    return null;
  });

  Future<Map<String, dynamic>?> createTask({
    required String purpose,
    String? number,
    String direction = 'outbound',
    String? context,
    String? peerLanguage,
    int? maxSeconds,
  }) async {
    final result = await guard(() => api.createAiCallTask(
      purpose: purpose,
      number: number,
      direction: direction,
      context: context,
      peerLanguage: peerLanguage,
      maxSeconds: maxSeconds,
    ));
    if (result != null) unawaited(refreshTasks());
    return result;
  }

  Future<bool> cancelTask(int id) async {
    final result = await guard(() => api.cancelAiCallTask(id));
    if (result != null) unawaited(refreshTasks());
    return result != null;
  }

  Future<bool> setTranslation(
    String callId, {
    String? mode,
    String? cadence,
    String? myLanguage,
    String? peerLanguage,
  }) async {
    final result = await guard(() => api.setTranslation(
      callId,
      mode: mode,
      cadence: cadence,
      myLanguage: myLanguage,
      peerLanguage: peerLanguage,
    ), setBusy: false);
    if (result == null) return false;
    _adoptFlow(callId, result);
    return true;
  }

  Future<bool> say(String callId, String text) async {
    final result = await guard(() => api.sayToCall(callId, text), setBusy: false);
    if (result == null) return false;
    final spoken = result['spoken'];
    if (spoken is String && spoken.isNotEmpty) {
      final subs = _subtitles.putIfAbsent(callId, () => []);
      subs.add({
        'kind': 'line',
        'entry': {'role': 'me', 'content': text, 'spoken': spoken},
      });
      notifyListeners();
    }
    return true;
  }

  Future<bool> attachAgent(
    String callId, {
    required String purpose,
    String? context,
    String? peerLanguage,
    int? maxSeconds,
  }) async {
    final result = await guard(() => api.attachAgent(
      callId,
      purpose: purpose,
      context: context,
      peerLanguage: peerLanguage,
      maxSeconds: maxSeconds,
    ));
    if (result == null) return false;
    _adoptFlow(callId, result);
    return true;
  }

  Future<bool> handover(String callId) async {
    final result = await guard(() => api.agentHandover(callId));
    if (result == null) return false;
    _adoptFlow(callId, result);
    return true;
  }

  Future<Map<String, dynamic>?> summary(String callId) =>
      guard(() => api.callSummary(callId));

  void _adoptFlow(String callId, Map<String, dynamic> result) {
    final flow = result['flow'];
    if (flow is Map) {
      _flows[callId] = Map<String, dynamic>.from(flow);
    }
    notifyListeners();
  }

  void applyEvent(RealtimeEvent event) {
    final callId = event.data['call_id']?.toString();
    switch (event.event) {
      case 'call.transcript':
        if (callId == null) return;
        final entry = event.data['entry'];
        if (entry is! Map) return;
        final line = Map<String, dynamic>.from(entry);
        _transcripts.putIfAbsent(callId, () => []).add(line);
        _subtitles.putIfAbsent(callId, () => []).add({'kind': 'line', 'entry': line});
        notifyListeners();
      case 'call.translation':
        if (callId == null) return;
        _subtitles
            .putIfAbsent(callId, () => [])
            .add({'kind': 'translation', 'entry': Map<String, dynamic>.from(event.data)});
        final history = _flows[callId]?['translation_history'];
        if (history is List) history.add(Map<String, dynamic>.from(event.data));
        notifyListeners();
      case 'call.flow':
      case 'call.flow.ended':
      case 'call.flow.handover':
      case 'call.flow.capture_failed':
        if (callId != null && event.data.isNotEmpty) {
          _flows[callId] = Map<String, dynamic>.from(event.data);
        }
        notifyListeners();
      case 'call.summary':
        if (callId != null) {
          _flows.putIfAbsent(callId, () => {})['summary'] = event.data['summary'];
        }
        notifyListeners();
      case 'ai_call.task_queued':
      case 'ai_call.task_started':
      case 'ai_call.task_finished':
      case 'ai_call.task_cancelled':
        unawaited(refreshTasks());
    }
  }
}

// --------------------------------------------------------------------------
// Gateway settings (admin)
// --------------------------------------------------------------------------

class SettingsController extends BaseController {
  SettingsController(super.api);

  Map<String, dynamic> _values = {};
  List<String> _editable = [];
  Map<String, dynamic> _stats = {};

  Map<String, dynamic> get values => _values;
  List<String> get editable => _editable;
  Map<String, dynamic> get stats => _stats;

  T? value<T>(String key, T fallback) {
    final v = _values[key];
    if (v == null) return fallback;
    if (v is T) return v;
    if (fallback is double && v is num) return v.toDouble() as T;
    if (fallback is int && v is num) return v.toInt() as T;
    if (fallback is bool) return (v.toString() == 'true') as T;
    return v.toString() as T;
  }

  Future<void> refresh() => guard(() async {
    final data = await api.settings();
    _values = Map<String, dynamic>.from(data['settings'] as Map? ?? {});
    _editable = ((data['editable'] as List?) ?? const [])
        .map((e) => e.toString())
        .toList();
    notifyListeners();
    return null;
  });

  Future<void> refreshStats() => guard(() async {
    _stats = await api.stats();
    notifyListeners();
    return null;
  }, setBusy: false);

  Future<bool> save(Map<String, dynamic> patch) async {
    final data = await guard(() => api.saveSettings(patch));
    if (data == null) return false;
    // PUT returns {"ok":true,"applied":{...}}. Merge what the server accepted,
    // then re-read so secret fields (never echoed back) stay truthful.
    final applied = data['applied'];
    if (applied is Map) {
      applied.forEach((k, v) {
        if (v == 'set' || v == 'cleared') return;
        _values[k.toString()] = v;
      });
    }
    await refresh();
    notifyListeners();
    return true;
  }
}

// --------------------------------------------------------------------------
// Users (admin)
// --------------------------------------------------------------------------

class UsersController extends BaseController {
  UsersController(super.api);

  List<Map<String, dynamic>> _items = [];
  List<Map<String, dynamic>> get items => _items;

  Future<void> refresh() => guard(() async {
    _items = await api.users();
    notifyListeners();
    return null;
  });

  Future<bool> create({
    required String username,
    required String password,
    String role = 'operator',
    String? displayName,
  }) async {
    final ok = await guard(
      () => api.createUser(
        username: username,
        password: password,
        role: role,
        displayName: displayName,
      ),
    );
    if (ok != null) await refresh();
    return ok != null;
  }

  Future<bool> update(
    int id, {
    String? role,
    bool? isActive,
    String? displayName,
  }) async {
    final ok = await guard(
      () => api.updateUser(
        id,
        role: role,
        isActive: isActive,
        displayName: displayName,
      ),
    );
    if (ok != null) await refresh();
    return ok != null;
  }

  Future<bool> resetPassword(int id, String password) async {
    final ok = await guard(() => api.resetPassword(id, password));
    return ok != null;
  }

  Future<bool> remove(int id) async {
    final ok = await guard(() => api.deleteUser(id));
    if (ok != null) await refresh();
    return ok != null;
  }
}

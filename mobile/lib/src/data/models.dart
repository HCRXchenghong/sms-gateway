/// JSON models mirroring the VoiceSMS Gateway HTTP API.
///
/// Field names match the server payload exactly (snake_case) so the client can
/// be diffed against /openapi.json without a translation layer.
library;

int? _asInt(Object? v) {
  if (v == null) return null;
  if (v is int) return v;
  if (v is num) return v.toInt();
  return int.tryParse(v.toString());
}

double _asDouble(Object? v, [double fallback = 0]) {
  if (v == null) return fallback;
  if (v is double) return v;
  if (v is num) return v.toDouble();
  return double.tryParse(v.toString()) ?? fallback;
}

String? _asStr(Object? v) {
  if (v == null) return null;
  final s = v.toString();
  return s.isEmpty ? null : s;
}

bool _asBool(Object? v, [bool fallback = false]) {
  if (v == null) return fallback;
  if (v is bool) return v;
  if (v is num) return v != 0;
  final s = v.toString().toLowerCase();
  return s == 'true' || s == '1' || s == 'yes';
}

/// Like [_asBool] but keeps "absent" distinct from false. Recording state
/// reaches the app in three payload shapes, and a key one of them omits must
/// not overwrite a value an earlier response already reported.
bool? _boolOrNull(Object? v) {
  if (v == null) return null;
  if (v is bool) return v;
  if (v is num) return v != 0;
  final s = v.toString().trim().toLowerCase();
  if (s == 'true' || s == '1' || s == 'yes') return true;
  if (s == 'false' || s == '0' || s == 'no') return false;
  return null;
}

/// One A7670X module as reported by /app/devices.
class DeviceInfo {
  DeviceInfo({
    required this.id,
    required this.code,
    required this.displayName,
    required this.status,
    this.simNumber,
    this.iccid,
    this.imsi,
    this.imei,
    this.model,
    this.manufacturer,
    this.atPort,
    this.lastError,
    this.lastSeenAt,
    this.signal,
  });

  final int id;
  final String code;
  final String displayName;
  final String status;
  final String? simNumber;
  final String? iccid;
  final String? imsi;
  final String? imei;
  final String? model;
  final String? manufacturer;
  final String? atPort;
  final String? lastError;
  final int? lastSeenAt;
  final int? signal;

  bool get isOnline => status == 'online';

  factory DeviceInfo.fromJson(Map<String, dynamic> json) {
    final sig = json['signal'];
    return DeviceInfo(
      id: _asInt(json['id']) ?? 0,
      code: _asStr(json['code']) ?? '',
      displayName: _asStr(json['display_name']) ?? _asStr(json['label']) ?? '设备',
      status: _asStr(json['status']) ?? 'unknown',
      simNumber: _asStr(json['sim_number']),
      iccid: _asStr(json['iccid']),
      imsi: _asStr(json['imsi']),
      imei: _asStr(json['imei']),
      model: _asStr(json['model']),
      manufacturer: _asStr(json['manufacturer']),
      atPort: _asStr(json['at_port']),
      lastError: _asStr(json['last_error']),
      lastSeenAt: _asInt(json['last_seen_at']),
      signal: sig is Map ? _asInt(sig['csq']) : _asInt(json['csq']),
    );
  }
}

/// Call state values emitted by app/voice/state.py.
class CallState {
  static const idle = 'idle';
  static const dialing = 'dialing';
  static const alerting = 'alerting';
  static const incoming = 'incoming';
  static const active = 'active';
  static const held = 'held';
  static const ended = 'ended';

  static bool isLive(String s) =>
      s == dialing || s == alerting || s == incoming || s == active || s == held;
}

/// A live or recently ended call, from /app/voice/state and the call.* WS events.
class CallSession {
  CallSession({
    required this.callId,
    required this.deviceId,
    required this.direction,
    required this.state,
    this.remote,
    this.createdAt,
    this.ringingAt,
    this.answeredAt,
    this.endedAt,
    this.endReason,
    this.duration = 0,
    this.ttsLog = const [],
    this.origin,
  });

  final String callId;
  final int deviceId;
  final String direction;
  final String state;
  final String? remote;
  final int? createdAt;
  final int? ringingAt;
  final int? answeredAt;
  final int? endedAt;
  final String? endReason;
  final int duration;
  final List<dynamic> ttsLog;
  final String? origin;

  bool get isInbound => direction == 'inbound';
  bool get isEnded => state == CallState.ended;

  CallSession copyWith({String? state, String? endReason, int? answeredAt, int? endedAt}) {
    return CallSession(
      callId: callId,
      deviceId: deviceId,
      direction: direction,
      state: state ?? this.state,
      remote: remote,
      createdAt: createdAt,
      ringingAt: ringingAt,
      answeredAt: answeredAt ?? this.answeredAt,
      endedAt: endedAt ?? this.endedAt,
      endReason: endReason ?? this.endReason,
      duration: duration,
      ttsLog: ttsLog,
      origin: origin,
    );
  }

  factory CallSession.fromJson(Map<String, dynamic> json) => CallSession(
        callId: _asStr(json['call_id']) ?? '',
        deviceId: _asInt(json['device_id']) ?? 0,
        direction: _asStr(json['direction']) ?? 'outbound',
        state: _asStr(json['state']) ?? CallState.idle,
        remote: _asStr(json['remote']),
        createdAt: _asInt(json['created_at']),
        ringingAt: _asInt(json['ringing_at']),
        answeredAt: _asInt(json['answered_at']),
        endedAt: _asInt(json['ended_at']),
        endReason: _asStr(json['end_reason']),
        duration: _asInt(json['duration']) ?? 0,
        ttsLog: (json['tts_log'] as List?) ?? const [],
        origin: _asStr(json['origin']),
      );
}

/// One row of the call log, from /app/voice/history.
class CallRecord extends CallSession {
  CallRecord({
    required super.callId,
    required super.deviceId,
    required super.direction,
    required super.state,
    super.remote,
    super.createdAt,
    super.answeredAt,
    super.endedAt,
    super.endReason,
    super.duration,
    this.deviceName,
    this.deviceCode,
    this.simNumber,
    this.recordingPath,
    this.recordingReason,
    this.recordingNotice,
    this.recordingNoticeOk,
  });

  final String? deviceName;
  final String? deviceCode;
  final String? simNumber;
  final String? recordingPath;
  final String? recordingReason;

  /// The sentence the far end heard, when they were told the call is recorded.
  final String? recordingNotice;
  final bool? recordingNoticeOk;

  bool get hasRecording => recordingPath != null && recordingPath!.isNotEmpty;

  /// Recording outcome as one line for the call log.
  String get recordingLabel {
    if (!hasRecording) {
      return recordingReason == 'policy_disabled' ? '策略关闭' : '未录音';
    }
    if (recordingNotice == null) return '已录音 · 静默';
    return recordingNoticeOk == true ? '已录音 · 已告知对方' : '已录音 · 提示未成功播放';
  }

  factory CallRecord.fromJson(Map<String, dynamic> json) {
    final dev = json['device'];
    return CallRecord(
      callId: _asStr(json['call_id']) ?? '',
      deviceId: _asInt(json['device_id']) ?? 0,
      direction: _asStr(json['direction']) ?? 'outbound',
      state: _asStr(json['state']) ?? CallState.ended,
      remote: _asStr(json['remote']),
      createdAt: _asInt(json['created_at']),
      answeredAt: _asInt(json['answered_at']),
      endedAt: _asInt(json['ended_at']),
      endReason: _asStr(json['end_reason']),
      duration: _asInt(json['duration']) ?? 0,
      deviceName: dev is Map ? _asStr(dev['display_name']) : null,
      deviceCode: dev is Map ? _asStr(dev['code']) : null,
      simNumber: _asStr(json['sim_number']),
      recordingPath: _asStr(json['recording_path']),
      recordingReason: _asStr(json['recording_reason']),
      recordingNotice: _asStr(json['recording_notice']),
      recordingNoticeOk: _boolOrNull(json['recording_notice_ok']),
    );
  }
}

/// Whether one call is being recorded, and whether the operator may change it.
///
/// Three server shapes land here: the flat capture dict published on the
/// call.recording websocket event, the same dict nested under "capture" by
/// /app/voice/calls/{id}/recording/state, and the per-call "recording" flag on
/// /app/voice/state. [fallback] keeps the fields a given shape does not carry,
/// so a websocket frame cannot erase the policy the status call reported.
class CallRecording {
  CallRecording({
    required this.callId,
    this.recording = false,
    this.canChange = true,
    this.policy = 'optional',
    this.reason = '',
    this.announce = false,
    this.noticeText,
    this.noticeOk,
    this.noticeError,
    this.failedReason,
    this.path,
  });

  final String callId;

  /// A capture process is running right now.
  final bool recording;
  final bool canChange;
  final String policy;
  final String reason;
  final bool announce;
  final String? noticeText;
  final bool? noticeOk;
  final String? noticeError;
  final String? failedReason;
  final String? path;

  bool get forced => policy == 'forced';
  bool get blocked => policy == 'disabled';
  bool get announced => noticeOk == true;

  String get policyLabel => switch (policy) {
        'forced' => '强制录音',
        'disabled' => '不录音',
        _ => '可选择录音',
      };

  String get reasonLabel => switch (reason) {
        'policy_forced' => '后台策略要求全程录音',
        'policy_disabled' => '后台策略已关闭录音',
        'policy_default' => '按后台默认设置',
        'operator_enabled' => '手动开启',
        'operator_disabled' => '手动关闭',
        _ => reason,
      };

  String get failedLabel => switch (failedReason ?? '') {
        'audio_capture_disabled' => '服务器声卡采集已关闭',
        'no_input_device' => '服务器没有可用的录音输入设备',
        'empty_file' => '录音文件为空，已丢弃',
        'record_dir_unwritable' => '服务器录音目录不可写',
        '' => '',
        _ => failedReason!,
      };

  CallRecording copyWith({bool? recording, String? path}) => CallRecording(
        callId: callId,
        recording: recording ?? this.recording,
        canChange: canChange,
        policy: policy,
        reason: reason,
        announce: announce,
        noticeText: noticeText,
        noticeOk: noticeOk,
        noticeError: noticeError,
        failedReason: failedReason,
        path: path ?? this.path,
      );

  factory CallRecording.fromJson(
    String callId,
    Map<String, dynamic> json, {
    CallRecording? fallback,
  }) {
    final rawCapture = json['capture'];
    final rawDecision = json['decision'];
    final capture = rawCapture is Map ? Map<String, dynamic>.from(rawCapture) : json;
    final decision = rawDecision is Map ? Map<String, dynamic>.from(rawDecision) : capture;
    final rawNotice = json['notice'] ?? capture['notice'];
    final notice = rawNotice is Map ? Map<String, dynamic>.from(rawNotice) : null;

    final canChange = _boolOrNull(decision['can_change']) ??
        _boolOrNull(capture['can_change']) ??
        fallback?.canChange ??
        true;
    final record = _boolOrNull(decision['record']) ?? _boolOrNull(capture['record']);
    var policy = _asStr(json['policy']) ?? _asStr(decision['policy']) ?? fallback?.policy;
    if (policy == null) {
      // The websocket frame carries no policy name. Derive it so the label
      // stays honest instead of claiming a switch that will be refused.
      policy = !canChange
          ? (record == false ? 'disabled' : 'forced')
          : 'optional';
    }

    return CallRecording(
      callId: callId,
      recording: _boolOrNull(capture['recording']) ??
          _boolOrNull(json['recording']) ??
          fallback?.recording ??
          false,
      canChange: canChange,
      policy: policy,
      reason: _asStr(decision['reason']) ??
          _asStr(capture['reason']) ??
          fallback?.reason ??
          '',
      announce: _boolOrNull(decision['announce']) ??
          _boolOrNull(capture['announce']) ??
          fallback?.announce ??
          false,
      noticeText: _asStr(notice?['text']) ?? fallback?.noticeText,
      noticeOk: notice == null ? fallback?.noticeOk : _boolOrNull(notice['ok']),
      noticeError: _asStr(notice?['error']) ?? fallback?.noticeError,
      failedReason: _asStr(capture['failed_reason']) ?? fallback?.failedReason,
      path: _asStr(capture['path']) ??
          _asStr(json['recording_path']) ??
          fallback?.path,
    );
  }
}

/// One SMS in a thread, from /app/conversations/{peer}/messages.
class SmsMessage {
  SmsMessage({
    required this.id,
    required this.direction,
    required this.peer,
    required this.content,
    required this.at,
    this.deviceId,
    this.failed = false,
    this.status,
  });

  final int id;
  final String direction;
  final String peer;
  final String content;
  final int at;
  final int? deviceId;
  final bool failed;
  final String? status;

  bool get isInbound => direction == 'in';
  bool get isPending => direction == 'out_pending';

  factory SmsMessage.fromJson(Map<String, dynamic> json) => SmsMessage(
        id: _asInt(json['id']) ?? 0,
        direction: _asStr(json['direction']) ?? 'in',
        peer: _asStr(json['peer']) ?? '',
        content: json['content']?.toString() ?? '',
        at: _asInt(json['at']) ?? 0,
        deviceId: _asInt(json['device_id']),
        failed: _asBool(json['failed']),
        status: _asStr(json['status']),
      );
}

/// A merged conversation row, from /app/conversations.
class Conversation {
  Conversation({
    required this.peerKey,
    required this.peer,
    required this.lastContent,
    required this.lastAt,
    required this.lastDirection,
    required this.unread,
    required this.messageCount,
    this.name,
    this.draft,
    this.deviceId,
  });

  final String peerKey;
  final String peer;
  final String lastContent;
  final int lastAt;
  final String lastDirection;
  final int unread;
  final int messageCount;
  final String? name;
  final String? draft;
  final int? deviceId;

  String get title => (name == null || name!.isEmpty) ? peer : name!;

  factory Conversation.fromJson(Map<String, dynamic> json) => Conversation(
        peerKey: _asStr(json['peer_key']) ?? '',
        peer: _asStr(json['peer']) ?? '',
        lastContent: json['last_content']?.toString() ?? '',
        lastAt: _asInt(json['last_at']) ?? 0,
        lastDirection: _asStr(json['last_direction']) ?? 'in',
        unread: _asInt(json['unread']) ?? 0,
        messageCount: _asInt(json['message_count']) ?? 0,
        name: _asStr(json['name']),
        draft: _asStr(json['draft']),
        deviceId: _asInt(json['device_id']),
      );
}

class Contact {
  Contact({
    required this.id,
    required this.name,
    required this.number,
    this.groupName,
    this.note,
    this.favorite = false,
  });

  final int id;
  final String name;
  final String number;
  final String? groupName;
  final String? note;
  final bool favorite;

  String get initial => name.isEmpty ? '?' : name.substring(0, 1).toUpperCase();

  factory Contact.fromJson(Map<String, dynamic> json) => Contact(
        id: _asInt(json['id']) ?? 0,
        name: _asStr(json['name']) ?? '',
        number: _asStr(json['number']) ?? '',
        groupName: _asStr(json['group_name']),
        note: _asStr(json['note']),
        favorite: _asBool(json['favorite']),
      );

  Map<String, dynamic> toJson() => {
        'name': name,
        'number': number,
        if (groupName != null) 'group_name': groupName,
        if (note != null) 'note': note,
        'favorite': favorite,
      };
}

class AudioDevice {
  AudioDevice({
    required this.id,
    required this.name,
    required this.kind,
    this.backend,
    this.description,
    this.state,
    this.isDefault = false,
    this.isMonitor = false,
  });

  final String id;
  final String name;
  final String kind;
  final String? backend;
  final String? description;
  final String? state;
  final bool isDefault;
  final bool isMonitor;

  factory AudioDevice.fromJson(Map<String, dynamic> json) => AudioDevice(
        id: _asStr(json['id']) ?? '',
        name: _asStr(json['name']) ?? _asStr(json['id']) ?? '',
        kind: _asStr(json['kind']) ?? 'output',
        backend: _asStr(json['backend']),
        description: _asStr(json['description']),
        state: _asStr(json['state']),
        isDefault: _asBool(json['is_default']),
        isMonitor: _asBool(json['is_monitor']),
      );
}

class AudioState {
  AudioState({
    required this.backend,
    required this.volume,
    required this.outputDevice,
    required this.inputDevice,
    required this.outputs,
    required this.inputs,
  });

  final String backend;
  final double volume;
  final String outputDevice;
  final String inputDevice;
  final List<AudioDevice> outputs;
  final List<AudioDevice> inputs;

  factory AudioState.fromJson(Map<String, dynamic> json) => AudioState(
        backend: _asStr(json['backend']) ?? 'alsa',
        volume: _asDouble(json['volume'], 0.8),
        outputDevice: json['output_device']?.toString() ?? '',
        inputDevice: json['input_device']?.toString() ?? '',
        outputs: ((json['outputs'] as List?) ?? const [])
            .map((e) => AudioDevice.fromJson(Map<String, dynamic>.from(e as Map)))
            .toList(),
        inputs: ((json['inputs'] as List?) ?? const [])
            .map((e) => AudioDevice.fromJson(Map<String, dynamic>.from(e as Map)))
            .toList(),
      );
}

class PromptTone {
  PromptTone({
    required this.id,
    required this.text,
    this.tone,
    this.category,
    this.description,
    this.hasSpokenVersion = false,
    this.filePath,
  });

  final String id;
  final String text;
  final String? tone;
  final String? category;
  final String? description;
  final bool hasSpokenVersion;
  final String? filePath;

  factory PromptTone.fromJson(Map<String, dynamic> json) => PromptTone(
        id: _asStr(json['id']) ?? '',
        text: json['text']?.toString() ?? '',
        tone: _asStr(json['tone']),
        category: _asStr(json['category']),
        description: _asStr(json['description']),
        hasSpokenVersion: _asBool(json['has_spoken_version']),
        filePath: _asStr(json['path']),
      );
}

class UserProfile {
  UserProfile({
    required this.id,
    required this.username,
    required this.role,
    this.displayName,
    this.isActive = true,
    this.totpEnabled = false,
    this.mustChangePassword = false,
    this.lastLoginAt,
  });

  final int id;
  final String username;
  final String role;
  final String? displayName;
  final bool isActive;
  final bool totpEnabled;
  final bool mustChangePassword;
  final int? lastLoginAt;

  bool get isAdmin => role == 'admin';
  String get label => (displayName == null || displayName!.isEmpty) ? username : displayName!;

  factory UserProfile.fromJson(Map<String, dynamic> json) => UserProfile(
        id: _asInt(json['id']) ?? 0,
        username: _asStr(json['username']) ?? '',
        role: _asStr(json['role']) ?? 'operator',
        displayName: _asStr(json['display_name']),
        isActive: _asBool(json['is_active'], true),
        totpEnabled: _asBool(json['totp_enabled']),
        mustChangePassword: _asBool(json['must_change_password']),
        lastLoginAt: _asInt(json['last_login_at']),
      );
}

class Capabilities {
  Capabilities({
    this.voice = false,
    this.sms = true,
    this.ai = false,
    this.admin = false,
  });

  final bool voice;
  final bool sms;
  final bool ai;
  final bool admin;

  factory Capabilities.fromJson(Map<String, dynamic> json) => Capabilities(
        voice: _asBool(json['voice']),
        sms: _asBool(json['sms'], true),
        ai: _asBool(json['ai']),
        admin: _asBool(json['admin']),
      );
}

class ChatTurn {
  ChatTurn({required this.role, required this.content});
  final String role;
  final String content;
  Map<String, dynamic> toJson() => {'role': role, 'content': content};
}

class ChatReply {
  ChatReply({required this.reply, this.provider, this.model, this.latencyMs});
  final String reply;
  final String? provider;
  final String? model;
  final int? latencyMs;

  factory ChatReply.fromJson(Map<String, dynamic> json) => ChatReply(
        reply: json['reply']?.toString() ?? json['content']?.toString() ?? '',
        provider: _asStr(json['provider']),
        model: _asStr(json['model']),
        latencyMs: _asInt(json['latency_ms']),
      );
}


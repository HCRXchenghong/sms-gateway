/// HTTP client for the VoiceSMS Gateway.
///
/// The server accepts the session id either as a cookie or as
/// Authorization: Bearer <sid>; the app always uses the bearer form because
/// Flutter has no shared cookie jar across platforms.
library;

import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import 'models.dart';

class ApiException implements Exception {
  ApiException(this.statusCode, this.message, {this.code});
  final int statusCode;
  final String message;
  final String? code;

  bool get isAuthError => statusCode == 401 || statusCode == 403;

  @override
  String toString() {
    final suffix = code == null ? '' : ' ' + code!;
    return 'ApiException(' + statusCode.toString() + suffix + '): ' + message;
  }
}

class LoginResult {
  LoginResult({
    required this.token,
    required this.expiresIn,
    required this.user,
    required this.mustChangePassword,
    this.voiceEnabled = true,
  });

  final String token;
  final int expiresIn;
  final UserProfile user;
  final bool mustChangePassword;
  final bool voiceEnabled;
}

class ApiClient {
  ApiClient({
    this.baseUrl,
    http.Client? httpClient,
    this.timeout = const Duration(seconds: 30),
  }) : _http = httpClient ?? http.Client();

  String? baseUrl;
  final http.Client _http;
  final Duration timeout;
  String? _token;

  /// Called when the server rejects the token so the UI can force a re-login.
  void Function()? onUnauthorized;

  set token(String? value) => _token = value;
  String? get token => _token;

  bool get isConfigured => baseUrl != null && baseUrl!.isNotEmpty;

  String _url(String path, [Map<String, dynamic>? query]) {
    final root = (baseUrl ?? '').replaceAll(RegExp(r'/+$'), '');
    final uri = Uri.parse(root + path);
    if (query == null || query.isEmpty) return uri.toString();
    final merged = Map<String, String>.from(uri.queryParameters);
    query.forEach((k, v) {
      if (v != null) merged[k] = v.toString();
    });
    return uri.replace(queryParameters: merged).toString();
  }

  Map<String, String> _headers({bool json = true}) {
    final h = <String, String>{'Accept': 'application/json'};
    if (json) h['Content-Type'] = 'application/json; charset=utf-8';
    if (_token != null && _token!.isNotEmpty)
      h['Authorization'] = 'Bearer $_token';
    return h;
  }

  Future<dynamic> _send(Future<http.Response> Function() send) async {
    final http.Response resp;
    try {
      resp = await send().timeout(timeout);
    } on TimeoutException {
      throw ApiException(0, '请求超时，请检查服务器地址与网络');
    } catch (e) {
      throw ApiException(0, '无法连接服务器: ' + e.toString());
    }
    if (resp.statusCode == 401 || resp.statusCode == 403) {
      onUnauthorized?.call();
    }
    final body = resp.body;
    dynamic decoded;
    if (body.isNotEmpty) {
      try {
        decoded = jsonDecode(body);
      } on FormatException {
        decoded = body;
      }
    }
    if (resp.statusCode >= 400) {
      throw ApiException(
        resp.statusCode,
        _errorMessage(decoded),
        code: _errorCode(decoded),
      );
    }
    return decoded;
  }

  String _errorMessage(dynamic decoded) {
    if (decoded is Map) {
      if (decoded['detail'] is Map) return _errorMessage(decoded['detail']);
      final err = decoded['error'];
      if (err is Map)
        return (err['message'] ?? err['code'] ?? '请求失败').toString();
      for (final k in const ['message', 'detail', 'error']) {
        if (decoded[k] != null) return decoded[k].toString();
      }
    }
    if (decoded is String && decoded.isNotEmpty) return decoded;
    return '请求失败';
  }

  String? _errorCode(dynamic decoded) {
    if (decoded is Map) {
      if (decoded['detail'] is Map) return _errorCode(decoded['detail']);
      final err = decoded['error'];
      if (err is Map) return err['code']?.toString();
      return decoded['code']?.toString();
    }
    return null;
  }

  Future<dynamic> get(String path, {Map<String, dynamic>? query}) => _send(
    () =>
        _http.get(Uri.parse(_url(path, query)), headers: _headers(json: false)),
  );

  Future<dynamic> post(
    String path, {
    Object? body,
    Map<String, dynamic>? query,
  }) => _send(
    () => _http.post(
      Uri.parse(_url(path, query)),
      headers: _headers(),
      body: body == null ? null : jsonEncode(body),
    ),
  );

  Future<dynamic> put(String path, {Object? body}) => _send(
    () => _http.put(
      Uri.parse(_url(path)),
      headers: _headers(),
      body: jsonEncode(body ?? {}),
    ),
  );

  Future<dynamic> patch(String path, {Object? body}) => _send(
    () => _http.patch(
      Uri.parse(_url(path)),
      headers: _headers(),
      body: jsonEncode(body ?? {}),
    ),
  );

  Future<dynamic> delete(String path) => _send(
    () => _http.delete(Uri.parse(_url(path)), headers: _headers(json: false)),
  );

  Future<List<int>> getBytes(String path) async {
    final resp = await _http.get(
      Uri.parse(_url(path)),
      headers: _headers(json: false),
    );
    if (resp.statusCode >= 400) {
      throw ApiException(
        resp.statusCode,
        '下载失败 (' + resp.statusCode.toString() + ')',
      );
    }
    return resp.bodyBytes;
  }

  /// POST that returns the raw body. /app/ai/tts answers with audio/wav, so a
  /// JSON decoding wrapper would corrupt it.
  Future<List<int>> postBytes(String path, Object body) async {
    final resp = await _http.post(
      Uri.parse(_url(path)),
      headers: _headers(),
      body: jsonEncode(body),
    );
    if (resp.statusCode == 401 || resp.statusCode == 403)
      onUnauthorized?.call();
    if (resp.statusCode >= 400) {
      throw ApiException(resp.statusCode, _errorMessage(_tryJson(resp.body)));
    }
    return resp.bodyBytes;
  }

  dynamic _tryJson(String body) {
    if (body.isEmpty) return null;
    try {
      return jsonDecode(body);
    } on FormatException {
      return body;
    }
  }

  // ---------------------------------------------------------------- auth

  Future<LoginResult> login({
    required String username,
    required String password,
    String code = '',
    String recovery = '',
  }) async {
    final data = await _send(
      () => _http.post(
        Uri.parse(_url('/api/v1/auth/login')),
        headers: _headers(),
        body: jsonEncode({
          'username': username,
          'password': password,
          if (code.isNotEmpty) 'code': code,
          if (recovery.isNotEmpty) 'recovery': recovery,
        }),
      ),
    ) as Map<String, dynamic>;
    final token = (data['token'] ?? '').toString();
    _token = token;
    final server = data['server'];
    return LoginResult(
      token: token,
      expiresIn: _asIntLocal(data['expires_in']) ?? 28800,
      user: UserProfile.fromJson(
        Map<String, dynamic>.from(data['user'] as Map),
      ),
      mustChangePassword: data['must_change_password'] == true,
      voiceEnabled: server is Map ? server['voice_enabled'] != false : true,
    );
  }

  Future<void> logout() async {
    try {
      await post('/api/v1/auth/logout');
    } catch (_) {
      // A logout that fails on the server still clears the local session.
    }
    _token = null;
  }

  Future<MeResult> me() async {
    final data = await get('/api/v1/auth/me') as Map<String, dynamic>;
    return MeResult(
      user: UserProfile.fromJson(
        Map<String, dynamic>.from(data['user'] as Map),
      ),
      caps: Capabilities.fromJson(
        Map<String, dynamic>.from(data['capabilities'] as Map? ?? {}),
      ),
      devices: _deviceList(data['devices']),
      activeCalls: parseActive(data['active_calls']),
    );
  }

  List<DeviceInfo> _deviceList(Object? raw) => ((raw as List?) ?? const [])
      .map((e) => DeviceInfo.fromJson(Map<String, dynamic>.from(e as Map)))
      .toList();

  // ------------------------------------------------------------- devices

  Future<List<DeviceInfo>> devices() async {
    final data = await get('/app/devices') as Map<String, dynamic>;
    return _deviceList(data['items']);
  }

  Future<Map<String, dynamic>> deviceSignal(int deviceId) async {
    final data = await get('/app/devices/' + deviceId.toString() + '/signal');
    return Map<String, dynamic>.from(data as Map);
  }

  Future<dynamic> sendRawAt(int deviceId, String command) => post(
    '/app/devices/' + deviceId.toString() + '/at',
    body: {'command': command},
  );

  // ----------------------------------------------------------------- sms

  Future<List<Conversation>> conversations({
    int limit = 50,
    int? deviceId,
    String? q,
  }) async {
    final data = await get(
      '/app/conversations',
      query: {
        'limit': limit,
        if (deviceId != null) 'device_id': deviceId,
        if (q != null) 'q': q,
      },
    ) as Map<String, dynamic>;
    return ((data['items'] as List?) ?? const [])
        .map((e) => Conversation.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
  }

  Future<List<SmsMessage>> thread(
    String peer, {
    int limit = 100,
    int? before,
    bool markRead = true,
  }) async {
    final data = await get(
      '/app/conversations/' + Uri.encodeComponent(peer) + '/messages',
      query: {
        'limit': limit,
        'mark_read': markRead,
        if (before != null) 'before': before,
      },
    ) as Map<String, dynamic>;
    return ((data['items'] as List?) ?? const [])
        .map((e) => SmsMessage.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
  }

  Future<dynamic> markConversation(String peer, {required bool read}) => post(
    '/app/conversations/' +
        Uri.encodeComponent(peer) +
        (read ? '/read' : '/unread'),
  );

  Future<dynamic> deleteConversations(List<String> peers) =>
      post('/app/conversations/bulk_delete', body: {'peers': peers});

  Future<dynamic> deleteReadConversations() =>
      post('/app/conversations/delete_read');

  Future<dynamic> sendSms({
    required String to,
    required String content,
    int? deviceId,
  }) => post(
    '/app/send',
    body: {
      'to': to,
      'content': content,
      if (deviceId != null) 'device_id': deviceId,
    },
  );

  Future<dynamic> saveDraft(String peer, String? draft) => post(
    '/app/conversations/' + Uri.encodeComponent(peer) + '/draft',
    body: {'draft': draft},
  );

  Future<Map<String, dynamic>> search(String q, {int limit = 50}) async {
    final data = await get(
      '/app/search',
      query: {'q': q, 'limit': limit},
    ) as Map<String, dynamic>;
    return data;
  }

  // ------------------------------------------------------------ contacts

  Future<ContactPage> contacts({String? q, String? group}) async {
    final data = await get(
      '/app/contacts',
      query: {if (q != null) 'q': q, if (group != null) 'group': group},
    ) as Map<String, dynamic>;
    return ContactPage(
      items: ((data['items'] as List?) ?? const [])
          .map((e) => Contact.fromJson(Map<String, dynamic>.from(e as Map)))
          .toList(),
      groups: ((data['groups'] as List?) ?? const [])
          .map((e) => e.toString())
          .toList(),
    );
  }

  Future<Contact> createContact(Contact c) async {
    final data =
        await post('/app/contacts', body: c.toJson()) as Map<String, dynamic>;
    return Contact.fromJson(data);
  }

  Future<Contact> updateContact(int id, Contact c) async {
    final data = await patch(
      '/app/contacts/' + id.toString(),
      body: c.toJson(),
    ) as Map<String, dynamic>;
    return Contact.fromJson(data);
  }

  Future<dynamic> deleteContact(int id) =>
      delete('/app/contacts/' + id.toString());

  // --------------------------------------------------------------- voice

  Future<Map<String, dynamic>> voiceState() async {
    final data = await get('/app/voice/state') as Map<String, dynamic>;
    return data;
  }

  List<CallSession> parseActive(Object? raw) => ((raw as List?) ?? const [])
      .map((e) => CallSession.fromJson(Map<String, dynamic>.from(e as Map)))
      .toList();

  Future<List<CallSession>> activeCalls() async {
    final data = await voiceState();
    return parseActive(data['active']);
  }

  Future<CallSession> dial({required String number, int? deviceId}) async {
    final data = await post(
      '/app/voice/dial',
      body: {'number': number, if (deviceId != null) 'device_id': deviceId},
    ) as Map<String, dynamic>;
    final session = data['session'] ?? data['call'] ?? data;
    return CallSession.fromJson(Map<String, dynamic>.from(session as Map));
  }

  Future<dynamic> answer(int deviceId) =>
      post('/app/voice/' + deviceId.toString() + '/answer');
  Future<dynamic> hangup(int deviceId) =>
      post('/app/voice/' + deviceId.toString() + '/hangup');
  Future<dynamic> dtmf(int deviceId, String digits) => post(
    '/app/voice/' + deviceId.toString() + '/dtmf',
    body: {'digits': digits},
  );
  Future<dynamic> speak(int deviceId, String text, {int? mode}) => post(
    '/app/voice/' + deviceId.toString() + '/tts',
    body: {'text': text, if (mode != null) 'mode': mode},
  );
  Future<dynamic> stopSpeak(int deviceId) =>
      post('/app/voice/' + deviceId.toString() + '/tts/stop');

  Future<List<CallRecord>> callHistory({
    int limit = 50,
    int? deviceId,
    String? direction,
  }) async {
    final data = await get(
      '/app/voice/history',
      query: {
        'limit': limit,
        if (deviceId != null) 'device_id': deviceId,
        if (direction != null) 'direction': direction,
      },
    ) as Map<String, dynamic>;
    return ((data['items'] as List?) ?? const [])
        .map((e) => CallRecord.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
  }

  // --------------------------------------------------------------- audio

  Future<AudioState> audioDevices() async {
    final data = await get('/app/audio/devices') as Map<String, dynamic>;
    return AudioState.fromJson(data);
  }

  Future<AudioState> setOutput(String device) async {
    final data = await post(
      '/app/audio/output',
      body: {'device': device},
    ) as Map<String, dynamic>;
    return AudioState.fromJson(data);
  }

  Future<AudioState> setInput(String device) async {
    final data = await post(
      '/app/audio/input',
      body: {'device': device},
    ) as Map<String, dynamic>;
    return AudioState.fromJson(data);
  }

  Future<AudioState> setVolume(double volume) async {
    final data = await post(
      '/app/audio/volume',
      body: {'volume': volume},
    ) as Map<String, dynamic>;
    return AudioState.fromJson(data);
  }

  Future<List<PromptTone>> prompts() async {
    final data = await get('/app/audio/prompts') as Map<String, dynamic>;
    return ((data['items'] as List?) ?? const [])
        .map((e) => PromptTone.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
  }

  Future<dynamic> playPrompt(String promptId) =>
      post('/app/audio/prompts/' + promptId + '/play');

  String promptFileUrl(String promptId) =>
      _url('/app/audio/prompts/' + promptId + '/file');

  // ------------------------------------------------------------------ ai

  Future<Map<String, dynamic>> aiStatus() async {
    final data = await get('/app/ai/status') as Map<String, dynamic>;
    return data;
  }

  Future<Map<String, dynamic>> aiHealth() async {
    final data = await get('/app/ai/health') as Map<String, dynamic>;
    return data;
  }

  Future<ChatReply> chat({
    required String message,
    List<ChatTurn> history = const [],
    String? system,
    double temperature = 0.4,
    int maxTokens = 800,
  }) async {
    final data = await post(
      '/app/ai/chat',
      body: {
        'message': message,
        'history': history.map((e) => e.toJson()).toList(),
        if (system != null) 'system': system,
        'temperature': temperature,
        'max_tokens': maxTokens,
      },
    ) as Map<String, dynamic>;
    return ChatReply.fromJson(data);
  }

  /// Cloud TTS. Returns WAV bytes; an empty list means the gateway is
  /// configured to synthesise on the module instead.
  Future<List<int>> tts(String text, {String? voice}) => postBytes(
    '/app/ai/tts',
    {'text': text, if (voice != null) 'voice': voice},
  );

  /// Render a prompt tone with the AI voice. Admin only.
  Future<dynamic> renderPromptWithAi(String promptId) =>
      post('/app/ai/tts/prompt/' + promptId);

  /// Ask the model then play the answer to the far end.
  /// engine=module uses the A7670 built in AT+CTTS synthesiser, engine=audio
  /// renders cloud TTS and plays it through the sound card bound to the module.
  Future<Map<String, dynamic>> aiSpeakInCall(
    int deviceId,
    String text, {
    String engine = 'module',
  }) async {
    final data = await post(
      '/app/voice/' + deviceId.toString() + '/ai/speak',
      body: {'text': text, 'engine': engine},
    ) as Map<String, dynamic>;
    return data;
  }

  // ---------------------------------------------------------- call flow
  //
  // Live translation, the call agent and the minutes all live behind these.
  // The shapes mirror what the console sees, so the app and the web UI agree.

  Future<Map<String, dynamic>> flowState() async =>
      Map<String, dynamic>.from(await get('/app/voice/flow') as Map);

  Future<Map<String, dynamic>> callFlow(String callId) async =>
      Map<String, dynamic>.from(
          await get('/app/voice/calls/$callId/flow') as Map);

  Future<List<Map<String, dynamic>>> callTranscript(
    String callId, {
    int limit = 200,
  }) async {
    final raw = await get('/app/voice/calls/$callId/transcript',
        query: {'limit': limit});
    final items = (raw['items'] as List?) ?? const <dynamic>[];
    return items
        .map((e) => Map<String, dynamic>.from(e as Map))
        .toList(growable: false);
  }

  Future<Map<String, dynamic>> setTranslation(
    String callId, {
    String? mode,
    String? cadence,
    String? myLanguage,
    String? peerLanguage,
  }) =>
      post('/app/voice/calls/$callId/translate', body: {
        if (mode != null) 'mode': mode,
        if (cadence != null) 'cadence': cadence,
        if (myLanguage != null) 'my_language': myLanguage,
        if (peerLanguage != null) 'peer_language': peerLanguage,
      }).then((raw) => Map<String, dynamic>.from(raw as Map));

  Future<dynamic> stopTranslation(String callId) =>
      delete('/app/voice/calls/$callId/translate');

  /// Say something to the far end. Translated first when translation is on.
  Future<Map<String, dynamic>> sayToCall(String callId, String text) =>
      post('/app/voice/calls/$callId/say', body: {'text': text})
          .then((raw) => Map<String, dynamic>.from(raw as Map));

  Future<Map<String, dynamic>> attachAgent(
    String callId, {
    required String purpose,
    String? context,
    String? peerLanguage,
    int? maxSeconds,
    String? opener,
  }) =>
      post('/app/voice/calls/$callId/agent', body: {
        'purpose': purpose,
        if (context != null) 'context': context,
        if (peerLanguage != null) 'peer_language': peerLanguage,
        if (maxSeconds != null) 'max_seconds': maxSeconds,
        if (opener != null) 'opener': opener,
      }).then((raw) => Map<String, dynamic>.from(raw as Map));

  Future<Map<String, dynamic>> agentHandover(String callId) =>
      post('/app/voice/calls/$callId/agent/handover')
          .then((raw) => Map<String, dynamic>.from(raw as Map));

  Future<Map<String, dynamic>> callSummary(String callId) async =>
      Map<String, dynamic>.from(
          await get('/app/voice/calls/$callId/summary') as Map);

  // ------------------------------------------------------- ai call tasks

  Future<Map<String, dynamic>> createAiCallTask({
    required String purpose,
    String? number,
    String direction = 'outbound',
    String? context,
    String? language,
    String? peerLanguage,
    int? maxSeconds,
    String? reference,
  }) =>
      post('/app/ai-call/tasks', body: {
        'purpose': purpose,
        if (number != null) 'number': number,
        'direction': direction,
        if (context != null) 'context': context,
        if (language != null) 'language': language,
        if (peerLanguage != null) 'peer_language': peerLanguage,
        if (maxSeconds != null) 'max_seconds': maxSeconds,
        if (reference != null) 'reference': reference,
      }).then((raw) => Map<String, dynamic>.from(raw as Map));

  Future<List<Map<String, dynamic>>> aiCallTasks({
    int limit = 50,
    String? status,
  }) async {
    final raw = await get('/app/ai-call/tasks', query: {
      'limit': limit,
      if (status != null) 'task_status': status,
    });
    final items = (raw['items'] as List?) ?? const <dynamic>[];
    return items
        .map((e) => Map<String, dynamic>.from(e as Map))
        .toList(growable: false);
  }

  Future<Map<String, dynamic>> aiCallTask(int id) async =>
      Map<String, dynamic>.from(await get('/app/ai-call/tasks/$id') as Map);

  Future<Map<String, dynamic>> cancelAiCallTask(int id) =>
      post('/app/ai-call/tasks/$id/cancel')
          .then((raw) => Map<String, dynamic>.from(raw as Map));

  // -------------------------------------------------------------- admin

  Future<Map<String, dynamic>> settings() async {
    final data = await get('/app/admin/settings') as Map<String, dynamic>;
    return data;
  }

  Future<Map<String, dynamic>> saveSettings(Map<String, dynamic> values) async {
    final data = await put(
      '/app/admin/settings',
      body: {'settings': values},
    ) as Map<String, dynamic>;
    return data;
  }

  Future<Map<String, dynamic>> stats() async {
    final data = await get('/app/admin/stats') as Map<String, dynamic>;
    return data;
  }

  Future<List<Map<String, dynamic>>> users() async {
    final data = await get('/app/admin/users') as Map<String, dynamic>;
    return ((data['items'] as List?) ?? const [])
        .map((e) => Map<String, dynamic>.from(e as Map))
        .toList();
  }

  Future<dynamic> createUser({
    required String username,
    required String password,
    String role = 'operator',
    String? displayName,
  }) => post(
    '/app/admin/users',
    body: {
      'username': username,
      'password': password,
      'role': role,
      if (displayName != null) 'display_name': displayName,
    },
  );

  Future<dynamic> updateUser(
    int id, {
    String? role,
    bool? isActive,
    String? displayName,
  }) => patch(
    '/app/admin/users/' + id.toString(),
    body: {
      if (role != null) 'role': role,
      if (isActive != null) 'is_active': isActive,
      if (displayName != null) 'display_name': displayName,
    },
  );

  Future<dynamic> resetPassword(int id, String password) => post(
    '/app/admin/users/' + id.toString() + '/password',
    body: {'password': password},
  );

  Future<dynamic> deleteUser(int id) =>
      delete('/app/admin/users/' + id.toString());

  Future<dynamic> changeMyPassword(String current, String next) => post(
    '/app/account/password',
    body: {'current_password': current, 'new_password': next},
  );

  // ---------------------------------------------------------- mock hooks

  Future<dynamic> mockIncomingCall(int deviceId, String number) => post(
    '/app/voice/mock/' + deviceId.toString() + '/incoming',
    body: {'number': number},
  );

  Future<dynamic> mockIncomingSms(
    int deviceId,
    String sender,
    String content,
  ) => post(
    '/app/sms/mock/' + deviceId.toString() + '/incoming',
    body: {'sender': sender, 'content': content},
  );

  Future<dynamic> mockScenario(int deviceId, String scenario) => post(
    '/app/voice/mock/' + deviceId.toString() + '/scenario',
    body: {'scenario': scenario},
  );

  void close() => _http.close();
}

class MeResult {
  MeResult({
    required this.user,
    required this.caps,
    required this.devices,
    required this.activeCalls,
  });
  final UserProfile user;
  final Capabilities caps;
  final List<DeviceInfo> devices;
  final List<CallSession> activeCalls;
}

class ContactPage {
  ContactPage({required this.items, required this.groups});
  final List<Contact> items;
  final List<String> groups;
}

int? _asIntLocal(Object? v) {
  if (v == null) return null;
  if (v is int) return v;
  if (v is num) return v.toInt();
  return int.tryParse(v.toString());
}

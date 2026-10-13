/// Owns the API client, the persisted session and every feature controller.
///
/// Realtime events arrive here first and are fanned out to the controllers
/// that care, which keeps the WebSocket wiring in one place.
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../data/api_client.dart';
import '../data/models.dart';
import '../data/realtime_client.dart';
import '../data/session_store.dart';
import 'controllers.dart';

enum AuthPhase { unknown, loggedOut, loggedIn }

class AppScope extends ChangeNotifier {
  AppScope({ApiClient? api, SessionStore? store})
      : api = api ?? ApiClient(),
        store = store ?? SessionStore() {
    devices = DevicesController(this.api);
    calls = CallsController(this.api);
    messages = MessagesController(this.api);
    contacts = ContactsController(this.api);
    audio = AudioController(this.api);
    ai = AiController(this.api);
    settings = SettingsController(this.api);
    users = UsersController(this.api);

    realtime.onEvent = _onRealtimeEvent;
    realtime.onReconnect = _reconnect;
    this.api.onUnauthorized = _onUnauthorized;
  }

  final ApiClient api;
  final SessionStore store;
  final RealtimeClient realtime = RealtimeClient();

  late final DevicesController devices;
  late final CallsController calls;
  late final MessagesController messages;
  late final ContactsController contacts;
  late final AudioController audio;
  late final AiController ai;
  late final SettingsController settings;
  late final UsersController users;

  AuthPhase _phase = AuthPhase.unknown;
  UserProfile? _user;
  Capabilities _caps = Capabilities();
  String? _loginError;
  bool _restoring = false;

  AuthPhase get phase => _phase;
  UserProfile? get user => _user;
  Capabilities get caps => _caps;
  String? get loginError => _loginError;
  bool get restoring => _restoring;
  bool get isLoggedIn => _phase == AuthPhase.loggedIn;

  /// Feed a realtime event in from tests without a live socket.
  @visibleForTesting
  void handleEvent(RealtimeEvent event) => _onRealtimeEvent(event);

  void _onRealtimeEvent(RealtimeEvent event) {
    if (event.isCall) calls.applyEvent(event);
    if (event.isSms) messages.applyEvent(event);
    if (event.event.startsWith('device.')) unawaited(devices.refresh());
  }

  /// Restore a saved session on cold start. Returns true when logged in.
  Future<bool> restore() async {
    _restoring = true;
    notifyListeners();
    try {
      await store.init();
      final server = store.serverUrl;
      final token = store.token;
      if (server == null || server.isEmpty || token == null || token.isEmpty) {
        _phase = AuthPhase.loggedOut;
        return false;
      }
      api.baseUrl = server;
      api.token = token;
      final me = await api.me();
      _user = me.user;
      _caps = me.caps;
      _phase = AuthPhase.loggedIn;
      if (store.preferredDeviceId != null) devices.setPreferred(store.preferredDeviceId);
      unawaited(_loadEverything());
      _connectRealtime();
      return true;
    } on ApiException catch (e) {
      if (e.isAuthError) {
        await store.clearSession();
        api.token = null;
      }
      _phase = AuthPhase.loggedOut;
      return false;
    } catch (_) {
      // Server unreachable: stay logged out so the login page can explain why.
      _phase = AuthPhase.loggedOut;
      return false;
    } finally {
      _restoring = false;
      notifyListeners();
    }
  }

  Future<void> _loadEverything() async {
    await Future.wait([
      devices.refresh(),
      calls.refresh(),
      calls.refreshHistory(),
      messages.refreshConversations(),
      contacts.refresh(),
      audio.refresh(),
      ai.refresh(),
      if (_caps.admin) settings.refresh(),
    ]);
  }

  Future<bool> login({
    required String serverUrl,
    required String username,
    required String password,
    String code = '',
    String recovery = '',
  }) async {
    _loginError = null;
    notifyListeners();
    final normalized = SessionStore.normalizeUrl(serverUrl);
    if (normalized.isEmpty) {
      _loginError = '请填写服务器地址';
      notifyListeners();
      return false;
    }
    api.baseUrl = normalized;
    try {
      final result = await api.login(
        username: username,
        password: password,
        code: code,
        recovery: recovery,
      );
      _user = result.user;
      _phase = AuthPhase.loggedIn;
      await store.saveServer(normalized);
      await store.saveSession(token: result.token, username: username);
      final me = await api.me();
      _user = me.user;
      _caps = me.caps;
      unawaited(_loadEverything());
      _connectRealtime();
      notifyListeners();
      return true;
    } on ApiException catch (e) {
      _loginError = e.message;
      _phase = AuthPhase.loggedOut;
      notifyListeners();
      return false;
    } catch (e) {
      _loginError = e.toString();
      _phase = AuthPhase.loggedOut;
      notifyListeners();
      return false;
    }
  }

  Future<void> logout() async {
    realtime.disconnect();
    await api.logout();
    await store.clearSession();
    api.token = null;
    _user = null;
    _caps = Capabilities();
    _phase = AuthPhase.loggedOut;
    notifyListeners();
  }

  void _connectRealtime() {
    final server = api.baseUrl;
    final token = api.token;
    if (server == null || token == null) return;
    realtime.connect(server, token);
  }

  void _reconnect() {
    if (!isLoggedIn) return;
    _connectRealtime();
  }

  void _onUnauthorized() {
    if (_phase != AuthPhase.loggedIn) return;
    _phase = AuthPhase.loggedOut;
    _user = null;
    realtime.disconnect();
    unawaited(store.clearSession());
    notifyListeners();
  }

  Future<void> selectDevice(int? id) async {
    devices.setPreferred(id);
    await store.savePreferredDevice(id);
  }

  /// Convenience wrapper used by the dialer.
  Future<CallSession?> dial(String number) =>
      calls.dial(number: number, deviceId: devices.effective?.id);

  RealtimeStatus get connectionStatus => realtime.status;

  @override
  void dispose() {
    realtime.dispose();
    devices.dispose();
    calls.dispose();
    messages.dispose();
    contacts.dispose();
    audio.dispose();
    ai.dispose();
    settings.dispose();
    users.dispose();
    api.close();
    super.dispose();
  }
}


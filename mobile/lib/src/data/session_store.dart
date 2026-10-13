/// Persists the server address and bearer token between app launches.
library;

import 'package:shared_preferences/shared_preferences.dart';

class SessionStore {
  static const _kServer = 'smsgw.server_url';
  static const _kToken = 'smsgw.token';
  static const _kUser = 'smsgw.username';
  static const _kDevice = 'smsgw.preferred_device_id';

  SharedPreferences? _prefs;

  Future<void> init() async {
    _prefs ??= await SharedPreferences.getInstance();
  }

  String? get serverUrl => _prefs?.getString(_kServer);
  String? get token => _prefs?.getString(_kToken);
  String? get lastUsername => _prefs?.getString(_kUser);
  int? get preferredDeviceId => _prefs?.getInt(_kDevice);

  Future<void> saveServer(String url) async {
    await _prefs?.setString(_kServer, _normalize(url));
  }

  Future<void> saveSession({required String token, required String username}) async {
    await _prefs?.setString(_kToken, token);
    await _prefs?.setString(_kUser, username);
  }

  Future<void> savePreferredDevice(int? id) async {
    if (id == null) {
      await _prefs?.remove(_kDevice);
    } else {
      await _prefs?.setInt(_kDevice, id);
    }
  }

  Future<void> clearSession() async {
    await _prefs?.remove(_kToken);
  }

  Future<void> clearAll() async {
    await _prefs?.remove(_kToken);
    await _prefs?.remove(_kUser);
    await _prefs?.remove(_kDevice);
  }

  /// Accepts host:port, http://host:port and https://host:port.
  static String _normalize(String raw) {
    var url = raw.trim();
    if (url.isEmpty) return url;
    if (!url.startsWith('http://') && !url.startsWith('https://')) {
      url = 'http://' + url;
    }
    while (url.endsWith('/')) {
      url = url.substring(0, url.length - 1);
    }
    return url;
  }

  static String normalizeUrl(String raw) => _normalize(raw);
}


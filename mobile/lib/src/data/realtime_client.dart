/// WebSocket client for the realtime event stream at /ws.
///
/// The server authenticates the socket from the cookie or a ?token= query
/// parameter. Browsers cannot set headers on a WebSocket handshake, so the app
/// always passes the token in the query string.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:web_socket_channel/web_socket_channel.dart';

/// One decoded server event: {"event": <name>, "data": <payload>}.
class RealtimeEvent {
  RealtimeEvent(this.event, this.data);
  final String event;
  final Map<String, dynamic> data;

  bool get isCall => event.startsWith('call.');
  bool get isSms => event.startsWith('sms.');
}

class RealtimeClient {
  RealtimeClient({this.onEvent, this.onStatusChange});

  void Function(RealtimeEvent event)? onEvent;
  void Function(RealtimeStatus status)? onStatusChange;

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _sub;
  Timer? _reconnect;
  Timer? _heartbeat;
  int _attempt = 0;
  bool _closedByUser = false;
  RealtimeStatus _status = RealtimeStatus.disconnected;

  RealtimeStatus get status => _status;
  bool get isConnected => _status == RealtimeStatus.connected;

  void _setStatus(RealtimeStatus s) {
    if (_status == s) return;
    _status = s;
    onStatusChange?.call(s);
  }

  /// [httpBaseUrl] looks like http://host:port; it is rewritten to ws/wss.
  void connect(String httpBaseUrl, String token) {
    _closedByUser = false;
    disconnect(silent: true);
    final uri = Uri.parse(httpBaseUrl).replace(
      scheme: httpBaseUrl.startsWith('https') ? 'wss' : 'ws',
      path: '/ws',
      queryParameters: {'token': token},
    );
    _setStatus(RealtimeStatus.connecting);
    try {
      _channel = WebSocketChannel.connect(uri);
    } catch (e) {
      _setStatus(RealtimeStatus.disconnected);
      _scheduleReconnect();
      return;
    }
    _sub = _channel!.stream.listen(
      _onMessage,
      onError: (Object e) {
        _setStatus(RealtimeStatus.disconnected);
        _scheduleReconnect();
      },
      onDone: () {
        _setStatus(RealtimeStatus.disconnected);
        _scheduleReconnect();
      },
      cancelOnError: true,
    );
    _heartbeat?.cancel();
    _heartbeat = Timer.periodic(const Duration(seconds: 25), (_) {
      // The server only reads to detect disconnects; any frame keeps it alive.
      try {
        _channel?.sink.add('ping');
      } catch (_) {}
    });
  }

  void _onMessage(dynamic raw) {
    if (raw is! String) return;
    if (raw == 'pong') return;
    _setStatus(RealtimeStatus.connected);
    _attempt = 0;
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map) return;
      final name = (decoded['event'] ?? '').toString();
      final payload = decoded['data'];
      if (name.isEmpty) return;
      onEvent?.call(RealtimeEvent(
        name,
        payload is Map ? Map<String, dynamic>.from(payload) : <String, dynamic>{},
      ));
    } catch (_) {
      // Ignore malformed frames rather than tearing down the socket.
    }
  }

  void _scheduleReconnect() {
    if (_closedByUser) return;
    _reconnect?.cancel();
    _attempt += 1;
    // Exponential backoff capped at 15s, with jitter so many clients do not
    // reconnect in lockstep after a server restart.
    final base = min(pow(2, _attempt.clamp(1, 4)).toDouble(), 15.0);
    final delay = Duration(milliseconds: (base * 1000).round() + Random().nextInt(500));
    _setStatus(RealtimeStatus.reconnecting);
    _reconnect = Timer(delay, () {
      if (_closedByUser) return;
      final ch = _channel;
      if (ch == null) return;
      // Re-run connect with the last known target by asking the owner.
      onReconnect?.call();
    });
  }

  /// Set by the owner so the client can re-establish the session after a drop.
  void Function()? onReconnect;

  void disconnect({bool silent = false}) {
    if (!silent) _closedByUser = true;
    _reconnect?.cancel();
    _heartbeat?.cancel();
    _sub?.cancel();
    _sub = null;
    try {
      _channel?.sink.close();
    } catch (_) {}
    _channel = null;
    _setStatus(RealtimeStatus.disconnected);
  }

  void dispose() => disconnect();
}

enum RealtimeStatus { disconnected, connecting, connected, reconnecting }


/// Keeps the most recent state of every call id the app has seen.
///
/// The in-call screen and the router both need to know whether a specific call
/// is still live. HTTP refreshes, WebSocket reconnects and out-of-order events
/// can briefly omit or replace that session, so the last known state lives here
/// instead of being inferred from the transient active list alone.
library;

import '../data/models.dart';

class CallSessionRegistry {
  final Map<String, CallSession> _byId = {};

  void update(CallSession session) {
    if (session.callId.isEmpty) return;
    final existing = _byId[session.callId];
    // Never let a stale payload resurrect or overwrite an ended call.
    if (existing != null && existing.isEnded && !session.isEnded) return;
    _byId[session.callId] = session;
  }

  CallSession? operator [](String callId) => _byId[callId];

  void remove(String callId) => _byId.remove(callId);

  void clear() => _byId.clear();
}

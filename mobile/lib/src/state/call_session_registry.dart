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

  /// Ids whose last known state is still live.
  List<String> liveIds() =>
      _byId.entries.where((e) => !e.value.isEnded).map((e) => e.key).toList();

  /// Book a call as ended because the gateway no longer lists it as live.
  ///
  /// The frame carrying call.ended can be missed: the socket drops and
  /// reconnects, or the app sits in the background while the call finishes. A
  /// finished call then simply disappears from /app/voice/state, so nothing
  /// ever corrects the last state seen here and the in-call screen keeps
  /// showing 对方振铃中 after the far end already hung up. Marking the gap as
  /// ended is what lets that screen close on a poll alone.
  bool markEnded(String callId, {String? endReason, int? endedAt}) {
    final existing = _byId[callId];
    if (existing == null || existing.isEnded) return false;
    _byId[callId] = existing.copyWith(
      state: CallState.ended,
      endReason: endReason,
      endedAt: endedAt ?? DateTime.now().millisecondsSinceEpoch ~/ 1000,
    );
    return true;
  }

  void remove(String callId) => _byId.remove(callId);

  void clear() => _byId.clear();
}

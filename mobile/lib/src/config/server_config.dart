/// The gateway endpoint compiled into the app.
///
/// Every deployment ships its own build: pass
/// `--dart-define=SMSGW_SERVER_URL=https://gw.example.com` to point the package
/// at another gateway. The default is the office LAN gateway used for
/// integration testing, so a fresh install logs in without typing an address.
library;

const String builtInServerUrl = String.fromEnvironment(
  'SMSGW_SERVER_URL',
  defaultValue: 'http://192.168.31.110:8811',
);

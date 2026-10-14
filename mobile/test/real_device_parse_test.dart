import 'dart:convert';

import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:voicesms_app/src/data/models.dart';

// Copied verbatim from GET /app/devices on a live gateway with one real
// A7670G-LLSE attached plus two leftover mock rows. The app showed "no device"
// against exactly this payload, so parsing it here pins the contract.
const String _payload = '''
{
 "items": [
  {"id":1,"number":1,"code":"001","label":null,"display_name":"编号001",
   "sim_key":"iccid:MOCKICCID001","sim_number":"+10000000001",
   "sim_number_auto":"+10000000001","sim_number_manual":null,
   "iccid":"MOCKICCID001","imsi":"MOCKIMSI001","imei":"MOCKIMEI001",
   "model":"A7670G Mock","manufacturer":"SIMCom","at_port":"MOCK1",
   "usb_path":"mock","status":"online","last_error":null,
   "first_seen_at":1791861818,"last_seen_at":1791949496,"updated_at":1791949496},
  {"id":3,"number":3,"code":"003","label":null,"display_name":"编号003",
   "sim_key":"iccid:898602D51524FA216310","sim_number":null,
   "sim_number_auto":null,"sim_number_manual":null,
   "iccid":"898602D51524FA216310","imsi":"460075525767326",
   "imei":"867284068161142","model":"A7670G-LLSE","manufacturer":"INCORPORATED",
   "at_port":"/dev/ttyUSB2",
   "usb_path":"/sys/devices/pci0000:00/0000:00:14.0/usb3/3-7",
   "status":"online","last_error":null,"first_seen_at":1791953567,
   "last_seen_at":1791958689,"updated_at":1791958689}
 ]
}
''';

void main() {
  test('device list from a live gateway parses', () {
    final data = jsonDecode(_payload) as Map<String, dynamic>;
    final raw = data['items'] as List;
    final devices = raw
        .map((e) => DeviceInfo.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
    expect(devices, hasLength(2));
    expect(devices[1].id, 3);
    expect(devices[1].model, 'A7670G-LLSE');
    expect(devices[1].isOnline, isTrue);
    expect(devices[1].simNumber, isNull);
  });

  test('the exact bytes served by /app/devices parse', () {
    final raw = File('test/fixtures/real_devices.json').readAsStringSync();
    final decoded = jsonDecode(raw);
    expect(decoded, isA<Map<String, dynamic>>());
    final data = decoded as Map<String, dynamic>;
    final items = data['items'] as List?;
    expect(items, isNotNull);
    final devices = (items ?? const [])
        .map((e) => DeviceInfo.fromJson(Map<String, dynamic>.from(e as Map)))
        .toList();
    // ignore: avoid_print
    print('parsed ' + devices.length.toString() + ' devices: ' +
        devices.map((d) => d.id.toString() + '/' + d.status + '/' + d.isOnline.toString()).join(' '));
    expect(devices, isNotEmpty);
    expect(devices.any((d) => d.isOnline), isTrue);
  });
}

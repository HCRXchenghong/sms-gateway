# VoiceSMS Flutter App

登录页只显示账号与密码。服务器地址编译进 App，不提供输入框或设置入口。

默认局域网地址：`http://192.168.31.110:8811`。

```sh
flutter test
dart analyze lib test
flutter build apk --release
# 正式部署时替换内置地址
flutter build apk --release --dart-define=SMSGW_SERVER_URL=https://gateway.example.com
adb install -r build/app/outputs/flutter-apk/app-release.apk
```

更新至不同部署地址时清除旧令牌，保留上次账号供重新登录。

短信列表点击会话即已读，长按可标为未读/已读、删除会话或批量选择。列表右上角菜单提供批量删除和删除已读。

设备选择尊重手动选择，离线设备不会自动替换为别的 SIM。选择“自动选择在线设备”恢复自动模式。

完整方案见 [商业化方案](../docs/COMMERCIAL_PLAN.md)，验收记录见 [真机联调](../docs/REAL_DEVICE_INTEGRATION.md)。

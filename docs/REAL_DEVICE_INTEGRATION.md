# 真机联调记录（A7670X + Flutter App）

本文记录在真实 SIM 卡与真实 A7670X 模块上完成的联调结果、复现步骤，以及固件层面的能力边界。
商业化方案见 [COMMERCIAL_PLAN.md](COMMERCIAL_PLAN.md)，接口说明见 [API.md](API.md)。

## 1. 联调环境

| 项目 | 值 |
| --- | --- |
| 模块型号 | SIMCom A7670G-LLSE（固件 A7670M7_V1.11.1） |
| IMEI | 867284068161142 |
| AT 口 | /dev/ttyUSB2（udev 规则 99-a7670x.rules 固定权限 0666） |
| SIM | ICCID 898602D51524FA216310，IMSI 460075525767326，状态 READY |
| 网络 | 中国移动 LTE，CEREG 0,1，CSQ 27~29（-57 dBm 左右） |
| 网关 | uvicorn app.main:app --port 8811，SMSGW_MOCK_MODEM=false |
| 手机 | Samsung SM-F9360（adb 序列号 RFCT81D80QR），App 为 release APK |
| 测试号 | 15331986287（用户本人授权用于联调） |

模块上电方式：该底板为自动开机，插入 USB 即完成枚举与注网，无需按 PWRKEY。
若换用其他底板，注意部分底板需要 PWRKEY 拉低 1~2 秒或 AUTO_POWER_ON 跳线。

## 2. 已验证链路

### 2.1 语音呼叫（App 发起）

在 Flutter App 拨号键盘输入 15331986287 并点击拨号：

1. 网关 `POST /app/voice/dial` 返回会话，状态机 dialing → alerting（`ringing_at` 落库）；
2. App 通话界面自动浮出，显示号码、"对方振铃中…"、"经由 编号003" 与提示音文案"请稍候，对方正在接听"；
3. 用户接听后状态转 active，通话 27 秒后由用户侧挂断；
4. 通话记录 `GET /app/voice/history` 落库：direction=outbound、answered_at 有值、duration=27、end_reason=remote_hangup；
5. 通话结束后 App 自动收回通话界面并回到拨号页，实时连接指示灯转绿。

模块 URC 时序（`VOICE CALL: BEGIN` / 推送 `+CLCC` / `+COLP` / `VOICE CALL: END: 000015` / `NO CARRIER`）
已由 `app/voice/parser.py` 与 `app/voice/manager.py` 解析，见提交 6d135b4。

### 2.2 短信（App 发起，中文）

在 App 短信会话页向 15331986287 发送中文内容，输入使用 ADBKeyboard 广播注入。
网关记录 direction=out、failed=false、device_id=3，用户手机实际收到该短信；
SIM 收件箱（10086、106586789 等真实下行短信）也同步出现在 App 会话列表中。

### 2.3 App 设备列表与状态

App 顶部设备选择器显示"编号003"，拨号页显示"通过 编号003 拨出"。
该问题曾表现为选择器常驻"无设备"：接口与解析均正常，但 AppScope 未转发子控制器的通知，
界面在首帧之后不再重绘。修复见 `mobile/lib/src/state/app_scope.dart`（通知转发合并到微任务），
回归测试 `mobile/test/app_scope_notify_test.dart`。

### 2.4 提示音与声卡

`/app/audio/prompts/dial_connecting/play` 在网关主机声卡播放成功（returncode 0），
声卡枚举接口返回主机全部 ALSA/Pulse 设备，App 与网页后台均可选择。

## 3. 固件能力边界（重要）

1. **模块无 TTS 语音库。** `AT+CTTS=1,"文本"` 全部返回 ERROR，`AT+CTTS=?` 仅回 OK。
   网关据此把 `tts_supported` 置为 false（`/app/devices/{id}/signal`），
   App 与网页的"播报"入口在该模块上会给出明确的能力提示而不是静默失败。
   通话中的语音播报只能走云端 TTS + 外部音频通路，见下条。
2. **模块未暴露 USB 声卡。** USB 配置中没有音频类接口（interface 1.3 缺失），
   主机侧看不到第二张声卡。因此"把云端 TTS 的音频送进通话链路"无法纯软件完成，
   需要硬件音频通路：带音频编解码的底板（MIC/SPK 或 I2S 接主机声卡），
   或在底板与主机之间加一张 USB 声卡做模拟环回。
   在硬件通路就绪前，产品上的降级策略是：通话中只播本地提示音，
   AI 语音回复以短信或 App/网页播放的形式交付。
3. **来电号码依赖网络下发。** 该 SIM 未开通来电显示补充业务时 `sim_number` 为空，
   网关以 ICCID 作为 SIM 主键，展示名用设备编号，不影响收发与呼叫。

## 4. 复现步骤

```bash
# 网关（真实模块）
cd sms-gateway && SMSGW_MOCK_MODEM=false .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8811

# App 安装与登录（手机已授权 USB 调试）
adb install -r mobile/build/app/outputs/flutter-apk/app-release.apk
adb shell am start -n com.voicesms.voicesms_app/.MainActivity

# 中文输入（已装 ADBKeyboard 并设为默认输入法）
adb shell "am broadcast -a ADB_INPUT_TEXT --es msg '要输入的中文'"

# 截图核对
adb shell screencap -p /sdcard/gw.png && adb pull /sdcard/gw.png /tmp/gw.png
```

注意：`adb exec-out screencap -p` 经管道重定向会损坏 PNG，务必先写到 /sdcard 再 pull。

## 5. 自动化回归

| 套件 | 命令 | 结果 |
| --- | --- | --- |
| 后端单测/接口 | `.venv/bin/python -m pytest -q` | 76 passed |
| Flutter 单元/组件 | `flutter test` | 35 passed |
| 静态检查 | `dart analyze lib test` | 0 error / 0 warning（仅存量 info 级风格提示） |
| 真机联调 | 本文第 2 节 | 呼叫 27s 接通后挂断、中文短信送达 |

mock 模式的端到端套件（`tests/test_live_integration.py`）与网页控制台 jsdom 套件
在每次改动后端或 `app/static` 时运行；本次改动只涉及移动端与设备对账逻辑，由上述套件覆盖。

## 6. 待用户提供

- AI 供应商 API Key（OpenAI 兼容 / 阿里云 / 讯飞均可），配置入口：网页后台"AI 设置"与 App"AI 设置"。
  未配置时所有 AI 功能自动落到离线 mock，保证可演示可测试。
- 若需要通话内云端 TTS，请确认底板音频通路方案（见第 3 节第 2 条）。

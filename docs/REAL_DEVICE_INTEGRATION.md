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

## 7. 通话录音链路（软件侧已完成）

录音编排独立在 `app/voice/recorder.py`：通话进入 active 状态时对选中的输入声卡
启动采集（arecord / parecord / pw-record 按后端自动选择），挂断时停止并把文件路径
写回 CDR 的 recording_path。文件落在 `SMSGW_AUDIO_RECORD_DIR`（默认 /var/lib/sms-gateway/recordings），以 call_id 命名。

- 开关：`SMSGW_AUDIO_RECORD_ENABLED` 设置启动默认值；网页后台的 audio.record_enabled 可运行时覆盖，无需重启。
- 提示：启用本地提示音时，录音开始会播放 recording_notice（"本次通话可能被录音"）；远端可听性待音频接线联调。
- 取回：`GET /app/voice/calls/{call_id}/recording` 流式返回 WAV；网页后台通话记录新增"录音"列可新窗口播放；App 通话记录显示红色录音标记。
- 实时：WebSocket 推送 call.recording（started / stopped / empty），供控制台显示 REC 标记。
- 现状：录音测试 5 项通过，包含模拟通话状态→PCM 采集替身→CDR 归档→鉴权下载，以及目录越界拒绝。
  已修复接通状态没有普通提示音时跳过录音启动的问题；真实声卡采集与双向语音质量仍待接线验收。
  通话音频通路等板子音频口接服务器声卡后联调（计划近日内），届时验证真实通话的录音文件与回放。

## 8. App 内置服务器地址

服务器地址编译进 App（`mobile/lib/src/config/server_config.dart` 的 builtInServerUrl），
登录页不显示服务器地址或编辑入口，只显示账号和密码（需要双因素认证时再显示验证码）。
登录和冷启动恢复都使用内置地址；升级后如部署地址改变，会清掉旧令牌并重新登录，避免把旧令牌发送到另一台服务器。
当前默认是局域网网关 http://192.168.31.110:8811；
后续切正式服务器时改这一处默认值，或打包时传 `--dart-define=SMSGW_SERVER_URL=https://你的域名` 覆盖。

## 9. 设备切换与短信管理验收（2026-10-14）

### 交付行为

- 服务器地址完全内置，登录页仅账号、密码；部署地址改变后不复用旧网关令牌。
- 手动选择编号 001/002 会显示离线并保留选择，重启仍有效；自动选择会切回在线的编号 003。
- 离线拨号显示“编号002 已离线，请切换在线设备”，不会改用另一个 SIM 发起呼叫。
- 短信点开即已读；长按会话可标未读/已读、删除、进入批量选择；右上角菜单可批量删除和删除已读。
- 删除已读保留未读会话、草稿和正在发送的短信。选中正在发送的会话做批量删除时返回 409，整批不删除。
- 网页“短信会话”提供相同的读/未读、单会话删除、全选/批量删除、删除已读；共享 WebSocket 通知与审计日志。
- 会话按号码合并所有设备的记录，当前为网关共享收件箱，所有登录账号共享读/未读状态与删除结果。

### 接口

| 方法 | 地址 | 用途 |
| --- | --- | --- |
| GET | `/app/conversations/{peer}/messages` | 打开会话并标读；后台刷新传 `mark_read=false` |
| POST | `/app/conversations/{peer}/read` | 标为已读 |
| POST | `/app/conversations/{peer}/unread` | 标为未读 |
| DELETE | `/app/conversations/{peer}` | 删除会话全部记录 |
| POST | `/app/conversations/bulk_delete` | JSON `{"peers":["号码1","号码2"]}`，最多 500 个 |
| POST | `/app/conversations/delete_read` | 清理符合条件的已读会话 |

### 自动化与真机结果

| 检查 | 结果 |
| --- | --- |
| 后端全量 pytest | 83 项通过；随后修正录音接线并增加 1 项集成测试 |
| 录音修正后的通话/录音回归 | 31 项通过（包含上述新增录音集成测试） |
| Flutter 全量 | 39 项通过；离线提示文案更新后短信/设备 3 项回归通过 |
| Dart 静态分析 | 0 error / 0 warning；存量风格 info 保留 |
| 原网页控制台 DOM 联调 | 51 项通过，包含账号管理、声卡与通话控制 |
| 新增网页短信 DOM 联调 | 已读、未读、删除已读保护、取消删除、批量删除、计数和实时刷新通过 |
| 三星 SM-F9360 真机 | 内置地址登录、001/002 切换、选择持久化、自动回到 003、离线拨号提示通过 |
| 三星短信真机 | 点开已读、长按未读、长按删除、删除已读保护、全选批量删除均通过，并核对服务端数据 |

删除实测使用独立模拟网关（8812）及专用测试短信；测试结束已替换回内置 `192.168.31.110:8811` 的正式局域网包并登录真实网关。原网关短信未参与删除测试。手机已恢复自动选择在线设备，输入法已恢复搜狗。

截图：[登录页](evidence/2026-10-14/login.png)、[编号001](evidence/2026-10-14/device-001.png)、[编号002](evidence/2026-10-14/device-002.png)、[离线拨号提示](evidence/2026-10-14/offline-dial.png)、[短信长按](evidence/2026-10-14/sms-long-press.png)、[短信多选](evidence/2026-10-14/sms-multi-select.png)。

APK：`mobile/build/app/outputs/flutter-apk/app-release.apk`（已通过 adb 覆盖安装）。

SHA-256：`31107fc12e3f3a739df3775202591af6a5d035e2682390cfb590ca2bf68f3ede`。

复现新增网页测试（必须指定独立模拟网关，测试会清理其中的短信）：

```sh
npm ci --prefix tests/web
SMSGW_URL=http://127.0.0.1:8812 npm --prefix tests/web run test:sms
```

音频线尚未连接，因此真实双向语音、实时录音与 AI 生产服务的最终联调仍待后续，不计为已通过。

## 2026-10-15 AI 代打与实时翻译进入 App 和网页控制台

### 本轮改动

- App 新增「AI 代打」标签页：任务队列按全部 / 进行中 / 已结束 / 未接通过滤，可新建外呼或待接入任务；任务详情显示状态、网关结论、纪要（关键事实、约定、待办、未确认与达成依据）和通话内容；未结束的任务可取消，取消会等线路真正挂断。
- App 通话页把原来的聊天式 AI 应答换成通话能力：翻译分关 / 字幕 / 语音三档，通话中直接显示实时字幕条；输入的文字翻译后说给对方；AI 接管需先写清目的，随时可以接管回来，通话不中断；一键生成通话纪要。
- App 底部导航改成三星原生电话的风格：细线图标加小标签，选中加粗变黑，没有指示条；「AI 助手」聊天页下线，位置交给「AI 代打」。
- 网页控制台拨号页新增「通话助手」面板：选择通话后可切换翻译模式、看实时字幕、把文字说给对方、AI 接管与接管回来、生成纪要。AI 语音页新增「通话策略」表单（来电处理、翻译默认值、录音策略、文字记录与纪要开关）和「AI 代打任务」面板（创建、列表、取消、查看纪要）。

### 真机与自动化结果

| 检查 | 结果 |
| --- | --- |
| 后端全量 pytest | 215 项通过 |
| Dart 静态分析 | 0 error / 0 warning，存量风格 info 保留 |
| release APK | 重新打包并经 adb 覆盖安装到三星 SM-F9360 |
| 真机登录 | 内置地址 192.168.31.110:8811 登录真实网关通过 |
| 真机 UI 走查 | 新底部导航、AI 代打空态、新建对话框、创建待接入任务、任务详情、取消任务全部通过 |

截图：[新底部导航](evidence/2026-10-15/nav-samsung-style.png)、[AI 代打标签页](evidence/2026-10-15/ai-calls-tab.png)、[新建任务对话框](evidence/2026-10-15/ai-task-dialog.png)、[任务详情](evidence/2026-10-15/ai-task-detail.png)、[取消后](evidence/2026-10-15/ai-task-cancelled.png)。

音频线仍未接到模块音频口，真实双向语音与翻译听感的最终联调依旧待后续，不计为已通过。

# A7670G 模块 + 本项目完整参考手册

> 本文是 **SIMCom A7670G 4G USB Dongle** 配合本项目（SMS Gateway）的**自用参考手册**。
>
> **关于权威性**：本文涉及"AT 命令的存在与作用"的部分，**信息源**为 SIMCom 公开发布的 *A76XX Series AT Command Manual*（V1.06–V1.09，可在 https://www.simcom.com/ 官网"Documents Download"或 "ask.simcom.com" 公开下载）；涉及 **PDU 协议、CMS/CME 错误码** 的部分，**信息源**为 3GPP 公开规范 *TS 23.040*、*TS 27.005*、*TS 23.038*、*TS 27.007*（在 https://www.3gpp.org/ 公开下载）。本文每个段落都会注明来源章节。
>
> **本文不重新分发**版权资料。本项目实际发送的 AT 指令、解析的数据、参数取值，是从代码（`app/modem.py`、`app/pdu.py`）反推出来的事实，与上述公开规范一致。
>
> **可信度声明**：
> - 标 ✅ 的内容已在本项目代码或本机测试中验证
> - 标 📖 的内容是依据公开规范的事实陈述
> - 标 ⚠️ 的内容是经验/最佳实践，不是规范性强制
> - 任何项目实际行为以最终代码为准；任何 AT 命令完整定义以 SIMCom 官方手册为准

---

## 目录

1. [硬件信息](#1-硬件信息)
2. [USB 端口与驱动（Linux）](#2-usb-端口与驱动linux)
3. [本项目使用到的 AT 命令清单](#3-本项目使用到的-at-命令清单)
4. [PDU 协议（短信编码）](#4-pdu-协议短信编码)
5. [错误码](#5-错误码)
6. [项目架构](#6-项目架构)
7. [HTTP API 与 Web 路由](#7-http-api-与-web-路由)
8. [数据库 schema](#8-数据库-schema)
9. [安全机制](#9-安全机制)
10. [部署流程](#10-部署流程)
11. [故障排查](#11-故障排查)
12. [官方资料索引](#12-官方资料索引)

---

## 1. 硬件信息

### 1.1 A7670G 简介 📖

A7670G 属于 SIMCom A7670 系列，4G LTE Cat-1 模组。"G" 后缀代表 Global（全球版）频段组合。模组对外暴露 USB 接口，PC/SBC 通过 USB 即可获得多路串口（AT、PCM、Trace 等）和一路网卡（NDIS / RNDIS）。

来源：SIMCom *A7670 Series Datasheet*。

### 1.2 频段（典型值，以你拿到的型号铭牌为准）📖

A7670G 全球版常见频段：
- **LTE FDD**：B1/B2/B3/B4/B5/B7/B8/B12/B13/B18/B19/B20/B25/B26/B28/B66
- **LTE TDD**：B34/B38/B39/B40/B41
- **WCDMA**：B1/B2/B4/B5/B8
- **GSM**：850/900/1800/1900 MHz

具体支持的频段以你那块板子模组上的丝印型号 + SIMCom 官方 datasheet 为准。

### 1.3 USB Dongle 板载常见组件 ✅

根据 USB DONGLE 板硬件原理图（项目根目录 `4G USB DONGLE A7670X/USB DONGLE 4G网卡树莓派和电脑免驱入网资料V4.0/3.原理图（尺寸图）/USB DONGLE 4G 原理图和尺寸.pdf` 中可查）：

- USB-A 公头（直插主机或线缆）
- A7670G 主芯片
- Nano-SIM 卡槽
- 主天线 + GNSS 副天线接口（IPEX）
- 板载 LED：电源 / 网络状态

> **本项目不依赖 GNSS、不依赖 PCM 音频、不开拨号上网功能**。你只要保证：电源 OK、SIM 已插入、主天线接好、USB 已识别成 ttyUSB，即可用本项目收发短信。

---

## 2. USB 端口与驱动（Linux）

### 2.1 USB 标识 ✅📖

A7670G 的 USB 设备 VID:PID 在不同固件版本下出现过多组组合，本项目脚本同时认这几个：

| VID:PID    | 备注                                       |
| ---------- | ------------------------------------------ |
| 1e0e:9001  | 常见组合 1                                 |
| 1e0e:9011  | 常见组合 2                                 |
| 1e0e:9018  | 常见组合 3                                 |

> 如果你的模组不在这三个里，把 `lsusb` 输出贴给我，我加进 `install.sh` 顶部的 `A7670G_USB_IDS` 数组就行。

### 2.2 ttyUSB 端口分工 📖

A7670 系列模组通过 USB 通常暴露 **5 个 ttyUSB 接口**（具体序号取决于内核加载顺序和 udev 规则）：

| 序号示例   | 用途                                       | 本项目是否使用 |
| ---------- | ------------------------------------------ | -------------- |
| ttyUSB0    | DIAG / Trace（调试日志）                   | ❌             |
| ttyUSB1    | NMEA（GNSS 数据）                          | ❌             |
| ttyUSB2    | **AT 主端口**（Modem 控制）                | ✅             |
| ttyUSB3    | AT 辅端口（与主端口一致，可同时连）        | 可选          |
| ttyUSB4    | NDIS 数据通道（RNDIS / PPP 拨号上网用）   | ❌             |

> ⚠️ 不要假设 AT 端口固定是 ttyUSB2。本项目代码在启动时会**自动探测**：把 `AT\r\n` 发到每个 ttyUSB，等待 2 秒，谁回 `OK` 谁就是 AT 端口。代码见 `app/modem.py` 的 `_find_at_port` 与 `_probe`。

### 2.3 Linux 驱动如何工作 ✅📖

**Ubuntu 22.04（内核 5.15+）开箱即用**，无需编译厂商提供的源码包。原理：

1. **`option` 模块**（Linux 内核自带）—— 支持 USB 蜂窝模组的串口驱动。Ubuntu 22.04 默认编译进内核或作为模块包含。
2. 当 USB 设备插入，内核遍历已注册的 USB 设备 ID 表；如果新设备的 VID:PID 不在表里，**option 不会自动绑定**，因此 ttyUSB 不会出现。
3. 解决方法：把新 VID:PID 写到 `/sys/bus/usb-serial/drivers/option1/new_id` —— option 立即接管该设备并创建 ttyUSBx。
4. 写入是**临时**的，重启后失效。所以还要写一条 udev 规则在每次插拔时自动绑定。

本项目 `install.sh` 自动完成这两步。

### 2.4 USB Mass-Storage 模式（CD-ROM 模式）兜底 ⚠️

部分 A7670 模组首次插入时会先以 USB Mass Storage / CD-ROM 模式枚举（厂商内置了一个 ZeroCD 镜像让 Windows 自动安装驱动）。Linux 上看到的不是 ttyUSB 而是 `sr0` 或 `sda`。

`install.sh` 检测到这种情况会调用 `usb-modeswitch -v <VID> -p <PID> -J` 切换模式。`-J` 选项专门针对 SIMCom/Huawei 这类设备。

如果切换失败，可手工：

```bash
sudo apt-get install -y usb-modeswitch
sudo usb_modeswitch -v 0x1e0e -p 0x9001 -J
```

### 2.5 udev 规则（项目自动写入）✅

`install.sh` 会写 `/etc/udev/rules.d/99-a7670g.rules`，等价于：

```
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="1e0e", ATTRS{idProduct}=="9001", \
    RUN+="/sbin/modprobe option", \
    RUN+="/bin/sh -c '/bin/echo 1e0e 9001 > /sys/bus/usb-serial/drivers/option1/new_id'"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1e0e", ATTRS{idProduct}=="9001", GROUP="smsgw", MODE="0660"
```

作用：
- 插入后自动加载 option 并绑定
- ttyUSB 设备的 owner 设为 `smsgw` 服务用户，权限 `0660`

不会因为重启或拔插而失效。

### 2.6 拔插自愈（项目代码层）✅

代码层在 `app/modem.py::Modem.run()` 里做了 reconnect 循环：

- 串口任何异常（`OSError` / `Exception`）→ 关闭 → 等 2~30 秒指数退避 → 重新探测端口 → 重连
- WebSocket 客户端在 topbar 看到的"在线 / 离线"指示就是这个状态的反映

---

## 3. 本项目使用到的 AT 命令清单

> **来源**：SIMCom *A76XX Series AT Command Manual*（V1.06+），章节号以 V1.09 为准（其它版本章节号略有差异）。
>
> **本节写法**：每条命令给你"项目里实际怎么发""为什么这么发""关键参数说明"。**完整语法、所有参数取值范围、所有 URC 上报形式、厂商特有扩展**，请去 SIMCom 官方手册对应章节查。

### 3.1 短信收发（核心）

#### `AT+CMGF` —— 设置短信格式 📖

- **手册章节**：9.2.3
- **本项目发送**：`AT+CMGF=0`
- **参数 `0`**：PDU 模式。所有短信内容以十六进制 PDU 字节流交互。
- **参数 `1`**：Text 模式。仅支持 GSM 7-bit 字符集，**不支持中文**。
- **为什么本项目用 0**：要支持中文 / Emoji / 长短信，必须 PDU 模式 + UCS-2 编码。Text 模式发不了中文。

#### `AT+CPMS` —— 设置短信存储区 📖

- **手册章节**：9.2.2
- **本项目发送**：`AT+CPMS="SM","SM","SM"`
- **三个参数**：分别是"读/删用的存储区"、"写/发用的存储区"、"接收上报来的短信落到哪个存储区"
- **`"SM"`**：SIM 卡上的短信存储
- **`"ME"`**：模组内置的存储
- **为什么用 SM**：SIM 卡存储兼容性最好，所有运营商网络都不会拒绝；项目逻辑读出立即删除（见 `AT+CMGD`），不会被存储满拖累

#### `AT+CNMI` —— 新短信 URC 上报方式 📖

- **手册章节**：9.2.9
- **本项目发送**：`AT+CNMI=2,1,0,0,0`
- **5 个参数依次含义**：
  - `mode=2` —— 新短信通知缓冲后立刻转发到 TE（终端，即我们的程序）
  - `mt=1` —— 新短信存储到 SIM/ME 后，主动以 URC `+CMTI: "<mem>",<idx>` 的形式上报存储位置
  - `bm=0` —— 不上报小区广播
  - `ds=0` —— 不上报状态报告（送达回执）
  - `bfr=0` —— 切到 mode=2 时清空缓冲区
- **效果**：模组收到短信后，会主动往串口发一行 `+CMTI: "SM",3`（数字是 SIM 上的存储索引）。我们的代码看到这个 URC 立即触发 `AT+CMGR=3` 读出来。

#### `+CMTI` —— 新短信 URC（接收事件） 📖

- **手册章节**：9.2.9（与 `+CNMI` 同节）
- **形态**：`+CMTI: "SM",<index>`
- **本项目处理**：`app/modem.py::_read_loop` 解析出 `<index>` 后，调用 `_fetch_index(idx)` → `AT+CMGR=<idx>` 拉取 PDU

#### `AT+CMGR` —— 读取一条短信 📖

- **手册章节**：9.2.12
- **本项目发送**：`AT+CMGR=<idx>`，其中 `<idx>` 来自上一步 `+CMTI`
- **响应形态**（PDU 模式）：
  ```
  +CMGR: <stat>,<alpha>,<length>
  <pdu_hex>
  OK
  ```
- **`<stat>`**：消息状态（0=未读 REC UNREAD，1=已读 REC READ，等）
- **`<pdu_hex>`**：完整的 SMS-DELIVER PDU 十六进制字符串。本项目把这一行喂给 `app/pdu.py::decode_deliver` 解码

#### `AT+CMGS` —— 发送短信 📖

- **手册章节**：9.2.13
- **本项目发送**（PDU 模式）：
  ```
  AT+CMGS=<tpdu_length>\r        ← 第一行，<tpdu_length> 是 TPDU 字节数（不含 SMSC）
                                   等待模组返回 "> " 提示符
  <pdu_hex><CTRL-Z>              ← 直接把整个 PDU（含 SMSC=00 前缀）的十六进制发上去，结尾是 0x1A
  ```
- **响应**：`+CMGS: <mr>` 后跟 `OK`，`<mr>` 是消息引用号（0..255）
- **关键约束**：`<tpdu_length>` 必须**精确**等于 TPDU（First-octet 起到 UD 末尾）的字节数，**不算 SMSC 头**。本项目 `app/pdu.py::OutgoingSegment.tpdu_length_octets` 自动计算
- **超时**：本项目设 60 秒（`SMSGW_MODEM_SEND_TIMEOUT_SECONDS`）

#### `AT+CMGD` —— 删除短信 📖

- **手册章节**：9.2.16
- **本项目发送**：`AT+CMGD=<idx>`，`<idx>` 来自刚刚 CMGR 的位置
- **效果**：从 SIM 删除该位置的短信，避免存储满
- **可选参数 2**：`AT+CMGD=1,4` 表示一次性删除所有短信（本项目没用，因为是逐条处理）

#### `AT+CMGL` —— 列出短信 📖

- **手册章节**：9.2.11
- **本项目发送**：`AT+CMGL=4`
- **参数 `4`**：列出所有状态的短信（包括未读、已读、未发送、已发送）。PDU 模式下数字而非字符串
- **何时使用**：服务**启动**时调用一次（`Modem._scan_inbox`），把模组离线时收到的短信取走

#### `AT+CSCA` —— 短信中心号码 📖

- **手册章节**：9.2.4
- **本项目**：**不显式设置**。SIM 卡里默认带有运营商的 SMSC 号码，本项目发 PDU 时 SMSC 字节填 `00`（让模组使用 SIM 默认的 SMSC）
- **手动查询**：`AT+CSCA?` 会返回当前用的中心号码

### 3.2 配置类

#### `ATE0` —— 关闭命令回显 📖

- **手册章节**：2.2.7
- **本项目发送**：`ATE0`
- **作用**：发送 `AT...\r` 后，模组不再回显这一行，只返回结果，方便程序解析

#### `AT+CMEE` —— 错误码详细程度 📖

- **手册章节**：3.2.11
- **本项目发送**：`AT+CMEE=2`
- **取值**：
  - `0`：仅 `ERROR`
  - `1`：`+CME ERROR: <数字码>`
  - `2`：`+CME ERROR: <文字描述>`（**本项目用此**，便于排错）

### 3.3 信号 / 网络状态（项目暂未在代码里查询，但故障排查会用）

下列命令本项目运行时不主动发，但出问题时你可以用 `minicom` / `picocom` 手动连 ttyUSB 调试：

| 命令         | 章节   | 用途                                                                 |
| ------------ | ------ | -------------------------------------------------------------------- |
| `AT+CSQ`     | 3.2.2  | 查询信号强度。返回 `+CSQ: <rssi>,<ber>`，rssi 0-31，>= 10 就够发短信 |
| `AT+CPSI?`   | 4.2.10 | 查询当前服务详情：网络制式、运营商、Cell ID、TAC                     |
| `AT+CREG?`   | 4.2.1  | GSM/2G 注册状态                                                      |
| `AT+CGREG?`  | 5.2.1  | GPRS/3G 注册状态                                                     |
| `AT+CEREG?`  | 5.2.2  | LTE 注册状态（最常用）                                               |
| `AT+CIMI`    | 6.2.5  | SIM 卡 IMSI                                                          |
| `AT+CCID`    | 6.2.6  | SIM 卡 ICCID                                                         |
| `AT+CGMM`    | 2.2.18 | 模组型号                                                             |
| `AT+CGMR`    | 2.2.19 | 固件版本                                                             |
| `AT+CGSN`    | 2.2.20 | IMEI                                                                 |
| `AT+CFUN=1`  | 3.2.1  | 全功能模式（默认）                                                  |
| `AT+CFUN=4`  | 3.2.1  | 飞行模式（射频关闭，省电）                                          |
| `AT+CFUN=1,1`| 3.2.1  | 软重启模组                                                          |

---

## 4. PDU 协议（短信编码）

> **信息源**：3GPP TS 23.040（SMS Realization）、TS 23.038（SMS Alphabet/DCS）、TS 27.005（DTE-DCE Interface for SMS）。这些是**3GPP 公开规范**，可在 https://www.3gpp.org/ 任意下载。

### 4.1 为什么用 PDU 模式而不是 Text 模式 📖

- Text 模式（`AT+CMGF=1`）只能发 GSM 7-bit 默认字符集——拉丁字母、数字、若干符号。中文、日文、emoji 都发不了。
- PDU 模式（`AT+CMGF=0`）以 8-bit 字节流方式交互，数据编码（Data Coding Scheme，DCS）可选 GSM 7-bit / 8-bit binary / **UCS-2 (UTF-16BE)**。UCS-2 可以承载任意 Unicode 字符。
- 中文、emoji、繁体、日文必须用 UCS-2 → 必须 PDU 模式。

### 4.2 SMS-DELIVER PDU 结构（**接收**方向） 📖

模组通过 `AT+CMGR` 给我们的是 SMS-DELIVER 格式（网络 → 手机）：

```
+--------+------+------+-----+------+-----+----+------+
| SMSC   | First|  OA  | PID | DCS  | SCTS| UDL| UD   |
| header | Octet|      |     |      |     |    |      |
+--------+------+------+-----+------+-----+----+------+
   变长     1    变长    1     1      7    1   变长
```

字段含义：
- **SMSC**（Service Centre Address）：短信中心号码段。第 1 字节是后续长度，第 2 字节是 Type-of-Address，再后是 BCD 编码的号码。可能为 0（即仅一个 `00` 字节，表示无 SMSC，或 SIM 默认）。
- **First Octet**：8 个标志位组成
  - bit0-1：MTI（Message Type Indicator）= `00` 表示 SMS-DELIVER
  - bit2：MMS（More Messages to Send）
  - bit5：SRI（Status Report Indication）
  - bit6：**UDHI**（User Data Header Indicator）—— 1 表示 UD 字段开头有 UDH，**长短信拼接靠它**
  - bit7：RP（Reply Path）
- **OA**（Originator Address）：发送方号码。第 1 字节 = 数字位数，第 2 字节 = TOA，后接 BCD 半字节交换的号码
- **PID**（Protocol Identifier）：通常 `00`
- **DCS**（Data Coding Scheme）：编码标识
  - `00` —— GSM 7-bit
  - `04` —— 8-bit binary
  - `08` —— **UCS-2**（UTF-16BE）
- **SCTS**（Service Centre Time Stamp）：7 字节时间戳，每个字节都是 nibble-swapped BCD（年月日时分秒，第 7 字节是时区，单位 1/4 小时）
- **UDL**（User Data Length）：
  - 7-bit 编码时单位是 **septet 数**（7-bit 字符数）
  - 8-bit / UCS-2 编码时单位是 **octet 数**（字节数）
- **UD**（User Data）：实际内容。如果 UDHI=1，前缀有 UDH（User Data Header）

### 4.3 UDH（长短信拼接头） 📖

单条短信容量上限：
- GSM 7-bit：160 字符
- UCS-2：70 字符

超长内容要切分成多段，每段加 6 字节 UDH（8-bit reference）或 7 字节 UDH（16-bit reference）：

```
6-byte UDH（8-bit ref，本项目用此）：
+-----+-----+-----+-----+-------+-----+
|UDHL |IEI  |IEDL |REF  |TOTAL  |SEQ  |
+-----+-----+-----+-----+-------+-----+
| 05  | 00  | 03  | xx  |  yy   | zz  |
```

- `UDHL=0x05`：UDH 长度（不含自己）共 5 字节
- `IEI=0x00`：Information Element Identifier = 8-bit 拼接
- `IEDL=0x03`：本 IE 数据长度
- `REF`：本组短信的引用号（同一组短信此值相同，0..255）
- `TOTAL`：本组共几段
- `SEQ`：本段是第几段（1-based）

加 UDH 后，每段可用容量减少：
- GSM 7-bit：160 - 7 = 153 字符（UDH 6 字节占 7 个 septet 位）
- UCS-2：(140 - 6) / 2 = **67 字符 / 段**

本项目 `app/pdu.py::encode_submit_ucs2` 实现了 UCS-2 单段（最多 70 字）+ 多段拼接（每段 67 字）的自动切分。

### 4.4 UCS-2 编码 📖

UCS-2 = UTF-16 Big Endian，**每个码点 2 字节**。Python 里就是 `text.encode("utf-16-be")`。

注意：
- BMP 平面外的 emoji（如 🌟 U+1F31F）需要 4 字节代理对（surrogate pair）。本项目编码端按字节流切分，但每段切分点对齐到 2 字节边界，避免把代理对劈开（`app/pdu.py::encode_submit_ucs2` 中 `if len(chunk) % 2 == 1` 的处理）。
- 解码端用 `payload.decode("utf-16-be", errors="replace")`，遇到无效字节用 `\ufffd` 替代。

### 4.5 时间戳（SCTS）解码 📖

SCTS 7 字节，每字节是 BCD 但**两个 nibble 交换**：

```
原始字节 0x32 → 实际值 23（即 32 反过来读）
```

第 7 字节是时区，单位 1/4 小时：
- bit3 of 低 nibble 是符号位（1=负）
- 其余按 BCD 解码

例如时区字节 `0x32` → 32 个 1/4 小时 = 8 小时（北京时区 UTC+8）。

本项目实现见 `app/pdu.py::_decode_scts`，并对异常时区做容错（fallback 到当前 UTC 时间）。

### 4.6 SMS-SUBMIT PDU 结构（**发送**方向） 📖

我们要构造给 `AT+CMGS` 的是 SMS-SUBMIT 格式（手机 → 网络）：

```
+--------+------+----+------+-----+------+-----+----+------+
| SMSC   | First| MR |  DA  | PID | DCS  | VP  | UDL| UD   |
| header | Octet|    |      |     |      |     |    |      |
+--------+------+----+------+-----+------+-----+----+------+
```

差别于 DELIVER：
- **First Octet bit0-1**：MTI = `01`（SUBMIT）
- 多了 **MR**（Message Reference）：本机发送计数（让我们填 0 让模组自管，或我们自己递增）
- 没有 SCTS，多了 **VP**（Validity Period）：消息有效期。本项目固定填 `0xAA` = 4 天
- **DA**（Destination Address）：接收方号码

本项目发送时 SMSC 填 `00`（让模组用 SIM 默认 SMSC）。

---

## 5. 错误码

### 5.1 `+CMS ERROR`（短信特定错误） 📖

来源：3GPP TS 27.005 + TS 24.011。本项目把数字直接显示给用户，常见值：

| 数字 | 含义                                                  | 排查方向                       |
| ---- | ----------------------------------------------------- | ------------------------------ |
| 1    | Unassigned (unallocated) number                       | 收件号码不存在                 |
| 8    | Operator determined barring                           | 运营商屏蔽                     |
| 10   | Call barred                                           | 卡或号码被禁                   |
| 21   | Short message transfer rejected                       | 网络拒绝                       |
| 27   | Destination out of service                            | 接收方关机或脱网               |
| 28   | Unidentified subscriber                               | 接收方号码未识别               |
| 29   | Facility rejected                                     | 服务被拒                       |
| 30   | Unknown subscriber                                    | 未知用户                       |
| 38   | Network out of order                                  | **网络故障**（最常见）         |
| 41   | Temporary failure                                     | 临时故障，稍后重试             |
| 42   | Congestion                                            | 网络拥塞                       |
| 47   | Resources unavailable, unspecified                    | 资源不可用                     |
| 50   | Requested facility not subscribed                     | **未开通短信功能**             |
| 69   | Requested facility not implemented                    | 不支持的特性                   |
| 81   | Invalid short message transfer reference value        | 引用号无效                     |
| 95   | Semantically incorrect message                        | 消息内容有误                   |
| 96   | Invalid mandatory information                         | 关键字段缺失                   |
| 97   | Message type non-existent or not implemented          | 消息类型错                     |
| 127  | Interworking, unspecified                             | 跨网未知错误                   |
| 300  | ME failure                                            | 模组故障                       |
| 301  | SMS service of ME reserved                            | 短信服务保留                   |
| 302  | Operation not allowed                                 | 操作不允许                     |
| 303  | Operation not supported                               | 操作不支持                     |
| 304  | Invalid PDU mode parameter                            | **PDU 格式错**（自查 PDU 编码）|
| 305  | Invalid text mode parameter                           | text 模式参数错                |
| 310  | SIM not inserted                                      | **没插 SIM**                   |
| 311  | SIM PIN required                                      | SIM 锁了 PIN                   |
| 313  | SIM failure                                           | SIM 卡硬件故障                 |
| 314  | SIM busy                                              | SIM 忙                         |
| 315  | SIM wrong                                             | SIM 错误                       |
| 320  | Memory failure                                        | 存储故障                       |
| 321  | Invalid memory index                                  | 索引错                         |
| 322  | Memory full                                           | **存储满**（项目应已自动删除） |
| 330  | SMSC address unknown                                  | **SMSC 号码未配置**            |
| 331  | No network service                                    | **无网络服务**（信号或注册问题）|
| 332  | Network timeout                                       | 网络超时                       |
| 500  | Unknown error                                         | 通用未知错误                   |

### 5.2 `+CME ERROR`（设备/SIM 错误） 📖

来源：3GPP TS 27.007。常见：

| 数字 | 含义                              |
| ---- | --------------------------------- |
| 0    | Phone failure                     |
| 3    | Operation not allowed             |
| 4    | Operation not supported           |
| 10   | SIM not inserted                  |
| 11   | SIM PIN required                  |
| 13   | SIM failure                       |
| 14   | SIM busy                          |
| 15   | SIM wrong                         |
| 16   | Incorrect password                |
| 17   | SIM PIN2 required                 |
| 18   | SIM PUK2 required                 |
| 20   | Memory full                       |
| 21   | Invalid index                     |
| 22   | Not found                         |
| 23   | Memory failure                    |
| 30   | No network service                |
| 31   | Network timeout                   |
| 32   | Network not allowed (emergency only) |
| 100  | Unknown                           |

> ⚠️ 厂商可能扩展额外错误码。项目已设 `AT+CMEE=2`，错误以**文字**形式返回，看到错误信息直接 Google 即可。

---

## 6. 项目架构

### 6.1 进程模型 ✅

单进程多协程：

```
┌──────────────────────────────────────────────────────┐
│ uvicorn (asyncio event loop)                         │
│                                                      │
│  ┌─────────────────┐  ┌────────────────────────────┐ │
│  │ HTTP / WS routes│  │ Modem coroutine            │ │
│  │ (FastAPI)       │  │ (app/modem.py)             │ │
│  │                 │  │                            │ │
│  │ - /setup /login │  │ - find AT port             │ │
│  │ - /app          │  │ - configure modem          │ │
│  │ - /api/v1/...   │  │ - read +CMTI URC           │ │
│  │ - /ws           │  │ - decode PDU               │ │
│  └────────┬────────┘  └──────────┬─────────────────┘ │
│           │                      │                   │
│           ↓                      ↓                   │
│      ┌──────────────────────────────┐                │
│      │ asyncio.to_thread(...)        │                │
│      │ → SQLite + bcrypt + crypto    │                │
│      └──────────────────────────────┘                │
│                                                      │
│      ┌──────────────────────────────┐                │
│      │ Hub (in-process pubsub)       │                │
│      │ → fan out to all WS clients   │                │
│      └──────────────────────────────┘                │
└──────────────────────────────────────────────────────┘
```

### 6.2 文件职责 ✅

| 文件                          | 职责                                                                   |
| ----------------------------- | ---------------------------------------------------------------------- |
| `app/main.py`                 | FastAPI 入口、生命周期、日志配置、modem 协程启动                       |
| `app/config.py`               | 配置（pydantic-settings，从 env / `.env` 读）                          |
| `app/db.py`                   | SQLite schema + connection 工厂（WAL 模式）                            |
| `app/repo.py`                 | 业务数据访问层（用户、Key、收件箱、发件箱、审计）                      |
| `app/security.py`             | bcrypt 密码、TOTP、API Key 哈希、AES-GCM 加密 TOTP secret              |
| `app/pdu.py`                  | GSM 03.40 PDU 编/解码（SMS-DELIVER 解码、SMS-SUBMIT UCS-2 编码 + 分段）|
| `app/modem.py`                | 异步 AT 客户端：端口探测、URC 处理、+CMGR/+CMGS、自动重连              |
| `app/modem_mock.py`           | 模拟 modem，用于无硬件时的 UI 预览（`SMSGW_MOCK_MODEM=true`）          |
| `app/auth.py`                 | 会话 / API Key 鉴权依赖                                                |
| `app/hub.py`                  | WebSocket 广播总线                                                     |
| `app/rate_limit.py`           | API 滑动窗口限流                                                       |
| `app/routes_web.py`           | Web 页面 + 会话内 AJAX（/app/inbox、/app/send 等）+ /ws                |
| `app/routes_api.py`           | 公开 HTTP API（/api/v1/sms/send 等，API Key 鉴权）                     |
| `app/templates/*.html`        | Jinja2 模板                                                            |
| `app/static/{app.css,app.js}` | 前端样式与脚本                                                         |
| `install.sh`                  | 一键部署脚本（先扫描后安装）                                           |
| `scripts/uninstall.sh`        | 卸载脚本                                                               |

### 6.3 数据流：收一条短信 ✅

```
[网络]
   ↓
[模组]
   ↓ (USB)
[ttyUSB2 串口]
   ↓ (+CMTI: "SM",3 URC)
Modem._read_loop
   ↓
Modem._fetch_index(3)
   ↓ AT+CMGR=3
[模组返回 PDU hex]
   ↓
pdu.decode_deliver(...)
   ↓ IncomingSms 对象
Modem._on_sms 回调
   ↓
repo.insert_inbox(sms) → SQLite
   ↓
hub.publish("sms.received", ...)
   ↓
所有连接的 WebSocket 客户端 → 浏览器收件箱实时插入新行

同时 Modem._fetch_index 末尾发 AT+CMGD=3 删除 SIM 上的副本
```

### 6.4 数据流：发一条短信（Web） ✅

```
[浏览器]
   ↓ POST /app/send  {to, content}  (会话 cookie)
routes_web.app_send
   ↓ 输入校验
repo.queue_outbox(...) → SQLite (status=queued)
   ↓
modem.send_sms(to, content)
   ↓
pdu.encode_submit_ucs2(...) → 1+ 段 PDU
   ↓
对每段：
    AT+CMGS=<len>
    > <pdu_hex><Ctrl-Z>
    +CMGS: <mr>
    OK
   ↓
repo.mark_outbox(status=sent, segments=N)
   ↓
hub.publish("sms.sent", ...)
   ↓
浏览器发件箱刷新 + 收件箱（mock 模式回显）
```

### 6.5 数据流：发一条短信（API） ✅

同上，差别：
- 入口 `routes_api.api_send_sms`
- 鉴权由 `Depends(require_scope_send)`：API Key 头 → 哈希比对 → 校验 scope 含 `sms:send`
- 命中 `WindowLimiter`：每 Key 每分钟 30 次

---

## 7. HTTP API 与 Web 路由

### 7.1 公开 API（API Key 鉴权） ✅

完整请求/响应见 `docs/API.md`。摘要：

| 方法 | 路径                             | 鉴权                | 限流          |
| ---- | -------------------------------- | ------------------- | ------------- |
| GET  | `/api/v1/health`                 | 无                  | 无            |
| POST | `/api/v1/sms/send`               | API Key + sms:send  | 30 req/min/Key |
| GET  | `/api/v1/sms/inbox?since=&limit=`| API Key + sms:read  | 无            |
| GET  | `/api/v1/sms/messages/{id}`     | API Key + sms:read  | 无            |

API Key 头（任一）：
- `Authorization: Bearer smsgw_<明文>`
- `X-API-Key: smsgw_<明文>`

### 7.2 Web 路由（会话 cookie 鉴权） ✅

| 方法 | 路径                        | 用途                                         |
| ---- | --------------------------- | -------------------------------------------- |
| GET  | `/`                         | 智能跳转：无用户→/setup；未登录→/login；已登录→/app |
| GET  | `/setup`                    | 首次配置步骤 1 + 步骤 2（同路径渲染不同模板） |
| POST | `/setup`                    | 提交账号密码，创建用户（TOTP 未启用），渲染步骤 2 |
| POST | `/setup/confirm`            | 验证 TOTP，启用账号，发恢复码                |
| GET  | `/login`                    | 登录页                                       |
| POST | `/login`                    | 提交账号 + 密码 + TOTP/恢复码                |
| POST | `/logout`                   | 退出                                         |
| GET  | `/app`                      | 主面板（Jinja2 渲染骨架）                    |
| GET  | `/app/inbox?limit=`         | 拉取收件箱 JSON                              |
| GET  | `/app/outbox`               | 拉取发件箱 JSON                              |
| POST | `/app/send`                 | 通过会话发短信                               |
| GET  | `/app/keys`                 | 列出 API Keys                                |
| POST | `/app/keys`                 | 生成 API Key（明文仅响应一次）              |
| DEL  | `/app/keys/{id}`            | 撤销 Key                                    |
| WS   | `/ws`                       | WebSocket 实时推送（`sms.received` / `sms.sent`）|

---

## 8. 数据库 schema

SQLite，路径 `/var/lib/sms-gateway/data.db`，权限 `0600`，WAL 模式。

```sql
-- 用户（系统全局只允许一个管理员）
CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,           -- bcrypt
    totp_secret_enc BLOB,                    -- AES-GCM(nonce ‖ ct ‖ tag)
    totp_enabled    INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

-- 恢复码（一次性使用）
CREATE TABLE recovery_codes (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    code_hash TEXT NOT NULL,                 -- sha256(code)
    used_at   INTEGER
);

-- 会话（HttpOnly cookie 中存的 sid）
CREATE TABLE sessions (
    sid         TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);

-- 登录尝试（用于按用户/IP 锁定）
CREATE TABLE login_attempts (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    ip TEXT,
    success INTEGER NOT NULL,
    at INTEGER NOT NULL
);

-- API Keys（明文不入库，存哈希 + salt）
CREATE TABLE api_keys (
    id           INTEGER PRIMARY KEY,
    label        TEXT NOT NULL,
    key_hash     TEXT UNIQUE NOT NULL,       -- sha256(salt || plaintext)
    salt         TEXT NOT NULL,
    prefix       TEXT NOT NULL,              -- 明文前 8 字符（仅展示用）
    scopes       TEXT NOT NULL,              -- "sms:read,sms:send"
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,
    revoked_at   INTEGER,
    last_used_at INTEGER,
    created_by   INTEGER
);

-- 收件箱
CREATE TABLE sms_inbox (
    id           INTEGER PRIMARY KEY,
    sender       TEXT NOT NULL,
    content      TEXT NOT NULL,
    received_at  INTEGER NOT NULL,           -- 模组报的时间（SCTS 解析）
    stored_at    INTEGER NOT NULL,           -- 服务器入库时间
    raw_pdu      TEXT,                       -- 完整十六进制（取证）
    decode_error INTEGER NOT NULL DEFAULT 0,
    udh_ref      INTEGER,                    -- 长短信拼接：引用号
    udh_total    INTEGER,                    -- 总段数
    udh_seq      INTEGER                     -- 当前段
);

-- 发件箱
CREATE TABLE sms_outbox (
    id                INTEGER PRIMARY KEY,
    recipient         TEXT NOT NULL,
    content           TEXT NOT NULL,
    status            TEXT NOT NULL,         -- queued|sending|sent|failed
    error             TEXT,
    segments          INTEGER NOT NULL DEFAULT 1,
    queued_at         INTEGER NOT NULL,
    sent_at           INTEGER,
    sender_user_id    INTEGER,               -- Web 发送时填
    sender_api_key_id INTEGER                -- API 发送时填
);

-- 审计日志
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY,
    at           INTEGER NOT NULL,
    actor_kind   TEXT NOT NULL,              -- user|api_key|system
    actor_id     INTEGER,
    actor_label  TEXT,                       -- 用户名 或 Key prefix
    ip           TEXT,
    action       TEXT NOT NULL,              -- login|sms.send|apikey.revoke|...
    target       TEXT,
    status_code  INTEGER
);
```

---

## 9. 安全机制

### 9.1 密码 ✅

- bcrypt（cost=12）哈希
- 强度要求：≥12 位，必须含字母 + 数字 + 特殊字符
- 不写入日志，不在响应里回显

### 9.2 二次验证（TOTP）✅

- 算法：HMAC-SHA1 / 6 位 / 30 秒周期（RFC 6238）
- 密钥长度：≥160 bit（pyotp 默认 base32 长度 32）
- 容忍漂移：±1 个时间窗（即 ±30 秒）
- **存储加密**：TOTP secret 不明文入库；用 AES-256-GCM 加密，主密钥放 `/etc/sms-gateway/secret.key`（root:smsgw 0640）
- 兼容 Microsoft Authenticator、Google Authenticator、1Password、Authy 等所有标准 TOTP App

### 9.3 恢复码 ✅

- 数量：10 个
- 长度：≥64 bit 随机
- 存储：SHA-256 哈希
- 一次性：使用后立即标记 used_at

### 9.4 登录限流 ✅

- 滑动窗口：5 分钟
- 用户维度：5 次失败 → 锁 15 分钟
- IP 维度：10 次失败 → 锁 15 分钟
- 计数包含密码错和 TOTP 错

### 9.5 会话 ✅

- 服务端会话表，cookie 仅持 sid（不放任何业务数据）
- Cookie 属性：`HttpOnly`，`SameSite=Lax`，HTTPS 时自动 `Secure`
- 默认 8 小时过期；服务端可单点失效

### 9.6 API Key ✅

- 长度：256 bit 随机 → base64url 编码 → 加 `smsgw_` 前缀
- 存储：每 Key 独立 salt + SHA-256 哈希，**明文仅生成时一次性返回**
- 鉴权：常数时间比较（`hmac.compare_digest`）
- 权限：`sms:read` / `sms:send` 标签
- 撤销：管理员 UI 一键，下次请求立即失效（不缓存）

### 9.7 TLS ✅

- 默认：自签证书（`install.sh` 用 openssl 生成 825 天有效期 RSA-2048）
- 可选：Let's Encrypt（`--tls=letsencrypt --domain= --email=`，certbot standalone 模式 + 自动续期 timer）

### 9.8 systemd 加固 ✅

`/etc/systemd/system/sms-gateway.service` 包含：

```
NoNewPrivileges=true     # 不能 setuid 提权
ProtectSystem=full       # /usr /boot /etc 只读
ProtectHome=true         # 看不到 /home /root
PrivateTmp=true          # 私有 /tmp
Restart=on-failure       # 崩溃自动重启
RestartSec=5
```

---

## 10. 部署流程

### 10.1 一键部署 ✅

在干净的 Ubuntu 22.04（root 或 sudo 用户）：

```bash
git clone https://github.com/HCRXchenghong/4G-A7670G.git
cd 4G-A7670G
chmod +x install.sh
sudo ./install.sh                                              # 自签证书（默认）
# 或：
sudo ./install.sh --tls=letsencrypt \
                  --domain=sms.example.com \
                  --email=admin@example.com
# 仅扫描不修改：
sudo ./install.sh --scan-only
# 没插模组先把软件装好：
sudo ./install.sh --no-driver
```

脚本阶段：

1. **环境扫描** —— os-release、apt-get、systemctl、dpkg-query 列出已装/未装包、`lsusb` 扫 USB
2. **按需 apt 安装** —— 只装缺的包（python3、python3-venv、usb-modeswitch、usbutils、udev、openssl、curl、ca-certificates、可选 certbot）
3. **驱动加载** —— modprobe option + 写 new_id + 等 ttyUSB + 写 udev 规则
4. **服务用户/目录** —— 建 smsgw user，加入 dialout 组，建 /opt /var/lib /var/log /etc 目录
5. **代码同步 + venv** —— rsync `app/` + `requirements.txt` 到 /opt/sms-gateway，建 venv 装依赖
6. **TLS** —— 自签 或 certbot
7. **systemd unit** —— 注册并启动

### 10.2 升级 ✅

```bash
cd 4G-A7670G && git pull
sudo ./install.sh
```

幂等：现有用户、API Key、短信记录、TLS 证书、主密钥都保留。

### 10.3 卸载 ✅

```bash
sudo ./scripts/uninstall.sh         # 保留数据
sudo ./scripts/uninstall.sh --purge  # 连数据一起删
```

---

## 11. 故障排查

### 11.1 看服务日志

```bash
sudo journalctl -u sms-gateway -f
sudo tail -f /var/log/sms-gateway/app.log
```

### 11.2 看 dmesg（驱动 / USB 问题）

```bash
sudo dmesg --follow | grep -iE 'usb|option|tty'
# 同时把 dongle 拔下再插上
```

关键关键字：
- `option1 ttyUSB0: GSM modem (1-port) converter now attached` —— **绑定成功**
- `usb 1-1: device descriptor read/64, error -71` —— USB 接触不良或供电不足
- `usb 1-1: USB disconnect` —— 设备断开

### 11.3 没有 ttyUSB 节点

按顺序排查：

1. `lsusb | grep 1e0e`
   - 没输出：USB 没识别。换线、换口、检查 5V 供电（≥1A）
   - 显示 `ID 1e0e:f000` 之类奇怪 PID：在 Mass Storage 模式
     ```bash
     sudo apt-get install -y usb-modeswitch
     sudo usb_modeswitch -v 0x1e0e -p 0xf000 -J
     ```
2. `lsmod | grep option`
   - 没有：`sudo modprobe option`
3. `cat /sys/bus/usb-serial/drivers/option1/new_id` 看不到你的 VID/PID
   - 写一次：`echo "1e0e 9001" | sudo tee /sys/bus/usb-serial/drivers/option1/new_id`
4. 等 5 秒看 `ls /dev/ttyUSB*`

### 11.4 ttyUSB 出来了但发不了短信

```bash
sudo apt-get install -y picocom
sudo picocom -b 115200 /dev/ttyUSB2     # AT 端口；如不响应换 0/1/3
# 进入后输入：
AT
AT+CSQ                # 信号 ≥10
AT+CEREG?            # +CEREG: x,1 或 x,5 表示已注册
AT+CSCA?             # 看是否有 SMSC 号码
AT+CMGF=1            # 切到 text 模式手动测发
AT+CMGS="13800138000"
> 你好<Ctrl-Z>
```

依次出现 `+CSQ` 信号低 / `+CEREG` 未注册 / `+CMGS` 报 `+CMS ERROR` 都各有处理（参考第 5 节错误码表）。

### 11.5 服务起不来

```bash
sudo systemctl status sms-gateway
sudo journalctl -u sms-gateway -n 200 --no-pager
```

常见：
- `[Errno 13] Permission denied: '/dev/ttyUSB2'` —— 服务用户没加 dialout 组（重启 service 即可）或 udev 规则未生效（`sudo udevadm trigger`）
- `address already in use` —— 8443 端口被占（`sudo lsof -i:8443`）
- 证书路径错 —— 检查 `/etc/sms-gateway/sms-gateway.env` 里 `SMSGW_TLS_CERT/KEY`

### 11.6 浏览器看到证书警告

自签证书的预期行为。要么：
- 在浏览器里点"高级 → 继续访问"
- 把 `/etc/sms-gateway/tls/cert.pem` 添加到本机受信任根
- 或者部署时换成 Let's Encrypt（需要公网域名）

### 11.7 收到短信不显示中文

- 检查 `decode_error` 字段：`sqlite3 /var/lib/sms-gateway/data.db 'select count(*) from sms_inbox where decode_error=1'`
- 把 `raw_pdu` 字段贴给我，我能反查是 DCS 不对、UDH 异常还是模组固件 bug

---

## 12. 官方资料索引

> 以下文件均可在 SIMCom 官方下载页（https://www.simcom.com/，技术资料 / Documents Download；或 https://ask.simcom.com/）公开下载。本仓库不再分发副本，请按文件名搜索下载最新版。

### 12.1 通用 AT 与硬件

| 文件名（搜索关键字）                                 | 用途                            |
| ---------------------------------------------------- | ------------------------------- |
| *A76XX Series AT Command Manual*                     | 所有 AT 命令完整定义（最重要） |
| *A7670 Series Hardware Design*                       | 硬件设计参考                    |
| *A7670C Datasheet*                                   | 模组规格 / 频段 / 引脚          |

### 12.2 应用笔记（按需）

| 文件名                                                 | 何时需要                 |
| ------------------------------------------------------ | ------------------------ |
| *A76XX Series Linux USB Application Note*              | Linux 驱动安装详细说明   |
| *A76XX Series Sleep Mode Application Note*             | 低功耗 / 唤醒            |
| *A76XX Series UART Application Note*                   | 用 UART 而非 USB 接入    |
| *A76XX Series UIM HOT SWAP Application Note*           | 热插拔 SIM 卡            |
| *A76XX Series GNSS Application Note*                   | 定位（本项目未用）      |
| *A76XX Series TCPIP Application Note*                  | 数据上网（本项目未用）   |
| *A76XX Series HTTP(S) / MQTT(S) / FTP(S) / SSL Application Note* | 各类协议（本项目未用） |
| *A76XX Series Audio Application Note*                  | 音频 / 通话（本项目未用）|
| *A76XX Series FOTA Application Note*                   | 固件升级                 |

### 12.3 3GPP 公开规范

| 编号        | 标题                                                 | 章节落点                |
| ----------- | ---------------------------------------------------- | ----------------------- |
| TS 23.040   | Technical realization of SMS                          | PDU 结构、UDH           |
| TS 23.038   | Alphabets and language-specific information           | DCS、GSM 7-bit、UCS-2   |
| TS 27.005   | DTE-DCE interface for SMS / CBS                      | AT 命令族               |
| TS 27.007   | AT command set for User Equipment                    | 通用 AT、CME ERROR      |
| TS 24.011   | Point-to-point SMS support on mobile radio interface  | CMS ERROR 码            |

下载地址：https://www.3gpp.org/ftp/Specs/latest/Rel-17/

---

## 修订记录

| 日期       | 版本 | 内容                       |
| ---------- | ---- | -------------------------- |
| 2026-05-25 | 1.0  | 首版                       |

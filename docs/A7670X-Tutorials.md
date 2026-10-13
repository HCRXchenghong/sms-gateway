# A7670X / A7670G 板子资料与实测教程整理

本文档汇总 4G 小板子（SIMCom A7670X / A7670G USB Dongle）的官方资料入口与实测教程要点，
作为本项目语音与短信功能实现的一手依据。

## 资料来源

| 资料 | 地址 | 状态 |
| --- | --- | --- |
| 4G 小板子资料教程包（百度网盘） | https://pan.baidu.com/s/1AwKQMCbkBstVs7NSETleBw?pwd=1234 | 需登录百度账号才能下载，服务器端无法抓取，已用下方 CSDN 教程替代关键技术内容 |
| A7670X 打电话 + 语音通话 + TTS 播报 | https://cstxiot.blog.csdn.net/article/details/149358278 | 已抓取，见第 3 节 |
| A7670X 发送英文短信到手机 | https://cstxiot.blog.csdn.net/article/details/149325840 | 已抓取，见第 1 节 |
| A7670X 发送中文短信到手机 | https://cstxiot.blog.csdn.net/article/details/149331061 | 已抓取，见第 2 节 |
| A7670X 串口监听工具分析中英文短信 | https://cstxiot.blog.csdn.net/article/details/149347495 | 已抓取，见第 4 节 |

百度网盘链接返回 302 跳转到登录页，服务端无凭据无法下载。教程正文已从 CSDN 完整抓取，
AT 指令与实测响应记录在下面，硬件手册（AT Command Manual、Hardware Design）建议在能登录的
机器上手动下载后放入 docs/vendor/。

## 模块基础信息（实测响应）

教程使用的模块为 A7670G-LLSE，固件 A7670M7_V1.11.1。

| 指令 | 用途 | 实测响应 |
| --- | --- | --- |
| ATI | 型号与固件 | Manufacturer: INCORPORATED / Model: A7670G-LLSE / Revision: A7670M7_V1.11.1 / IMEI: 867284066278229 |
| AT+CIMI | IMSI | 460023606806489 |
| AT+CICCID | ICCID | +ICCID: 898600261322F1000989 |
| AT+CGSN / AT+CGSN=1 | 序列号 | 867284066278229 |
| AT+CPIN? | SIM 状态 | +CPIN: READY |
| AT+CSQ | 信号强度 0-31 | +CSQ: 21,99 |
| AT+CGATT? | PS 域附着 | +CGATT: 1 |
| AT+CREG? | GPRS 注册 | +CREG: 0,1 |
| AT+CEREG? | 4G 注册 | +CEREG: 0,1 |
| AT+CGPADDR | PDP 地址 | +CGPADDR: 1,10.139.176.153 |

Windows 下插入模块后设备管理器会出现三个带感叹号的未知设备，需要手动指向驱动目录安装。
Linux 下枚举为多个 /dev/ttyUSB*，其中 AT 口用于控制，Audio 口枚举为 USB 声卡用于通话音频。

## 1. 发送英文短信（文本模式）

教程走的是文本模式，适合快速验证：

```
AT+CMGF=1            # 1 = 文本模式，0 = PDU 模式
AT+CMGS="19359372754"
>                    # 模块返回提示符，等待正文
Hello                # 输入正文
1A                   # Ctrl+Z（0x1A）结束发送
+CMGS: 123
OK
```

## 2. 发送中文短信（PDU 模式）

中文短信必须走 PDU：

```
AT+CMGF=0            # 切到 PDU 模式
AT+CMGS=<len>        # len 为 PDU 字节长度，教程实测为 18
>
<PDU 十六进制串>
1A
+CMGS: <mr>
OK
```

把抓到的 PDU 串贴进在线解码工具，中文短信的 TP_DCS = 08，对应 UCS2（16 bit）字母表；
英文短信的 TP_DCS = 00，对应 GSM 7bit 默认字母表。本项目统一用 UCS2 编码，兼容性最好，
代价是单条容量从 160 字符降到 70 字符，超长走分段。

## 3. 拨打电话 + 通话中 TTS 播报

这是本项目语音功能的直接依据，实测顺序如下：

```
AT+CTTS=?                        # 先探测固件是否支持语音合成，返回 OK 即支持
AT+CDTAM=1                       # 开启语音与数据并发，不开则通话会挤掉数据
ATD19359372754;                  # 发起语音呼叫，结尾分号不可省
OK
AT+CTTS=2,"你好"                  # 模式 2：直接传文本（GB2312）
+CTTS: <进度>
OK
AT+CTTS=1,"6B228FCE4F7F75288BED97F3540862107CFB7EDF"   # 模式 1：UCS2 十六进制
+CTTS: <进度>
OK
AT+CTTS=0                        # 停止 / 关闭语音合成
OK
AT+CHUP                          # 挂断当前通话
OK
```

上面那串 UCS2 十六进制对应「欢迎使用语音合成系统」。

实现要点：
- AT+CTTS=1 的 UCS2 模式比模式 2 更稳，不受串口编码与固件区域设置影响，本项目默认走 UCS2，
  失败再回退模式 2。
- 发 AT+CTTS 之前必须先用 AT+CTTS=? 探测，部分批次固件不带语音合成，直接发会返回 ERROR。
- AT+CDTAM=1 要放在初始化序列里，不能等到拨号前才发。
- 拨号响应可能是 OK（受理）也可能是 BUSY / NO CARRIER / NO ANSWER / NO DIALTONE 直接结束，
  AT 命令封装必须把这些行当作终止条件，否则会一直等到超时。

## 4. 串口监听分析（第三方软件的初始化序列）

用串口监控精灵抓「企业短信通」这类商业软件的流量，可以看到成熟的初始化顺序：

```
ATE0                     # 关回显，避免指令被重复返回
AT+CMGF                  # 查询当前短信模式，默认返回 0（PDU）
AT+CNMI=2,1,0,0,0        # 新短信主动上报
AT+CLIP=1                # 开来电显示，来电时能拿到主叫号码
AT+CMGL=0                # 列出全部短信
AT+CMGL=1                # 只列未读
AT+CPMS="SM"             # 短信存储设为 SIM 卡
AT+CMGR=2 / AT+CMGR=1    # 按索引读取
AT+CMGD=2 / AT+CMGD=1    # 按索引删除
AT+CPMS="SM"             # 每次删除后重新确认存储状态
AT+CMGS=18               # 发送，18 为 PDU 长度
```

值得注意的两点：一是删除短信后重新执行 AT+CPMS="SM" 来同步容量状态，本项目的
modem 层沿用了这个习惯；二是 ATE0 必须最先发，否则后续所有响应都会混入指令回显，
解析器会被污染。

串口监控精灵下载地址（教程原文给出）：https://www.ceiwei.com/mt/

## 5. 与本项目实现的对应关系

| 教程要点 | 代码位置 |
| --- | --- |
| ATE0 / AT+CMEE=2 / AT+CMGF=0 / AT+CPMS / AT+CNMI 初始化 | app/modem.py 初始化序列 |
| AT+CVHU=1 / AT+CLIP=1 / AT+CDTAM=1 语音初始化 | app/modem.py 初始化序列 |
| ATD<n>; 拨号与终止行处理 | app/modem.py dial_call，accept_call_progress |
| AT+CHUP 挂断并回退 ATH | app/modem.py hangup |
| AT+CTTS=? 能力探测 | app/modem.py tts_supported |
| AT+CTTS=1 UCS2 / =2 文本 / =0 停止 | app/modem.py speak |
| AT+VTS DTMF | app/modem.py send_dtmf |
| AT+CLCC 校正状态机 | app/voice/manager.py 轮询 |
| AT+CSDVC 音频通道 / AT+CLVL 音量 / AT+CMUT 静音 | app/modem.py 音频控制 |
| RING / +CLIP / NO CARRIER / BUSY / NO ANSWER / NO DIALTONE 解析 | app/voice/parser.py |
| PDU 编解码（TP_DCS 08 UCS2） | app/pdu.py |


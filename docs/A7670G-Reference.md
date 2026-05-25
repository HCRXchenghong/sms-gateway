# A7670G 模块与项目参考

本项目面向 SIMCom A7670G 4G USB Dongle，用 PDU 模式收发短信。

## 关键 AT 指令

- `ATE0`：关闭回显
- `AT+CMEE=2`：开启详细错误
- `AT+CMGF=0`：启用 PDU 短信模式
- `AT+CPMS="SM","SM","SM"`：使用 SIM 卡短信存储
- `AT+CNMI=2,1,0,0,0`：新短信以 `+CMTI` 上报
- `AT+CMGR=<idx>`：读取短信
- `AT+CMGD=<idx>`：删除 SIM 上已处理短信
- `AT+CMGS=<len>`：发送 PDU 短信
- `AT+CMGL=4`：启动时扫描 SIM 上已有短信
- `AT+CNUM`：尽力读取 SIM 本机号码，运营商不返回时需要后台人工备注
- `AT+CCID` / `AT+CIMI` / `AT+CGSN`：读取 ICCID、IMSI、IMEI，用于设备识别与排查

## 项目结构

- `app/modem_manager.py`：多模块发现、物理 USB 分组、设备生命周期管理
- `app/modem.py`：单个 AT 串口连接、AT 命令、收发短信
- `app/pdu.py`：PDU 解码与 UCS2 编码
- `app/routes_web.py`：Web 页面与会话接口
- `app/routes_api.py`：API Key 保护的 HTTP API
- `app/db.py` / `app/repo.py`：SQLite schema 与数据访问

## 接收流程

1. `ModemManager` 按物理 USB 设备分组，为每个 A7670G 选择一个可响应 AT 的端口
2. 单个 modem 收到 `+CMTI: "SM",<idx>`
3. 服务执行 `AT+CMGR=<idx>` 读取 PDU
4. `app/pdu.py` 解码短信内容
5. 写入 `sms_inbox`，同时记录 `device_id`
6. 通过 WebSocket 推送到浏览器
7. 执行 `AT+CMGD=<idx>` 释放 SIM 存储

## 发送流程

1. Web 或 API 提交号码、内容和可选 `device_id`
2. 写入 `sms_outbox`
3. 未指定设备时自动选择第一台在线设备；指定设备时只使用该设备的串口
4. `encode_submit_ucs2` 生成 1 个或多个 PDU 段
5. 目标 modem 逐段执行 `AT+CMGS`
6. 成功后更新发件箱状态并推送 WebSocket

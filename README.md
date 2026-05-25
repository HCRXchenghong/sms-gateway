# SMS Gateway for SIMCom A7670G 4G USB Dongle

基于 SIMCom A7670G 4G USB Dongle 的短信收发网关，支持 Web 实时收发短信、HTTP API 调用，目标运行环境为 Ubuntu 22.04。

## 功能

- 多 A7670G 模块同时接入：按物理 USB 设备分组，每个模块只绑定一个 AT 端口，避免多个设备抢同一串口
- 设备自动编号：新设备按发现顺序显示为 `编号001`、`编号002`，管理员可备注设备名和 SIM 号码
- SIM 信息识别：启动时会尽力读取 `AT+CNUM`、ICCID、IMSI、IMEI、厂商和型号；运营商不返回本机号码时使用后台备注
- 实时接收短信：PDU 模式、UCS2 中文、长短信拼接
- 网页发送短信：支持指定设备发送，未指定时自动选择第一台在线设备
- HTTP API + API Key 鉴权：支持 `sms:read` / `sms:send` 权限
- API Key 授权策略：可限制可用 SIM、发送次数上限，或设置为全部 SIM / 无限使用
- Python SDK：位于 `sdk/python/smsgw_client.py`
- 独立 API 客户端：位于 `api-client/`，给 API 使用者填写地址和 Key 后收发短信
- 用户登录 + TOTP 双因素认证：兼容 Microsoft Authenticator
- 一键部署脚本：安装依赖、配置 systemd、TLS、USB 串口权限

## 本地预览

Windows 本地预览可运行：

```bat
dev_run.bat
```

它会使用 `.env.dev` 中的 mock modem，不需要真实 4G 模块。可通过 `SMSGW_MOCK_MODEM_COUNT` 设置模拟设备数量。

Windows 真实硬件测试可运行：

```bat
real_run.bat
```

它会关闭 mock，并自动识别已插入的 SIMCom AT Port。

## Ubuntu 部署

```bash
sudo ./install.sh
sudo ./install.sh --tls=letsencrypt --domain=sms.example.com --email=admin@example.com
```

部署完成后，访问输出的 HTTPS 地址，按引导创建管理员账号并绑定 2FA。
更完整的 Ubuntu 部署、升级和排障说明见 [docs/UBUNTU_DEPLOY.md](docs/UBUNTU_DEPLOY.md)。

上传前可在 Linux/Ubuntu 上执行：

```bash
bash scripts/check.sh
```

## 文档

- API 说明：[docs/API.md](docs/API.md)
- API 与 SDK 详细文档：[docs/API_SDK.md](docs/API_SDK.md)
- Ubuntu 部署：[docs/UBUNTU_DEPLOY.md](docs/UBUNTU_DEPLOY.md)
- 独立 API 客户端：[api-client/README.md](api-client/README.md)
- Python SDK：[sdk/README.md](sdk/README.md)
- A7670G 参考：[docs/A7670G-Reference.md](docs/A7670G-Reference.md)
- 卸载：`sudo ./scripts/uninstall.sh`

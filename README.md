# SMS Gateway for SIMCom A7670G 4G USB Dongle

基于 SIMCom A7670G 4G USB Dongle 的短信收发网关，支持 Web 实时收发短信、HTTP API 调用，运行在 Ubuntu 22.04。

## 功能

- 实时接收短信（PDU 模式 + UCS2 中文 + 长短信拼接）
- 网页发送短信（支持中文与多段）
- HTTP API + API Key 鉴权（`sms:read` / `sms:send` 权限粒度）
- 用户登录 + TOTP 双因素鉴权（兼容 Microsoft Authenticator）
- 一键部署脚本：自动加载 USB 串口驱动、识别 AT 端口、安装 systemd 服务

## 一键部署

在 Ubuntu 22.04 上：

```bash
git clone <this-repo>
cd <repo-dir>
sudo ./install.sh                                    # 默认自签证书
# 或者使用 Let's Encrypt：
sudo ./install.sh --tls=letsencrypt --domain=sms.example.com --email=admin@example.com
```

部署完成后用浏览器访问输出的 HTTPS 地址，按引导完成首次配置（设置账号密码 + 扫码绑定 2FA）。

## 文档

- 需求：[`.kiro/specs/sms-gateway-a7670x/requirements.md`](.kiro/specs/sms-gateway-a7670x/requirements.md)
- 卸载：`sudo ./scripts/uninstall.sh`
- API 速查：[docs/API.md](docs/API.md)

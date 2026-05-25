# Ubuntu 部署说明

目标环境：Ubuntu 22.04/24.04，systemd，SIMCom A7670G USB 4G 模块。

## 1. 上传代码

```bash
git clone https://github.com/your-org/sms-gateway.git
cd sms-gateway
```

生产环境不要提交或上传以下目录：

- `.env`
- `_realdata/`
- `_devdata/`
- `.venv/`
- `*.db`
- `*.log`
- `4G USB DONGLE A7670X/`

这些已经在 `.gitignore` 中排除。

## 2. 执行安装

自签名 HTTPS：

```bash
sudo bash install.sh
```

Let’s Encrypt HTTPS：

```bash
sudo bash install.sh --tls=letsencrypt --domain=sms.example.com --email=admin@example.com
```

安装脚本会完成：

- 安装 Python、venv、udev、usb-modeswitch、usbutils、OpenSSL 等依赖
- 创建 `smsgw` 系统用户
- 写入 A7670G udev 串口权限规则
- 默认停用 `ModemManager`，避免它抢占 AT 串口
- 同步代码到 `/opt/sms-gateway`
- 创建数据目录 `/var/lib/sms-gateway`
- 创建日志目录 `/var/log/sms-gateway`
- 创建配置目录 `/etc/sms-gateway`
- 注册并启动 `sms-gateway.service`

如果服务器必须保留 `ModemManager`，可以使用：

```bash
sudo bash install.sh --keep-modemmanager
```

但它可能会和本服务争抢 `/dev/ttyUSB*` AT 端口。

## 3. 查看服务

```bash
systemctl status sms-gateway
journalctl -u sms-gateway -f
```

健康检查：

```bash
curl -k https://127.0.0.1:8443/api/v1/health
```

首次打开 Web 控制台后，按页面引导创建唯一管理员账号并绑定 2FA。

## 4. 硬件排查

查看 USB 设备：

```bash
lsusb
```

查看串口：

```bash
ls -l /dev/ttyUSB*
dmesg | grep -i ttyUSB
```

正常情况下，一个 A7670G 会暴露多个串口，本项目会按物理 USB 设备分组，并只选择一个可响应 AT 指令的端口，避免同一模块的多个接口互相冲突。

## 5. 升级

```bash
git pull
sudo bash install.sh
```

安装脚本会重新同步代码、安装依赖并重启服务；数据库和配置不会被删除。

## 6. 卸载

保留数据：

```bash
sudo bash scripts/uninstall.sh
```

彻底删除数据、配置和用户：

```bash
sudo bash scripts/uninstall.sh --purge
```

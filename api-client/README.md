# SMS API Client

这是独立于管理后台的静态客户端，只面向 API Key 使用者。

## 使用

直接打开 `index.html`，填写：

- API 地址：例如 `https://sms.example.com` 或 `http://127.0.0.1:8080`
- API Key：后台 API 页面生成的 `smsgw_...`

进入后只能进行短信发送、收件箱查看和发送记录查看。可用 SIM、发送权限和发送次数额度都由管理员在后台的 API Key 设置中控制。

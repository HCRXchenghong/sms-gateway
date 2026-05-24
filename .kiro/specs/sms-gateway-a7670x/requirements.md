# Requirements Document

## Introduction

本项目基于 SIMCom **A7670G** 4G USB Dongle 模块，在 Ubuntu 22.04 系统上构建一个短信收发网关。系统提供一键部署能力（克隆自 GitHub 后执行单一脚本即可完成驱动加载、依赖安装与服务启动），通过 Web 界面实时展示收到的短信、提交发送任务，并通过受保护的 HTTP API + API Key 鉴权方式供第三方系统调用。Web 界面前置登录页，使用账号密码鉴权。

仅聚焦短信收发功能，不涉及通话、彩信、数据拨号等其它能力。

## Glossary

- **A7670G**：SIMCom A7670 系列 4G LTE Cat-1 USB 模块（全球版），通过 USB 暴露多个 ttyUSB 串口（其中 AT 端口用于发送 AT 指令）。
- **AT 端口**：模块通过 USB 暴露的若干 `/dev/ttyUSBx` 中专用于 AT 指令通信的串口（A7670X 通常为序号靠后的端口，如 ttyUSB2）。
- **PDU 模式 / 文本模式**：短信编码模式。文本模式（CMGF=1）仅支持 GSM 7bit；PDU 模式支持 UCS2，能够发送和接收中文及任意 Unicode。
- **API Key**：调用 HTTP API 时使用的密钥凭证；以哈希形式存储于服务器，密钥明文仅在生成时一次性展示。
- **会话 Token**：Web 登录后服务端签发的会话凭证（HttpOnly Cookie 中的 JWT 或随机 session id）。
- **TOTP**：Time-based One-Time Password（RFC 6238），基于共享密钥与当前时间生成 6 位动态验证码，标准实现兼容 Microsoft Authenticator、Google Authenticator 等通用 2FA App。
- **2FA / MFA**：Two-Factor / Multi-Factor Authentication，登录除账号密码外额外要求 TOTP 验证码。
- **一键部署脚本**：项目根目录下的 `install.sh`，执行后自动完成驱动检测/安装、依赖安装、服务配置与启动。

## Requirements

### Requirement 1: 一键部署

**User Story:** 作为运维人员，我希望从 GitHub 克隆项目后只执行一个脚本就能完成全部部署，这样不必手动处理驱动、依赖和服务配置。

#### Acceptance Criteria

1. WHEN 用户在 Ubuntu 22.04 上以 root（或具备 sudo 权限的用户）执行 `sudo ./install.sh` THEN 系统 SHALL 在无需任何额外手动步骤的情况下完成驱动加载、Python/系统依赖安装、服务安装与启动。
2. IF 当前系统不是 Ubuntu 22.04 或缺少 `apt`、`systemd` THEN `install.sh` SHALL 立即终止并输出明确的不兼容提示。
3. WHEN `install.sh` 执行过程中任一关键步骤失败（依赖安装、驱动加载、服务启动） THEN 脚本 SHALL 以非零状态码退出并打印失败步骤与日志位置。
4. WHEN 脚本执行成功 THEN 输出 SHALL 包含 Web 访问地址（含端口、HTTPS 协议）以及生成的初始 API Key（仅展示一次）；初始管理员账号密码不在脚本中预设，由用户在首次访问 Web 时设置。
5. WHILE `install.sh` 正在运行 THE 脚本 SHALL 在每个阶段（环境检查 / 驱动 / 依赖 / 配置 / 服务）输出明确的阶段标记和成功失败状态。
6. WHEN 用户重复执行 `install.sh`（幂等场景） THEN 脚本 SHALL 跳过已完成的步骤并保持现有数据（用户、API Key、短信记录）不被覆盖。
7. WHEN `install.sh` 执行 HTTPS 配置阶段 THEN 脚本 SHALL 通过命令行参数支持两种模式：
   - 默认 / `--tls=self-signed`：生成自签证书写入 `/etc/sms-gateway/tls/`，并在首次访问时提示用户信任。
   - `--tls=letsencrypt --domain=<domain> --email=<email>`：调用 `certbot`（脚本自动安装）以 standalone 或 webroot 方式签发证书，并写入自动续期 cron / systemd timer。
8. IF 用户传入 `--tls=letsencrypt` 但未提供 `--domain` 或 `--email` THEN 脚本 SHALL 终止并报错，明确要求补全参数。

### Requirement 2: 模块识别与驱动自动加载

**User Story:** 作为用户，我希望插上 A7670G 之后脚本能自动识别并打驱动，这样我不用手动 modprobe 或编译内核模块。

#### Acceptance Criteria

1. WHEN `install.sh` 进入驱动阶段 THEN 系统 SHALL 通过 `lsusb` 检查是否存在 A7670G 的 USB VID:PID（已知候选包含 SIMCom `1e0e:9001`、`1e0e:9011`、`1e0e:9018`，VID/PID 列表 SHALL 集中在 `install.sh` 顶部以便维护）。
2. IF 已检测到 A7670X 设备但未出现 `/dev/ttyUSB*` 节点 THEN 脚本 SHALL 自动加载 `option` 内核模块并通过 `/sys/bus/usb-serial/drivers/option1/new_id` 写入对应 VID:PID 以绑定串口驱动。
3. WHEN 驱动绑定成功 THEN 系统 SHALL 在 30 秒内出现至少 3 个 `/dev/ttyUSB*` 节点。
4. IF 30 秒内未出现 ttyUSB 节点 THEN 脚本 SHALL 报错并提示用户检查 USB 连接、`dmesg` 日志和 USB 模式（避免设备处于 CD-ROM 模式，必要时提示安装 `usb-modeswitch`）。
5. WHEN 检测到设备处于 USB Mass Storage / CD-ROM 模式 THEN 脚本 SHALL 安装并调用 `usb-modeswitch` 切换到串口模式后再绑定驱动。
6. WHEN 多个 ttyUSB 节点出现 THEN 系统 SHALL 通过向各端口发送 `AT` 指令自动识别哪个端口对 AT 指令有有效响应（OK），并将该端口写入运行配置作为 AT 端口。
7. WHILE 服务运行中 IF AT 端口被拔出后重新插入 THEN 服务 SHALL 在 60 秒内重新探测并恢复连接，无需重启 systemd 服务。
8. THE 脚本 SHALL 写入 udev 规则（`/etc/udev/rules.d/99-a7670g.rules`），将匹配的 ttyUSB 设备的属主设置为运行服务的系统用户并赋予读写权限。

### Requirement 3: 短信接收（实时）

**User Story:** 作为用户，我希望收到的短信能立即显示在 Web 页面上，这样我能即时看到信息。

#### Acceptance Criteria

1. WHEN 服务启动并完成 AT 端口绑定 THEN 系统 SHALL 配置模块进入 PDU 模式（`AT+CMGF=0`）并启用新短信主动上报（`AT+CNMI=2,1,0,0,0`）。
2. WHEN 模块上报新短信索引（`+CMTI: "SM",<index>`） THEN 系统 SHALL 在 5 秒内通过 `AT+CMGR=<index>` 读取该短信。
3. WHEN 短信被读取成功 THEN 系统 SHALL 解码 PDU（包括 GSM 7bit 与 UCS2 中文）并将以下字段持久化到本地数据库：发送方号码、接收时间（模块时间戳）、入库时间（服务器时间戳）、内容、原始 PDU。
4. WHEN 短信入库后 THEN 系统 SHALL 通过 WebSocket（或 SSE）向所有已认证的浏览器连接广播该短信，端到端延迟（模块上报到浏览器渲染） SHALL 不超过 3 秒（在本机网络下）。
5. WHEN 单条短信为长短信（多段拼接） THEN 系统 SHALL 通过 UDH 拼接所有分段后再入库与推送，仅推送拼接后的完整内容。
6. WHEN 模块 SIM 存储满 THEN 系统 SHALL 在读取并入库后立即执行 `AT+CMGD=<index>` 删除模块上的副本以释放空间。
7. IF 短信解码失败 THEN 系统 SHALL 仍将原始 PDU 入库并标记 `decode_error=true`，但不阻塞后续短信处理。

### Requirement 4: 短信发送（Web 与 API）

**User Story:** 作为用户，我希望在网页上输入号码和内容就能发送短信，第三方系统也能通过 API + Key 触发发送。

#### Acceptance Criteria

1. WHEN 已登录用户在 Web 发送页提交目标号码与内容并点击发送 THEN 系统 SHALL 在后端校验号码格式（E.164 或纯数字+可选 `+`）和内容长度（UCS2 模式下不超过 70 字符 / 段，自动分段最多 N 段，N 默认 6）。
2. WHEN 提交校验通过 THEN 系统 SHALL 通过 PDU 模式（含 UCS2 编码以支持中文）调用 `AT+CMGS` 完成发送。
3. WHEN 发送成功（模块返回 `+CMGS: <mr>` 与 `OK`） THEN 系统 SHALL 入库一条 `outbound` 记录并通过 WebSocket 推送状态 `sent`。
4. IF 模块返回 `+CMS ERROR` 或超时（默认 60 秒） THEN 系统 SHALL 入库为 `failed` 并返回结构化错误信息（错误码与原因）。
5. WHEN 第三方系统使用合法的 API Key 调用 `POST /api/v1/sms/send` 并提供 `to`、`content` THEN 系统 SHALL 执行与 Web 等价的发送流程并返回 JSON 结果（`message_id`、`status`、`segments`）。
6. WHEN API Key 缺失、错误或被禁用 THEN API SHALL 返回 HTTP 401，且 SHALL NOT 透露密钥是否存在的细节。
7. WHEN 同一 API Key 在 60 秒内调用 `/api/v1/sms/send` 超过 配置上限（默认 30 次） THEN 系统 SHALL 返回 HTTP 429，并在响应头中包含 `Retry-After`。
8. THE 发送队列 SHALL 串行化对 AT 端口的访问（同一时刻只有一条 `AT+CMGS` 在执行），避免并发损坏 PDU 协议状态。

### Requirement 5: 短信查询 API

**User Story:** 作为第三方系统集成方，我希望能通过 API 拉取已收到的短信，以便对接到自己的业务流程。

#### Acceptance Criteria

1. WHEN 第三方使用合法 API Key 调用 `GET /api/v1/sms/inbox?since=<iso8601>&limit=<n>` THEN 系统 SHALL 返回该时间点之后接收到的短信列表（按接收时间升序），`limit` 默认 50、上限 500。
2. WHEN 查询参数 `since` 缺失 THEN 系统 SHALL 返回最新 `limit` 条短信（按时间降序）。
3. WHEN 第三方调用 `GET /api/v1/sms/messages/{id}` 且该消息存在 THEN 系统 SHALL 返回单条消息的完整字段（含发送方号码、内容、PDU、状态、时间戳）。
4. IF 调用方仅有"只读"权限的 Key（参见需求 7） THEN 该 Key SHALL 仅能访问查询类接口，不得调用发送接口。
5. THE API 响应 SHALL 使用 JSON 格式，错误响应 SHALL 包含 `error.code` 与 `error.message` 两个字段。

### Requirement 6: 用户登录、首次配置与双因素鉴权

**User Story:** 作为系统管理员，我希望首次访问时设置自己的账号密码并完成 2FA 绑定，之后每次登录都需要密码 + TOTP 验证码，这样能防止账号泄漏后被直接登录。

#### Acceptance Criteria

1. WHEN 系统首次启动且数据库中没有任何管理员账号 THE 系统 SHALL 进入"首次配置"状态：访问任何路径都被重定向到 `/setup`，且 `/setup` 仅在该状态下可访问。
2. WHEN 用户在 `/setup` 提交 `用户名 / 密码 / 确认密码` 且通过校验（用户名 3-32 字符；密码至少 12 字符并包含字母、数字、特殊字符各 1 个） THEN 系统 SHALL：
   - 使用 bcrypt（cost ≥ 12）或 argon2id 哈希密码并入库；
   - 生成一个高熵 TOTP 共享密钥（≥ 160 bit）并以 `otpauth://totp/SMS-Gateway:<username>?secret=...&issuer=SMS-Gateway&algorithm=SHA1&digits=6&period=30` 形式渲染为二维码返回到浏览器（兼容 Microsoft Authenticator、Google Authenticator）。
3. WHEN 二维码页面展示后 THE 系统 SHALL 要求用户输入一次当前 TOTP 验证码以确认绑定成功，确认通过后才将该 TOTP 密钥标记为 `enabled=true`；未确认前账号 SHALL NOT 被激活，也 SHALL NOT 允许登录。
4. WHEN 用户首次绑定 TOTP 成功 THEN 系统 SHALL 同时生成 N 个一次性"恢复码"（默认 N=10，每个至少 64 bit 随机），仅展示一次，明文 SHALL NOT 入库（仅存哈希），用于丢失 2FA 设备时一次性登录。
5. WHEN 已存在管理员账号 THE `/setup` 路径 SHALL 返回 404 或重定向到 `/login`，禁止再次创建。
6. WHEN 未登录用户访问任何 Web 页面（除 `/login`、`/setup`、静态资源） THEN 系统 SHALL 重定向到 `/login`。
7. WHEN 用户在 `/login` 提交 `账号 + 密码 + 6 位 TOTP 验证码` THEN 后端 SHALL 依次校验密码哈希与 TOTP（容忍前后 1 个时间窗，即 ±30 秒）；只有两者都通过才签发会话 Token 并写入 `HttpOnly`、`Secure`（HTTPS 时）、`SameSite=Lax` 的 Cookie。
8. WHEN 用户提交的是恢复码而非 TOTP（前端切换至"使用恢复码"模式） THEN 系统 SHALL 校验恢复码哈希并在使用后立即标记该恢复码为已用，单个恢复码 SHALL NOT 被复用。
9. WHEN 同一账号在 5 分钟内连续登录失败超过 5 次（密码错或 TOTP 错均计数） THEN 系统 SHALL 锁定该账号 15 分钟，期间即使凭据正确也拒绝登录；锁定 SHALL 同时按账号与来源 IP 双维度计数。
10. WHEN 已登录用户请求 `POST /logout` THEN 系统 SHALL 使当前会话立即失效。
11. THE 会话 Token 默认有效期 SHALL 为 8 小时，过期后用户必须重新登录（含 TOTP）。
12. THE 密码、TOTP 共享密钥、恢复码 SHALL NOT 以明文形式写入日志；TOTP 密钥在数据库中 SHALL 以加密形式存储（使用 `install.sh` 写入 `/etc/sms-gateway/secret.key` 的本地主密钥进行 AES-GCM 加密）。
13. WHEN 已登录用户访问"账户安全"页面 THEN 系统 SHALL 允许其重置密码（需输入旧密码 + 当前 TOTP）以及重新绑定 2FA（旧 TOTP 验证后展示新二维码与新恢复码）。

### Requirement 7: API Key 管理与安全

**User Story:** 作为系统管理员，我希望能签发、查看、撤销 API Key，并且密钥本身要安全存储，这样泄漏风险可控。

#### Acceptance Criteria

1. WHEN 已登录管理员在密钥管理页点击"生成新 Key" THEN 系统 SHALL 生成至少 32 字节高熵随机字符串作为 Key，仅在生成时一次性向用户展示明文。
2. WHEN 生成 Key 时 THEN 系统 SHALL 使用 SHA-256（加 per-key salt）对 Key 哈希后存储；明文 Key SHALL NOT 以任何形式持久化。
3. WHEN 客户端通过 `Authorization: Bearer <key>` 或 `X-API-Key` 头携带 Key 调用 API THEN 系统 SHALL 对传入 Key 进行哈希后与库中记录比对（使用常数时间比较函数），并校验 Key 是否被禁用、是否过期。
4. WHEN 管理员在管理页"撤销" 一个 Key THEN 该 Key 在下一次请求时立即失效（无缓存延迟超过 60 秒）。
5. THE Key SHALL 携带权限标签（至少 `sms:read`、`sms:send` 两种），调用接口时 SHALL 校验所需权限。
6. WHEN 任意 API 请求被处理 THEN 系统 SHALL 记录调用日志（时间、Key 标识 / 前 4 位、IP、路径、状态码），但 SHALL NOT 记录 Key 明文。
7. THE API SHALL 仅接受 HTTPS 请求；IF 部署在反向代理后 THE 服务 SHALL 通过受信任的 `X-Forwarded-Proto` 校验来源协议（默认 HTTPS 部署由 install 脚本协助配置自签证书或提示用户挂在 nginx 之后）。

### Requirement 8: 数据持久化与日志

**User Story:** 作为运维，我希望短信和审计日志被可靠保存且重启后仍然可见。

#### Acceptance Criteria

1. THE 系统 SHALL 使用 SQLite（默认）持久化用户、API Key 哈希、短信收件箱、短信发件箱、审计日志，数据库文件位于 `/var/lib/sms-gateway/data.db`，权限 `0600`。
2. WHEN 服务停止再启动 THEN 所有历史短信与配置 SHALL 保留。
3. THE 系统 SHALL 将运行日志写入 `/var/log/sms-gateway/app.log`，按日轮转，保留至少 14 天。
4. THE 日志中 SHALL NOT 出现密码明文、API Key 明文、完整的 PDU 长串以外的敏感数据。

### Requirement 9: 服务运行与可观测

**User Story:** 作为运维，我希望服务能开机自启，并能方便地查看状态。

#### Acceptance Criteria

1. WHEN 部署完成 THEN 系统 SHALL 注册名为 `sms-gateway.service` 的 systemd 单元并设置为 `enabled`。
2. WHEN 系统重启 THEN `sms-gateway.service` SHALL 自动启动并在 30 秒内完成 AT 端口连接。
3. WHEN 服务进程崩溃 THEN systemd SHALL 自动重启服务（`Restart=on-failure`，`RestartSec=5`）。
4. WHEN 调用 `GET /api/v1/health`（无需鉴权） THEN 系统 SHALL 返回 JSON `{ "status": "ok|degraded|down", "modem": "connected|disconnected", "uptime_seconds": <n> }`，其中 `modem` 反映与 AT 端口的实时连接状态。

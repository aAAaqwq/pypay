# 启动、部署与回滚

本文覆盖本地启动、单机生产部署、日常运维和回滚。首期按**单机 + SQLite + 独立 worker** 设计，不引入额外中间件。

## 1. 本地启动

```bash
# Python 3.10+
uv venv
uv pip install -e ".[service,dev]"

cp .env.example .env
# 编辑 .env：数据库路径、公网地址、master key、admin 密码、渠道账户

python -m pypay_service migrate up
python -m pypay_service serve --host 127.0.0.1 --port 8080
```

另开一个终端运行 worker：

```bash
python -m pypay_service worker
```

本地没有真实渠道时，直接运行 `python examples/service_demo.py`，它使用假渠道和假 notifier，不需要任何商户凭据。

## 2. 生产部署前提

- Python 3.10+，已安装 `pypay[service]`。
- 一个可写的持久化目录存放 SQLite 数据库。
- 公网 HTTPS 域名，例如 `pay.example.com`；微信 Native 要求 HTTPS 回调。
- 反向代理（Caddy/Nginx）负责 TLS、请求体大小限制和访问日志。
- 微信/支付宝商户凭据通过环境变量或只读密钥文件注入，不落盘到 Git 或镜像。
- 管理后台放在 HTTPS + VPN/SSO/来源限制之后。

## 3. 安装与目录

```bash
sudo useradd --system --home /opt/pypay --shell /usr/sbin/nologin pypay
sudo mkdir -p /opt/pypay/var
sudo chown -R pypay:pypay /opt/pypay

python -m venv /opt/pypay/venv
/opt/pypay/venv/bin/pip install '/path/to/pypay[service]'
```

生成 master key：

```bash
python -c "import base64,os; print('base64:'+base64.b64encode(os.urandom(32)).decode())"
```

将环境变量写入 `/opt/pypay/pypay.env`，权限 `0600`，属主 `pypay`：

```bash
PYPAY_SERVICE_DATABASE_PATH=/opt/pypay/var/pypay_service.db
PYPAY_SERVICE_PUBLIC_BASE_URL=https://pay.example.com
PYPAY_SERVICE_MASTER_KEY=base64:...
PYPAY_SERVICE_ADMIN_USERNAME=admin
PYPAY_SERVICE_ADMIN_PASSWORD=...
PYPAY_SERVICE_VIEWER_USERNAME=viewer
PYPAY_SERVICE_VIEWER_PASSWORD=...
PYPAY_SERVICE_ALLOW_HTTP_URLS=0
PYPAY_SERVICE_ALLOW_PRIVATE_URLS=0
PYPAY_SERVICE_ENABLE_DOCS=0

# 渠道凭据（示例占位，使用真实密钥文件路径）
PYPAY_WECHAT_MCHID=...
PYPAY_WECHAT_APPID=...
PYPAY_WECHAT_APIV3_KEY=...
PYPAY_WECHAT_CERT_SERIAL_NO=...
PYPAY_WECHAT_PUBLIC_KEY_ID=...
PYPAY_WECHAT_PRIVATE_KEY_PATH=/opt/pypay/secrets/apiclient_key.pem
PYPAY_WECHAT_PUBLIC_KEY_PATH=/opt/pypay/secrets/wechatpay_public_key.pem
PYPAY_WECHAT_NOTIFY_URL=https://pay.example.com/notify/wechat/wechat-default

PYPAY_ALIPAY_APPID=...
PYPAY_ALIPAY_PRIVATE_KEY_PATH=/opt/pypay/secrets/app_private_key.pem
PYPAY_ALIPAY_PUBLIC_KEY_PATH=/opt/pypay/secrets/alipay_public_key.pem
PYPAY_ALIPAY_NOTIFY_URL=https://pay.example.com/notify/alipay/alipay-default
PYPAY_ALIPAY_RETURN_URL=https://pay.example.com/pay/return/alipay
```

> 支付宝的 `RETURN_URL` 会被服务按订单覆盖为 `/pay/{token}/return/alipay`；配置里的值只需是合法绝对地址，用于满足渠道配置校验。

密钥文件权限建议 `0600`，目录 `0700`。

## 4. 迁移

部署新版本前先备份数据库，再执行迁移：

```bash
sudo -u pypay bash -c '
  set -a
  . /opt/pypay/pypay.env
  set +a
  exec /opt/pypay/venv/bin/python -m pypay_service migrate up
'
```

环境文件中的值如含空格，请按 shell 规则加引号（systemd `EnvironmentFile` 同样适用）。

服务启动时默认也会执行 `migrate up`。生产建议把迁移作为独立发布步骤，确认成功后再启动新进程。

## 5. 进程管理

HTTP 与 worker 必须分别运行，worker 不能省。

### systemd：HTTP

```ini
# /etc/systemd/system/pypay-web.service
[Unit]
Description=pypay unified payment service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pypay
Group=pypay
WorkingDirectory=/opt/pypay
EnvironmentFile=/opt/pypay/pypay.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/pypay/venv/bin/python -m pypay_service serve --host 127.0.0.1 --port 8080
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/pypay/var

[Install]
WantedBy=multi-user.target
```

### systemd：worker

```ini
# /etc/systemd/system/pypay-worker.service
[Unit]
Description=pypay outbox and reconciliation worker
After=network-online.target pypay-web.service
Wants=network-online.target

[Service]
Type=simple
User=pypay
Group=pypay
WorkingDirectory=/opt/pypay
EnvironmentFile=/opt/pypay/pypay.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/opt/pypay/venv/bin/python -m pypay_service worker
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/pypay/var

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pypay-web pypay-worker
```

多 worker 可以同时运行：数据库租约保证同一事件只被一个 worker 领取。SQLite 写入仍是单写者，首期不建议高并发多实例。

### 健康检查

```bash
curl -fsS https://pay.example.com/health
curl -fsS https://pay.example.com/ready
```

`/ready` 依赖数据库可用。将 `/ready` 接入进程健康检查；`/health` 可用于查看已配置渠道。

## 6. 反向代理

要求：

- 保留原始请求路径和 body，不重写、不压缩、不重新编码。微信回调验签依赖原始字节。
- 请求体上限不小于服务配置 `PYPAY_SERVICE_REQUEST_MAX_BYTES`（默认 256 KiB）。
- 关闭对 `/notify/*` 的缓存。
- 为 `/v1/*`、`/pay/*`、`/notify/*` 配置合理超时；不要对回调做重试放大。
- 强制 HTTPS；HSTS 由服务在 HTTPS 时输出。

Caddy 示例：

```caddy
pay.example.com {
    encode zstd gzip
    request_body {
        max_size 1MB
    }
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Nginx 示例：

```nginx
server {
    listen 443 ssl http2;
    server_name pay.example.com;

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_request_buffering on;
        proxy_read_timeout 60s;
    }

    location /notify/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_buffering off;
    }
}
```

管理后台建议额外限制来源：

```nginx
location /admin/ {
    allow 10.0.0.0/8;
    deny all;
    proxy_pass http://127.0.0.1:8080;
}
```

## 7. 备份与恢复

SQLite 使用 WAL 模式。在线备份不要直接 `cp` 主库文件，使用：

```bash
sqlite3 /opt/pypay/var/pypay_service.db ".backup '/backup/pypay-$(date +%F-%H%M).db'"
```

或停服务后同时复制 `-wal`/`-shm`。建议每日备份并定期做恢复演练。

恢复：

```bash
sudo systemctl stop pypay-web pypay-worker
cp /backup/pypay-YYYY-MM-DD.db /opt/pypay/var/pypay_service.db
rm -f /opt/pypay/var/pypay_service.db-wal /opt/pypay/var/pypay_service.db-shm
sudo systemctl start pypay-web pypay-worker
```

`PYPAY_SERVICE_MASTER_KEY` 必须与备份时的数据库配套保存；丢失 master key 后，项目 API/通知密钥无法解密，只能为项目轮换凭证。渠道凭据在环境变量中，不在数据库里。

## 8. 监控与告警

至少监控：

| 指标 | 来源 | 告警建议 |
| --- | --- | --- |
| 服务存活 | `/ready` | 连续失败 3 次 |
| worker 存活 | 进程/日志心跳 | 5 分钟无 outbox 处理 |
| 死信事件 | 管理台“问题事件”/`outbox_events.status='dead'` | 任意新增 |
| 待确认退款 | `refunds.status='pending'` 超过 30 分钟 | 人工检查 |
| 待支付订单积压 | `payment_orders.status='pending'` 数量 | 突增 |
| 渠道失败率 | 日志 `provider_*` | 5xx 持续出现 |
| 业务通知失败 | `outbox_deliveries` 4xx/5xx | 4xx 立即处理 |

管理后台首页显示死信/问题事件和最近订单。数据库表 `audit_logs`、`outbox_deliveries`、`channel_notifications` 可用于对账。

日志不记录密钥、完整回调体或 API secret；排查时使用订单号、事件 ID、退款号。

## 9. 回滚

### 9.1 应用回滚（推荐）

1. 在业务网站侧停止创建新支付单（功能开关）。
2. **保持 pypay HTTP 和 worker 继续运行**，让已创建订单完成支付、过期、退款和通知投递。
3. 回滚 pypay 代码到上一版本（数据库结构保持向前兼容）。
4. 验证 `/ready`、worker 日志、死信数量。
5. 确认无在途订单后，再恢复业务下单或切换到旧支付路径。

数据库迁移一般向前兼容，**不要**在还有在途订单时执行 `migrate down`。

### 9.2 数据库回滚（谨慎）

`migrate down` 会删表，只适用于本地或确认无数据价值的场景：

```bash
# 先停止服务并备份
sudo systemctl stop pypay-web pypay-worker
sqlite3 /opt/pypay/var/pypay_service.db ".backup '/backup/before-down.db'"
python -m pypay_service migrate down --steps 1
```

生产回滚优先恢复备份，而不是执行 down 迁移。

### 9.3 回滚到旧支付方式

如果决定停用 pypay：

1. 业务网站停止新建 pypay 订单。
2. 继续运行 pypay，直到所有 `pending` 订单变为终态、所有 `refund` 完成。
3. 保留渠道回调域名和路由；不要提前删除 DNS 或反向代理配置。
4. 旧系统如有历史未完成订单，按其原商户配置继续处理（见 [migration.md](migration.md)）。
5. 确认无在途订单后再停止 worker 和 HTTP，保留数据库与备份至少一个对账周期。

## 10. 安全清单

- [ ] 生产 `ALLOW_HTTP_URLS=0`、`ALLOW_PRIVATE_URLS=0`、`ENABLE_DOCS=0`。
- [ ] master key 随机、离线备份、不在镜像/Git/日志中。
- [ ] 管理后台 HTTPS + 来源限制/VPN/SSO，密码 ≥8 位且非默认值。
- [ ] 渠道私钥文件权限 `0600`，只挂载到服务进程。
- [ ] 反向代理不缓存 `/notify/*`，不修改 body。
- [ ] 项目 `notify_url` 为公网 HTTPS，返回地址全部登记白名单。
- [ ] 定期检查 `dead` 事件和 pending 退款。
- [ ] 备份可用性演练。

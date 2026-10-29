# 验收记录与未完成联调

记录日期：2026-10-29。范围：本地开发、构建和模拟测试；**未部署生产、未修改生产商户配置、未发布软件包、未发起真实支付/退款、未读取或复制桌面真实凭据**。

## 1. 测试环境

- Python 3.14.7（代码兼容 Python 3.10+）
- FastAPI 0.162、Starlette TestClient、httpx 0.28、pytest 9.1、ruff
- SQLite（临时文件、WAL）
- 微信/支付宝适配器使用仓库内的临时 RSA 密钥和模拟 HTTP 传输，不访问真实网关
- 业务通知使用 `FakeNotifier`；渠道下单/查单/退款使用 `FakeGateway`
- 浏览器验证使用本地 `examples/service_demo.py` 和 headless Chromium，仅访问 `127.0.0.1`

## 2. 自动化结果

```text
$ .venv/bin/python -m pytest
279 passed, 1 warning in 6.01s

$ .venv/bin/ruff check src tests examples
All checks passed!

$ .venv/bin/ruff format --check src tests examples
82 files already formatted
```

唯一 warning 来自 FastAPI/Starlette TestClient 对 httpx 的弃用提示，不影响测试结果。

测试分层：

| 范围 | 数量（含参数化） | 说明 |
| --- | --- | --- |
| 原有 `pypay` 库与 demo 回归 | 191 | 微信/支付宝适配器、配置、模型、金额、FastAPI demo、端到端、异步客户端 |
| 新 `pypay_service` | 88 | 订单、认证、回调、outbox、退款、worker、管理台、客户端、迁移、安全、真实适配器 E2E、通知接收示例 |

## 3. 验收矩阵

| 验收要求 | 对应测试 | 结果 |
| --- | --- | --- |
| 两个项目各自商品/价格、互不串单 | `test_two_projects_create_orders_and_render_their_own_cashier`、`test_wechat_and_alipay_attempts_are_both_reachable` | 通过 |
| 重复业务单号幂等 | `test_duplicate_order_is_idempotent_and_conflicts_are_rejected` | 通过 |
| 金额/商品冲突拒绝 | 同上；`test_return_url_must_be_registered` | 通过 |
| 订单过期后不能支付并发过期事件 | `test_order_expiry_blocks_payment_and_emits_event`、`test_expiry_sweep_enqueues_one_event` | 通过 |
| 跨项目查询/退款拒绝 | `test_cross_project_queries_and_refunds_are_rejected`、`test_refund_query_cross_project_is_rejected` | 通过 |
| 签名错误拒绝 | `test_bad_signature_is_rejected`、`test_signature_error_is_rejected`、`test_verify_webhook_rejects_tampering_and_stale_timestamps` | 通过 |
| 金额不符拒绝并回滚 | `test_amount_mismatch_is_rejected_and_rolled_back` | 通过 |
| 重复回调不重复改状态/建事件 | `test_payment_success_is_idempotent_across_notification_ids`、`test_refund_duplicate_notification_does_not_double_count` | 通过 |
| 并发重复回调 | `test_concurrent_duplicate_callbacks_create_one_event`、`test_concurrent_different_notification_ids_still_create_one_event` | 通过 |
| 通知只发所属项目 | `test_event_is_sent_only_to_the_owning_project`、`test_notify_helpers_are_configured_for_the_right_project` | 通过 |
| 业务不可用/重试/退避 | `test_retry_backoff_and_worker_restart`、`test_retry_exhaustion_marks_dead_without_business_ack` | 通过 |
| worker 进程重启恢复 | `test_stale_sending_lease_is_recovered_after_restart`、`test_retry_backoff_and_worker_restart` | 通过 |
| 重复通知不重复发权益 | `test_receiver_verifies_and_grants_exactly_once`（示例接收器按 `event_id` 去重，权益只写一次） | 通过 |
| 支付创建超时后查单确认 | `test_payment_create_timeout_then_query_confirms` | 通过 |
| 退款超时后查单确认 | `test_refund_timeout_then_query_confirms` | 通过 |
| 退款明确失败 | `test_definite_provider_error_marks_refund_failed` | 通过 |
| 退款幂等与额度占用 | `test_refund_idempotency_key_replays_the_same_refund`、`test_refund_idempotency_key_header_is_supported`、`test_pending_refund_reserves_the_remaining_amount`、`test_refund_exceeding_remaining_amount_is_rejected` | 通过 |
| 全额退款状态 | `test_full_refund_marks_order_refunded`、`test_full_refund_before_payment_keeps_refunded_status` | 通过 |
| 关单与重复关单 | `test_close_pending_order_without_attempts`、`test_close_paid_order_is_rejected` | 通过 |
| 项目停用/渠道停用 | `test_disabled_project_cannot_call_the_api`、`test_channel_disabled_for_project_is_rejected`、`test_disabled_project_still_drains_existing_events` | 通过 |
| 收银台 token/CSRF/金额不可篡改 | `test_cashier_rejects_unknown_token_and_bad_csrf`、`test_cashier_never_accepts_an_amount_parameter`、`test_cashier_form_token_is_bound_to_the_token` | 通过 |
| 收银台“已支付待确认/已确认” | `test_cashier_shows_service_pending_then_acked`、`test_paid_cashier_does_not_keep_a_live_countdown` | 通过 |
| 管理台认证/角色/CSRF | `test_admin_requires_basic_auth`、`test_viewer_can_read_but_not_write`、`test_admin_post_requires_csrf`、`test_viewer_cannot_refund` | 通过 |
| 管理台项目/渠道/订单/人工操作 | `test_admin_can_create_update_rotate_and_disable_a_project`、`test_admin_can_manage_channel_accounts`、`test_admin_order_pages_and_manual_actions`、`test_dashboard_shows_dead_events` | 通过 |
| SSRF/密钥加密/基础 URL 校验 | `test_outbound_url_rejects_private_hosts_and_bad_schemes`、`test_dns_resolution_rejects_private_answers`、`test_secret_box_round_trip_and_wrong_key`、`test_service_base_url_must_be_root_https_or_http` | 通过 |
| 可回滚迁移 | `test_migrations_apply_and_roll_back`、`test_down_migration_is_reversible_with_data_loss` | 通过 |
| 业务客户端签名/错误/异步 | `test_sync_client_signs_every_request`、`test_async_client_signs_requests`、`test_client_raises_structured_errors`、`test_client_rejects_base_url_with_path` | 通过 |
| 真实适配器 + 模拟渠道 E2E（微信） | `test_real_wechat_native_callback_end_to_end` | 通过 |
| 真实适配器 + 模拟渠道 E2E（支付宝） | `test_real_alipay_page_pay_and_callback_end_to_end` | 通过 |
| 原有能力回归 | 191 个原有测试 | 通过 |

## 4. 真实适配器 E2E 覆盖内容

这两个测试使用仓库内生成的临时 RSA 密钥，走**现有 `pypay` 微信/支付宝适配器的真实构造逻辑**，但把 HTTP 传输替换为模拟响应：

微信：

- 服务端为 attempt 生成 `/notify/wechat/{account_id}`，并在下单请求中覆盖 notify URL。
- 构造 `wechatpayv3` 要求的签名回调，服务端验签并解密 resource。
- 校验金额、订单号、商户/appid，最终订单为 paid，事件进入 outbox。
- 未访问微信网关，未使用真实商户号。

支付宝：

- 服务端生成电脑网站支付表单，包含正确的 `notify_url`、`return_url`、金额和订单号。
- 模拟支付宝签名异步通知，服务端验签并更新订单。
- 未访问支付宝网关，未使用真实 APPID/私钥。

## 5. 手工浏览器验证

使用 `examples/service_demo.py`（假渠道 + 假 notifier）和 headless Chromium：

| 页面 | 验证内容 | 结果 |
| --- | --- | --- |
| 收银台（1280px） | 项目名、商品、金额 ¥99.00、业务单号、倒计时、微信/支付宝入口、支付进度 | 正常 |
| 收银台（390px） | 单列布局、无横向溢出、金额/按钮/进度可读 | 正常 |
| 收银台（320px） | 最小宽度下无横向溢出、渠道按钮和金额完整 | 正常 |
| 收银台（已支付） | “支付成功，服务已确认”、返回网站按钮、业务确认状态 | 正常 |
| 管理台总览 | 项目数、待支付/已支付订单数、死信数、最近订单、问题通知 | 正常 |
| 订单详情 | 快照、attempt、退款、事件/投递、重发入口 | 正常 |

验证中发现并修复：已支付订单仍显示倒计时，且到期后会反复自动刷新。现在只有 `pending` 订单渲染倒计时；终态订单显示静态有效期，轮询仍继续以更新业务确认状态。新增 `test_paid_cashier_does_not_keep_a_live_countdown` 防回归。

## 6. 明确未完成的真实商户联调

以下均**未进行**，上线前必须在测试商户环境逐项完成：

1. **真实微信支付**
   - 使用真实 mchid/appid 创建 Native 订单，确认 appid 与 mchid 绑定关系。
   - 验证 APIv3 密钥、商户证书序列号、微信支付公钥及公钥 ID 与商户平台一致。
   - 确认 Native 产品已开通、结算账户正常。
   - 用手机真实扫码支付，确认 `/notify/wechat/{account}` 公网可达、验签/解密成功。
   - 真实退款、退款查询、退款通知与账单对账。
2. **真实支付宝**
   - 确认应用已签约“电脑网站支付”，APPID、应用私钥、支付宝公钥匹配。
   - 真实打开表单、登录付款，确认异步通知和同步返回地址。
   - 真实退款、退款查询、seller_id 校验与账单对账。
3. **网络与部署**
   - 真实 HTTPS 证书、DNS、反向代理不修改 body、请求体上限、回调不被缓存。
   - 微信/支付宝出口网络连通性、超时和重试行为。
   - 生产进程守护、日志采集、告警通道。
4. **生产数据与容量**
   - SQLite 单机容量/并发压测；多实例或 PostgreSQL 迁移未做。
   - 长时间运行下的 WAL 增长、备份恢复演练。
5. **身份与运维**
   - 管理后台接入现有 SSO/VPN/IP 白名单；当前仅 Basic Auth。
   - 密钥托管/KMS、master key 轮换、渠道证书轮换流程。
   - 业务方 webhook 地址的真实可达性和故障演练。
6. **历史订单迁移**
   - 旧系统历史 `pending` 订单的实际清退、已支付未发货补发、旧退款结清。
   - 迁移方案已文档化（[migration.md](migration.md)），但未在真实站点执行。
7. **其他**
   - 支付宝证书模式未实现，首期仅公钥模式。
   - 未做真实浏览器端微信/支付宝完整支付流程（扫码、登录、风控）。
   - 未发布软件包、未部署生产、未修改任何生产商户配置。

## 7. 复现命令

```bash
uv venv
uv pip install -e ".[service,dev]"

.venv/bin/python -m pytest
.venv/bin/ruff check src tests examples
.venv/bin/ruff format --check src tests examples

# 本地演示（不接触真实渠道）
.venv/bin/python examples/service_demo.py
```

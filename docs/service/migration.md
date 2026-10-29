# 旧回调迁移方案

本文说明如何把当前“业务网站直接嵌入 `pypay`、自己接收微信/支付宝回调”的方式（README 的方式 A/示例 `examples/fastapi_app.py`），迁移到统一下单 + 统一收银台 + 统一通知的 `pypay_service`，同时保留历史未完成订单的处理路径。

核心原则：**新订单可以切换，历史在途订单不能切换。** 旧订单的渠道回调地址和商户配置在创建时已经绑定，必须继续由旧路径处理，直到全部到达终态。

## 1. 迁移前盘点

对每个站点记录：

| 项目 | 内容 |
| --- | --- |
| 站点/项目 | `Super-AIGC`、`super-aigc-digitalhuman` 等 |
| 旧代码位置 | 直接调用 `pypay` 的模块、回调路由、队列/发货逻辑 |
| 旧订单表 | 表名、主键、`out_trade_no` 格式、金额/状态字段 |
| 旧回调地址 | 例如 `https://api.site-a.example/pay/notify/wechat` |
| 商户账户 | mchid/appid 或 appid/seller_id；是否与将接入的新账户相同 |
| 在途订单 | `pending`、已支付但未发货、退款处理中 |
| 密钥来源 | 环境变量/密钥文件位置，是否有轮换计划 |

建议导出在途订单清单，字段至少包括：

```text
site, business_order_no, out_trade_no, channel, amount_fen, status,
created_at, expires_at, paid_at, fulfilled_at, refund_state
```

## 2. 目标状态

```text
新订单：
  业务网站后端 ──HMAC──► pypay_service ──► 渠道

历史订单：
  渠道回调 ──► 业务网站旧回调路由（保持原样）──► 旧订单表/旧发货逻辑
```

两套路径**并行存在**，由站点级功能开关决定新订单走哪边。

## 3. 准备 pypay_service

1. 部署服务与 worker，完成 [部署文档](deployment.md) 的配置。
2. 为每个站点创建项目：

   ```bash
   python -m pypay_service create-project \
     --id super-aigc \
     --name "Super-AIGC" \
     --notify-url https://www.super-aigc.example/pypay/notify \
     --return-url https://www.super-aigc.example/pay/success \
     --return-url https://www.super-aigc.example/pay/cancel \
     --channel wechat=wechat-default \
     --channel alipay=alipay-default
   ```

3. 在业务网站实现新的 webhook 接收器（可按 [`examples/business_receiver.py`](../../examples/business_receiver.py) 改造），先只记录事件、不发放权益。
4. 用**测试项目/测试商户**跑通模拟支付和签名通知。没有测试商户时，使用 `examples/service_demo.py` 的假渠道做流程验证。
5. 确认新接收器的 `notify_url` 公网可达、HTTPS 有效、返回 2xx。

## 4. 灰度切换

### 4.1 站点级功能开关

业务网站增加开关，例如：

```python
USE_UNIFIED_PAYMENTS = os.environ.get("USE_UNIFIED_PAYMENTS") == "1"
```

下单入口：

```python
if USE_UNIFIED_PAYMENTS:
    payment = pay_client.create_payment(...)
    return redirect(payment["cashier_url"])
else:
    return legacy_start_payment(order)   # 原 pypay 直连流程
```

注意：

- 开关只影响**新创建**的订单，不影响旧订单查询/回调。
- 新旧订单共用业务订单表，但支付标识不同：旧流程用旧 `out_trade_no`，新流程用 pypay 的 `payment_id`。业务订单上增加 `payment_id` 字段区分。
- 不要复用或改写旧订单的 `out_trade_no`。
- 回滚开关必须能立即生效，不依赖重启（或至少能快速重启）。

### 4.2 保留旧回调路由

迁移期间**不要删除**旧路由、旧渠道配置或旧密钥：

```python
@app.post("/pay/notify/wechat")     # 旧路径，继续服务历史订单
async def legacy_wechat_notify(...):
    ...  # 原逻辑保持不变

@app.post("/pay/notify/alipay")
async def legacy_alipay_notify(...):
    ...
```

原因：

- 微信 Native 的 `notify_url` 在创建订单时写入渠道；历史订单仍会回调旧地址。
- 支付宝的 `notify_url` 也是按请求传入；历史订单同样绑定旧地址。
- 渠道会重试未确认的通知，旧路由可能在新订单切换后数小时甚至更久仍有流量。

旧的商户凭据也必须保留，直到历史订单全部结清。

### 4.3 商户账户

- 新旧流程可以使用同一个商户账户，但必须保证旧回调地址仍可访问。
- 如果迁移到新商户账户，新订单用新账户；旧订单必须继续在旧账户下退款和对账，**不能**用新账户处理旧订单。
- 不要在同一笔在途订单上更换商户号、appid、APIv3 密钥或证书。

## 5. 历史订单清退

按以下顺序，不要跳步：

1. **停止旧流程创建新订单**：开关切到新流程。
2. **继续运行旧回调路由**：保持至少 24 小时，直到确认没有新的旧订单回调。
3. **主动查单清退**：用旧代码的 `query_payment` 扫描旧 `pending` 订单：
   - 已支付 → 走旧发货逻辑（保持幂等）。
   - 已关闭/失败/过期 → 标记终态。
   - 查单超时 → 保持 pending，稍后重试，不能直接判失败。
4. **处理旧退款**：旧退款继续用旧账户和旧 `query_refund`，直到全部 `succeeded/failed/closed`。
5. **对账**：以渠道账单/查询结果为准，核对旧订单表和业务权益表。
6. **确认零在途**：

   ```sql
   -- 旧订单表：应为 0
   SELECT COUNT(*) FROM old_orders WHERE status = 'pending';

   -- 已支付未发货：应为 0 或已人工处理
   SELECT COUNT(*) FROM old_orders WHERE status = 'paid' AND fulfilled_at IS NULL;
   ```

7. **观察期**：旧回调路由至少保留一个渠道重试周期（建议 ≥24–48 小时），确认无流量后再下线。
8. **归档**：导出旧订单、回调记录、退款记录，保留至少一个财务对账周期。

### 5.1 已支付但未发货的旧订单

这类订单最容易在迁移中丢失：

- 逐笔用渠道查询确认支付状态。
- 在业务库中按业务单号幂等补发权益。
- 不要依赖“用户重新支付”解决，避免重复扣款。
- 补发后记录审计（操作人、时间、依据）。

### 5.2 旧回调能否转发给新服务？

不建议。原因：

- 新服务没有旧 `out_trade_no` 的订单映射，无法关联。
- 旧回调签名对应旧商户配置；转发会引入二次验签和重放问题。
- 旧路由已经具备完整处理逻辑，保留它更简单、更可回滚。

如果确实要统一审计，可以在旧回调处理成功后写一条只读记录到新服务的对账表，但不要让它改变新服务订单状态。

## 6. 回滚

迁移期间任何一步出问题，按以下顺序回滚：

1. 业务网站开关切回旧流程，恢复旧下单。
2. **保持 pypay_service HTTP 和 worker 运行**：已经创建的新订单仍可能支付、过期、退款，回调仍在服务上。
3. 保持旧回调路由和旧商户配置运行：两套路径同时存在是迁移期的正常状态。
4. 修复问题后重新灰度，或确认无新订单后按 [部署回滚](deployment.md#9-回滚) 清退服务。
5. 不要删除 pypay 数据库；已创建订单、退款、事件和审计需要保留。

只有在新旧两套都确认零在途、且保留期结束后，才停止旧路由或服务。

## 7. 检查清单

- [ ] 每个站点有独立 `project_id`、凭证和 `notify_url`。
- [ ] 新 webhook 接收器验签、按 `event_id` 幂等、事务提交后返回 2xx。
- [ ] 新流程金额只来自业务后端，浏览器不能改金额。
- [ ] 旧回调路由、旧商户凭据、旧密钥保留。
- [ ] 新旧订单在业务库中可区分（`payment_id` / 旧 `out_trade_no`）。
- [ ] 历史 `pending` 订单已查单清退。
- [ ] 已支付未发货订单已人工核对并幂等补发。
- [ ] 旧退款已结清。
- [ ] 对账完成，差异有记录。
- [ ] 回滚开关经过演练。
- [ ] 旧路由下线前确认一个完整重试周期无流量。

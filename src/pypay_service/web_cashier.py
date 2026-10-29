from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from pypay import Channel

from .enums import AttemptStatus, OrderStatus
from .models import ChannelAccount, PaymentAttempt, PaymentOrder, Project
from .web_common import (
    CASHIER_CSS,
    cashier_header,
    e,
    fmt_dt,
    money,
    page_layout,
    status_pill,
)

_ORDER_LABELS = {
    OrderStatus.PENDING: "等待支付",
    OrderStatus.PAID: "支付成功",
    OrderStatus.CLOSED: "订单已关闭",
    OrderStatus.EXPIRED: "订单已过期",
    OrderStatus.REFUNDED: "已退款",
    OrderStatus.FAILED: "支付失败",
}
_ATTEMPT_LABELS = {
    AttemptStatus.PENDING: "待支付",
    AttemptStatus.PAID: "已支付",
    AttemptStatus.CLOSED: "已关闭",
    AttemptStatus.FAILED: "失败",
    AttemptStatus.REFUNDED: "已退款",
}
_CHANNEL_LABELS = {
    Channel.WECHAT: ("微", "微信支付", "使用微信扫一扫"),
    Channel.ALIPAY: ("支", "支付宝", "跳转支付宝收银台"),
}


def _attempt_for(attempts: Iterable[PaymentAttempt], channel: Channel) -> PaymentAttempt | None:
    for attempt in attempts:
        if attempt.channel is channel:
            return attempt
    return None


def _primary_attempt(
    attempts: list[PaymentAttempt], selected_attempt_id: str | None
) -> PaymentAttempt | None:
    if selected_attempt_id:
        for attempt in attempts:
            if attempt.id == selected_attempt_id:
                return attempt
    for attempt in reversed(attempts):
        if attempt.status is AttemptStatus.PENDING and (
            attempt.qr_code_url or attempt.form_html or attempt.redirect_url
        ):
            return attempt
    for attempt in attempts:
        if attempt.status is AttemptStatus.PAID:
            return attempt
    return attempts[-1] if attempts else None


def _channel_tiles(
    order: PaymentOrder,
    channels: list[ChannelAccount],
    attempts: list[PaymentAttempt],
    form_token: str,
) -> str:
    tiles: list[str] = []
    for account in channels:
        glyph, label, hint = _CHANNEL_LABELS.get(
            account.channel, ("？", account.channel.value, "支付")
        )
        attempt = _attempt_for(attempts, account.channel)
        disabled = order.status is not OrderStatus.PENDING
        if attempt is not None:
            hint = _ATTEMPT_LABELS.get(attempt.status, attempt.status.value)
            if attempt.status in {AttemptStatus.FAILED, AttemptStatus.CLOSED}:
                disabled = True
        if disabled:
            tiles.append(
                f'<button class="channel" type="button" disabled>'
                f'<span class="icon">{e(glyph)}</span><span><strong>{e(label)}</strong>'
                f"<span>{e(hint)}</span></span></button>"
            )
            continue
        tiles.append(
            f'<form method="post" action="/pay/{e(order.cashier_token)}/start">'
            f'<input type="hidden" name="csrf_token" value="{e(form_token)}">'
            f'<input type="hidden" name="channel" value="{e(account.channel.value)}">'
            f'<button class="channel" type="submit">'
            f'<span class="icon">{e(glyph)}</span><span><strong>{e(label)}</strong>'
            f"<span>{e(hint)}</span></span></button></form>"
        )
    return '<div class="channel-grid">' + "".join(tiles) + "</div>"


def _attempt_panel(order: PaymentOrder, attempt: PaymentAttempt | None, form_token: str) -> str:
    if attempt is None:
        return ""
    if attempt.status is not AttemptStatus.PENDING:
        return ""
    token = order.cashier_token
    if attempt.qr_code_url:
        qr = f"/pay/{e(token)}/qr.svg?attempt={e(attempt.id)}"
        return f"""
<div class="qr-panel">
  <div class="qr-frame"><img src="{qr}" width="166" height="166" alt="微信支付二维码"></div>
  <div>
    <div class="pill paid">微信扫码支付</div>
    <p style="margin:12px 0 6px">请使用微信扫描左侧二维码完成支付。</p>
    <p class="small muted" style="margin:0">二维码仅对当前订单有效。支付结果会自动刷新。</p>
    <p class="small mono" style="margin:10px 0 0; word-break:break-all">{e(attempt.qr_code_url)}</p>
  </div>
</div>"""
    if attempt.form_html:
        form_url = f"/pay/{e(token)}/alipay-form?attempt={e(attempt.id)}"
        return f"""
<div class="form-panel">
  <div class="pill pending">支付宝收银台</div>
  <p style="margin:12px 0">支付宝网页支付需要在新窗口打开收银台。</p>
  <a class="btn accent" href="{form_url}" target="_blank" rel="noopener noreferrer">前往支付宝</a>
</div>"""
    if attempt.redirect_url:
        return f"""
<div class="form-panel">
  <div class="pill pending">支付跳转</div>
  <p style="margin:12px 0">支付渠道要求跳转到外部收银台。</p>
  <a class="btn accent" href="{e(attempt.redirect_url)}" rel="noopener noreferrer">继续支付</a>
</div>"""
    return f"""
<div class="warn-box">
  <strong>支付信息尚未就绪</strong>
  <p class="small" style="margin:6px 0 12px">渠道响应超时或尚未返回二维码。请重试获取，不要重复创建新订单。</p>
  <form method="post" action="/pay/{e(token)}/start">
    <input type="hidden" name="csrf_token" value="{e(form_token)}">
    <input type="hidden" name="channel" value="{e(attempt.channel.value)}">
    <button class="btn" type="submit">重新获取支付信息</button>
  </form>
</div>"""


def _state_banner(order: PaymentOrder, service_ack: str | None) -> str:
    if order.status is OrderStatus.PAID:
        if service_ack == "acked":
            return (
                '<div class="state-banner success"><div><h3>支付成功，服务已确认</h3>'
                "<p>业务系统已确认收到支付结果。可以返回网站继续使用服务。</p></div></div>"
            )
        return (
            '<div class="state-banner success"><div><h3>支付成功，服务开通中</h3>'
            "<p>支付已完成，正在等待业务系统确认。此页面会自动刷新。</p></div></div>"
        )
    if order.status is OrderStatus.REFUNDED:
        return (
            '<div class="state-banner closed"><div><h3>订单已退款</h3>'
            f"<p>已退款 {money(order.refunded_amount_fen)}，如有疑问请联系项目客服。</p></div></div>"
        )
    if order.status is OrderStatus.EXPIRED:
        return (
            '<div class="state-banner closed"><div><h3>订单已过期</h3>'
            "<p>请返回网站重新下单。未支付的订单不会扣款。</p></div></div>"
        )
    if order.status is OrderStatus.CLOSED:
        return (
            '<div class="state-banner closed"><div><h3>订单已关闭</h3>'
            "<p>该订单已关闭，未支付不会扣款。</p></div></div>"
        )
    if order.status is OrderStatus.FAILED:
        return (
            '<div class="state-banner closed"><div><h3>支付失败</h3>'
            "<p>请返回网站重新发起支付或更换支付方式。</p></div></div>"
        )
    return (
        '<div class="state-banner pending"><div><h3>等待支付</h3>'
        "<p>请选择支付方式并完成付款。支付结果以服务器通知或主动查单为准。</p></div></div>"
    )


def _timeline(order: PaymentOrder, service_ack: str | None) -> str:
    paid = order.paid_at is not None
    acked = service_ack == "acked"
    refunded = order.status is OrderStatus.REFUNDED
    items = [
        ("done", "订单创建", fmt_dt(order.created_at)),
        ("done" if paid else "", "支付完成", fmt_dt(order.paid_at) if paid else "等待支付"),
        (
            "done" if acked else ("warn" if paid else ""),
            "业务确认",
            "已确认" if acked else ("通知中" if paid else "待支付后通知"),
        ),
    ]
    if refunded:
        items.append(("done", "退款完成", money(order.refunded_amount_fen)))
    lis = "".join(
        f'<li><span class="dot {cls}"></span><div><strong>{e(title)}</strong>'
        f'<div class="small muted">{e(detail)}</div></div></li>'
        for cls, title, detail in items
    )
    return f'<ul class="timeline">{lis}</ul>'


def render_cashier(
    *,
    order: PaymentOrder,
    project: Project,
    attempts: list[PaymentAttempt],
    channels: list[ChannelAccount],
    service_ack: str | None,
    form_token: str,
    nonce: str,
    now: datetime,
    selected_attempt_id: str | None = None,
) -> str:
    primary = _primary_attempt(attempts, selected_attempt_id)
    return_url = order.return_url if order.paid_at is not None else None
    return_block = ""
    if return_url:
        return_block = (
            f'<a class="btn accent" href="{e(return_url)}" rel="noopener noreferrer">返回网站</a>'
        )
    paid_attempt = next(
        (attempt for attempt in attempts if attempt.status is AttemptStatus.PAID), primary
    )
    payment_no = paid_attempt.out_trade_no if paid_attempt else "尚未创建"
    is_pending = order.status is OrderStatus.PENDING
    remaining = max(0, int((order.expires_at - now).total_seconds()))
    if is_pending:
        validity = (
            f'<span class="countdown" id="countdown">'
            f"剩余 {remaining // 60} 分 {remaining % 60:02d} 秒</span>"
        )
        expires_attr = int(order.expires_at.timestamp() * 1000)
    else:
        validity = e(fmt_dt(order.expires_at))
        expires_attr = 0
    state = f"{order.status.value}:{service_ack or ''}"
    body = f"""
{cashier_header(project.name, project.id)}
<div class="cashier-grid" data-status-url="/pay/{e(order.cashier_token)}/status"
     data-state="{e(state)}" data-expires="{expires_attr}">
  <section class="card pay-card" aria-labelledby="subject">
    <div class="project-line">
      {status_pill(order.status.value, _ORDER_LABELS.get(order.status))}
      <span>{e(project.name)}</span>
    </div>
    <h1 class="subject" id="subject">{e(order.subject)}</h1>
    <div class="amount">{money(order.amount_fen)} <small>CNY</small></div>
    <dl class="meta-row">
      <div><dt>业务订单号</dt><dd class="mono">{e(order.business_order_no)}</dd></div>
      <div><dt>支付单号</dt><dd class="mono">{e(payment_no)}</dd></div>
      <div><dt>有效期</dt><dd>{validity}</dd></div>
    </dl>
    <div class="perforation" aria-hidden="true"></div>
    <div class="sr-live" aria-live="polite">{_state_banner(order, service_ack)}</div>
    {_channel_tiles(order, channels, attempts, form_token) if order.status is OrderStatus.PENDING else ""}
    <div style="margin-top:18px">{_attempt_panel(order, primary, form_token)}</div>
    <div class="btn-row" style="margin-top:20px">{return_block}</div>
    <p class="small muted" style="margin:18px 0 0">
      支付结果以服务器回调或主动查单为准；浏览器跳转和页面提示不作为支付成功依据。
    </p>
  </section>
  <aside class="card side-card" aria-label="支付进度">
    <h2>支付进度</h2>
    <p class="small muted" style="margin:0">订单金额与商品信息由业务系统锁定，浏览器无法修改。</p>
    {_timeline(order, service_ack)}
    <div style="margin-top:18px; padding-top:16px; border-top:1px dashed var(--line)">
      <div class="small muted">创建时间</div>
      <div class="small mono">{e(fmt_dt(order.created_at))}</div>
      <div class="small muted" style="margin-top:8px">过期时间</div>
      <div class="small mono">{e(fmt_dt(order.expires_at))}</div>
    </div>
  </aside>
</div>
<script nonce="{e(nonce)}">
(function () {{
  const root = document.querySelector('[data-status-url]');
  if (!root) return;
  const statusUrl = root.getAttribute('data-status-url');
  const expected = root.getAttribute('data-state');
  const expires = Number(root.getAttribute('data-expires') || 0);
  const countdown = document.getElementById('countdown');
  let reloading = false;
  function tick() {{
    if (!countdown || !expires) return;
    const left = Math.max(0, Math.floor((expires - Date.now()) / 1000));
    const m = Math.floor(left / 60);
    const s = String(left % 60).padStart(2, '0');
    countdown.textContent = left > 0 ? `剩余 ${{m}} 分 ${{s}} 秒` : '已过期';
    if (left === 0 && !reloading) {{
      reloading = true;
      setTimeout(() => location.reload(), 1200);
    }}
  }}
  tick();
  setInterval(tick, 1000);
  setInterval(async () => {{
    if (reloading) return;
    try {{
      const response = await fetch(statusUrl, {{headers: {{Accept: 'application/json'}}, cache: 'no-store'}});
      if (!response.ok) return;
      const data = await response.json();
      const state = `${{data.status}}:${{data.service_ack_status || ''}}`;
      if (state !== expected) {{
        reloading = true;
        location.reload();
      }}
    }} catch (error) {{}}
  }}, 4000);
}})();
</script>
"""
    return page_layout(
        f"{project.name} · {order.subject}",
        body,
        nonce=nonce,
        css=CASHIER_CSS,
    )


def render_return_page(order: PaymentOrder, project: Project, nonce: str) -> str:
    back = f"/pay/{order.cashier_token}"
    body = f"""
{cashier_header(project.name, project.id)}
<section class="card pay-card" style="max-width:680px;margin:8vh auto 0">
  <div class="pill pending">结果确认中</div>
  <h1 class="subject" style="font-size:clamp(26px,4vw,38px)">支付结果待确认</h1>
  <p>浏览器跳转只用于展示结果，不能作为支付成功依据。请等待服务器回调或主动查单确认。</p>
  <dl class="meta-row">
    <div><dt>业务订单号</dt><dd class="mono">{e(order.business_order_no)}</dd></div>
    <div><dt>金额</dt><dd class="mono">{money(order.amount_fen)}</dd></div>
  </dl>
  <div class="btn-row" style="margin-top:24px">
    <a class="btn" href="{e(back)}">查看订单状态</a>
    {f'<a class="btn secondary" href="{e(order.return_url)}" rel="noopener noreferrer">返回网站</a>' if order.return_url else ""}
  </div>
</section>
"""
    return page_layout(f"{project.name} · 支付结果", body, nonce=nonce, css=CASHIER_CSS)


def render_cashier_error(message: str, nonce: str, title: str = "无法打开收银台") -> str:
    body = f"""
<header class="masthead"><div class="brand"><span class="brand-mark">PP</span>
<span class="brand-name">pypay</span></div></header>
<section class="card pay-card" style="max-width:620px;margin:6vh auto 0">
  <div class="pill failed">链接不可用</div>
  <h1 class="subject" style="font-size:clamp(26px,4vw,38px)">{e(title)}</h1>
  <p class="muted">{e(message)}</p>
</section>
"""
    return page_layout(title, body, nonce=nonce, css=CASHIER_CSS)

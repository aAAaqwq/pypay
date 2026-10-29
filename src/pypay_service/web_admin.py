from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .docs_view import DocEntry
from .enums import AttemptStatus, OrderStatus
from .models import (
    AuditEntry,
    ChannelAccount,
    CreatedProject,
    DeliveryRecord,
    OutboxEvent,
    PaymentAttempt,
    PaymentOrder,
    Project,
    Refund,
)
from .timeutil import from_db
from .web_common import (
    ADMIN_CSS,
    LOGIN_CSS,
    e,
    fmt_dt,
    money,
    page_layout,
    status_pill,
)

_ORDER_LABELS = {
    OrderStatus.PENDING: "等待支付",
    OrderStatus.PAID: "已支付",
    OrderStatus.CLOSED: "已关闭",
    OrderStatus.EXPIRED: "已过期",
    OrderStatus.REFUNDED: "已退款",
    OrderStatus.FAILED: "失败",
}
_ATTEMPT_LABELS = {
    AttemptStatus.PENDING: "待支付",
    AttemptStatus.PAID: "已支付",
    AttemptStatus.CLOSED: "已关闭",
    AttemptStatus.FAILED: "失败",
    AttemptStatus.REFUNDED: "已退款",
}


def _csrf(csrf: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{e(csrf)}">'


def _flash(message: str | None, kind: str = "notice") -> str:
    if not message:
        return ""
    css = "notice-box" if kind == "notice" else ("error-box" if kind == "error" else "warn-box")
    return f'<div class="{css}" style="margin-bottom:18px">{e(message)}</div>'


def _head(title: str, subtitle: str = "", actions: str = "") -> str:
    sub = f'<p class="muted" style="margin:6px 0 0">{e(subtitle)}</p>' if subtitle else ""
    return f'<div class="admin-head"><div><h1>{e(title)}</h1>{sub}</div><div class="btn-row">{actions}</div></div>'


def _admin_page(
    title: str,
    body: str,
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    active: str,
) -> str:
    return page_layout(
        f"{title} · pypay 管理台",
        body,
        nonce=nonce,
        css=ADMIN_CSS,
        admin=True,
        principal=principal,
        active=active,
        csrf=csrf,
    )


def _channel_selects(accounts: list[ChannelAccount], selected: dict[str, str]) -> str:
    by_channel: dict[str, list[ChannelAccount]] = {}
    for account in accounts:
        by_channel.setdefault(account.channel.value, []).append(account)
    blocks: list[str] = []
    for channel in ("wechat", "alipay"):
        options = ['<option value="">不启用</option>']
        for account in by_channel.get(channel, []):
            if not account.enabled:
                continue
            checked = " selected" if selected.get(channel) == account.id else ""
            options.append(
                f'<option value="{e(account.id)}"{checked}>{e(account.name)} '
                f"({e(account.id)})</option>"
            )
        label = "微信支付" if channel == "wechat" else "支付宝"
        blocks.append(
            f'<label class="field"><span>{e(label)}账户</span>'
            f'<select name="account_{e(channel)}">{"".join(options)}</select></label>'
        )
    return "".join(blocks)


def render_dashboard(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    stats: dict[str, Any],
    recent_orders: list[PaymentOrder],
    problem_events: list[OutboxEvent],
) -> str:
    kpis = [
        ("项目", stats.get("project_count", 0)),
        ("待支付订单", stats.get("pending_orders", 0)),
        ("已支付订单", stats.get("paid_orders", 0)),
        ("死信通知", stats.get("dead_events", 0)),
    ]
    kpi_html = "".join(
        f'<div class="card kpi"><div class="k">{e(k)}</div><div class="v">{e(v)}</div></div>'
        for k, v in kpis
    )
    order_rows = "".join(
        f"<tr><td class='mono small'>{e(fmt_dt(o.created_at))}</td>"
        f"<td>{e(o.project_id)}</td>"
        f"<td class='mono small'><a href='/admin/orders/{e(o.id)}'>{e(o.business_order_no)}</a></td>"
        f"<td>{e(o.subject)}</td><td class='mono'>{money(o.amount_fen)}</td>"
        f"<td>{status_pill(o.status.value, _ORDER_LABELS.get(o.status))}</td></tr>"
        for o in recent_orders
    )
    event_rows = "".join(
        f"<tr><td class='mono small'>{e(ev.event_id)}</td>"
        f"<td><a href='/admin/orders/{e(ev.order_id)}'>{e(ev.event_type.value)}</a></td>"
        f"<td>{status_pill(ev.status.value)}</td>"
        f"<td class='small'>{e(ev.last_error or '')}</td></tr>"
        for ev in problem_events
    )
    body = f"""
{_head("总览", "支付、通知与业务确认状态分开记录")}
<div class="kpi-grid">{kpi_html}</div>
<section class="card section">
  <h2>最近订单</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>创建</th><th>项目</th><th>业务单号</th><th>商品</th><th>金额</th><th>状态</th></tr></thead>
    <tbody>{order_rows or '<tr><td colspan="6" class="muted">暂无订单</td></tr>'}</tbody>
  </table></div>
</section>
<section class="card section">
  <h2>需要关注的通知</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>事件</th><th>订单</th><th>投递</th><th>最近错误</th></tr></thead>
    <tbody>{event_rows or '<tr><td colspan="4" class="muted">没有失败或重试中的通知</td></tr>'}</tbody>
  </table></div>
</section>
"""
    return _admin_page(
        "总览", body, principal=principal, nonce=nonce, csrf=csrf, active="dashboard"
    )


def render_projects(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    projects: list[Project],
    flash: str | None = None,
) -> str:
    rows = "".join(
        f"<tr><td class='mono'>{e(p.id)}</td><td>{e(p.name)}</td>"
        f"<td>{status_pill('paid' if p.status.value == 'active' else 'failed', '启用' if p.status.value == 'active' else '停用')}</td>"
        f"<td class='small mono'>{e(p.notify_url)}</td>"
        f"<td class='small'>{e(fmt_dt(p.created_at))}</td>"
        f"<td><a class='btn secondary' href='/admin/projects/{e(p.id)}'>查看</a></td></tr>"
        for p in projects
    )
    body = f"""
{_head("项目", "每个业务网站一个项目，独立凭证与通知地址", '<a class="btn" href="/admin/projects/new">新建项目</a>')}
{_flash(flash)}
<section class="card section">
  <div class="table-wrap"><table>
    <thead><tr><th>项目 ID</th><th>名称</th><th>状态</th><th>通知地址</th><th>创建时间</th><th></th></tr></thead>
    <tbody>{rows or '<tr><td colspan="6" class="muted">还没有项目</td></tr>'}</tbody>
  </table></div>
</section>
"""
    return _admin_page("项目", body, principal=principal, nonce=nonce, csrf=csrf, active="projects")


def render_project_new(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    accounts: list[ChannelAccount],
    error: str | None = None,
    form: dict[str, str] | None = None,
) -> str:
    form = form or {}
    if not any(account.enabled for account in accounts):
        body = f"""
{_head("新建项目")}
<div class="warn-box">还没有启用的渠道账户。请先到 <a href="/admin/channels">渠道账户</a> 添加并启用账户。</div>
"""
        return _admin_page(
            "新建项目", body, principal=principal, nonce=nonce, csrf=csrf, active="projects"
        )
    channels_html = _channel_selects(accounts, {})
    body = f"""
{_head("新建项目", "创建后会显示一次 API 密钥与通知密钥", '<a class="btn secondary" href="/admin/projects">返回</a>')}
{_flash(error, "error")}
<section class="card section">
  <form method="post" action="/admin/projects">
    {_csrf(csrf)}
    <div class="form-grid">
      <label class="field"><span>项目 ID</span>
        <input type="text" name="project_id" required pattern="[a-z][a-z0-9_-]{{2,31}}"
               value="{e(form.get("project_id", ""))}" placeholder="super-aigc"></label>
      <label class="field"><span>项目名称</span>
        <input type="text" name="name" required maxlength="100"
               value="{e(form.get("name", ""))}" placeholder="Super-AIGC"></label>
      <label class="field wide"><span>业务通知地址（服务器回调，必须可公网访问）</span>
        <input type="url" name="notify_url" required
               value="{e(form.get("notify_url", ""))}" placeholder="https://api.example.com/pypay/notify"></label>
      <label class="field wide"><span>允许的支付后返回地址（每行一个）</span>
        <textarea name="return_urls" placeholder="https://www.example.com/pay/success">{e(form.get("return_urls", ""))}</textarea>
        <span class="help">浏览器跳转只用于交互，不作为支付成功依据。</span></label>
      <div class="wide"><div class="help" style="margin-bottom:8px">可用支付渠道（每个渠道最多一个商户账户）</div>{channels_html}</div>
    </div>
    <button class="btn" type="submit">创建项目</button>
  </form>
</section>
"""
    return _admin_page(
        "新建项目", body, principal=principal, nonce=nonce, csrf=csrf, active="projects"
    )


def render_project_created(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    created: CreatedProject,
    base_url: str,
) -> str:
    project = created.project
    snippet = (
        f"PYPAY_BASE_URL={base_url}\n"
        f"PYPAY_KEY_ID={project.api_key_id}\n"
        f"PYPAY_API_SECRET={created.api_secret}\n"
        f"PYPAY_NOTIFY_SECRET={created.notify_secret}\n"
    )
    body = f"""
{_head(project.name, "项目已创建；密钥只显示这一次")}
<div class="warn-box" style="margin-bottom:18px">
  请立即把下列凭据保存到业务网站的密钥管理系统中。离开本页后将无法再次查看明文。
</div>
<section class="card section">
  <h2>接入凭据</h2>
  <div class="small muted">API Key ID</div><div class="credential">{e(project.api_key_id)}</div>
  <div class="small muted" style="margin-top:12px">API Secret</div><div class="credential">{e(created.api_secret)}</div>
  <div class="small muted" style="margin-top:12px">通知签名密钥</div><div class="credential">{e(created.notify_secret)}</div>
  <div class="small muted" style="margin-top:12px">服务地址</div><div class="credential">{e(base_url)}</div>
  <h2 style="margin-top:22px">.env 片段</h2>
  <pre class="credential">{e(snippet)}</pre>
  <div class="btn-row" style="margin-top:18px">
    <a class="btn" href="/admin/projects/{e(project.id)}">进入项目</a>
    <a class="btn secondary" href="/admin/projects">返回项目列表</a>
  </div>
</section>
"""
    return _admin_page(
        "项目已创建", body, principal=principal, nonce=nonce, csrf=csrf, active="projects"
    )


def render_project_detail(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    project: Project,
    accounts: list[ChannelAccount],
    selected: dict[str, str],
    orders: list[PaymentOrder],
    rotated: Any | None = None,
    flash: str | None = None,
    error: str | None = None,
) -> str:
    is_admin = getattr(principal, "can_write", False)
    order_rows = "".join(
        f"<tr><td class='mono small'><a href='/admin/orders/{e(o.id)}'>{e(o.business_order_no)}</a></td>"
        f"<td>{e(o.subject)}</td><td class='mono'>{money(o.amount_fen)}</td>"
        f"<td>{status_pill(o.status.value, _ORDER_LABELS.get(o.status))}</td>"
        f"<td class='small'>{e(fmt_dt(o.created_at))}</td></tr>"
        for o in orders
    )
    secrets_block = ""
    if rotated is not None:
        parts = []
        if rotated.api_secret:
            parts.append(
                f'<div class="small muted">新的 API Secret（只显示一次）</div>'
                f'<div class="credential">{e(rotated.api_secret)}</div>'
            )
        if rotated.notify_secret:
            parts.append(
                f'<div class="small muted" style="margin-top:12px">新的通知签名密钥（只显示一次）</div>'
                f'<div class="credential">{e(rotated.notify_secret)}</div>'
            )
        secrets_block = f'<div class="warn-box" style="margin-bottom:18px">{"".join(parts)}</div>'
    actions = ""
    if is_admin:
        actions = f"""
<form class="inline-form" method="post" action="/admin/projects/{e(project.id)}/rotate">
  {_csrf(csrf)}<input type="hidden" name="target" value="api">
  <button class="btn secondary" type="submit">轮换 API 密钥</button>
</form>
<form class="inline-form" method="post" action="/admin/projects/{e(project.id)}/rotate">
  {_csrf(csrf)}<input type="hidden" name="target" value="notify">
  <button class="btn secondary" type="submit">轮换通知密钥</button>
</form>
<form class="inline-form" method="post" action="/admin/projects/{e(project.id)}/status">
  {_csrf(csrf)}<input type="hidden" name="enabled" value="{"0" if project.status.value == "active" else "1"}">
  <button class="btn {"danger" if project.status.value == "active" else "accent"}" type="submit">
    {"停用项目" if project.status.value == "active" else "启用项目"}
  </button>
</form>"""
    channels_html = _channel_selects(accounts, selected)
    settings = ""
    if is_admin:
        settings = f"""
<section class="card section">
  <h2>项目设置</h2>
  <form method="post" action="/admin/projects/{e(project.id)}/settings">
    {_csrf(csrf)}
    <div class="form-grid">
      <label class="field"><span>项目名称</span><input type="text" name="name" required value="{e(project.name)}"></label>
      <label class="field"><span>状态</span><input type="text" value="{e(project.status.value)}" disabled></label>
      <label class="field wide"><span>业务通知地址</span><input type="url" name="notify_url" required value="{e(project.notify_url)}"></label>
      <label class="field wide"><span>允许的支付后返回地址（每行一个）</span>
        <textarea name="return_urls">{e(chr(10).join(project.allowed_return_urls))}</textarea></label>
      <div class="wide">{channels_html}</div>
    </div>
    <button class="btn" type="submit">保存设置</button>
  </form>
</section>"""
    body = f"""
{_head(project.name, f"项目 ID：{project.id}", actions)}
{_flash(flash)}
{_flash(error, "error")}
{secrets_block}
<section class="card section">
  <h2>接入信息</h2>
  <div class="form-grid">
    <div><div class="small muted">API Key ID</div><div class="mono">{e(project.api_key_id)}</div></div>
    <div><div class="small muted">状态</div>{status_pill("paid" if project.status.value == "active" else "failed", "启用" if project.status.value == "active" else "停用")}</div>
    <div class="wide"><div class="small muted">业务通知地址</div><div class="mono small">{e(project.notify_url)}</div></div>
    <div class="wide"><div class="small muted">允许的返回地址</div>
      <div class="small mono">{"<br>".join(e(u) for u in project.allowed_return_urls) or "未配置"}</div></div>
    <div class="wide"><div class="small muted">启用渠道</div>
      <div>{"".join(status_pill("paid", a.name) for a in accounts if selected.get(a.channel.value) == a.id) or "未启用"}</div></div>
  </div>
</section>
{settings}
<section class="card section">
  <h2>最近订单</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>业务单号</th><th>商品</th><th>金额</th><th>状态</th><th>创建</th></tr></thead>
    <tbody>{order_rows or '<tr><td colspan="5" class="muted">暂无订单</td></tr>'}</tbody>
  </table></div>
  <div style="margin-top:14px"><a class="btn secondary" href="/admin/orders?project_id={e(project.id)}">查看全部订单</a></div>
</section>
"""
    return _admin_page(
        project.name, body, principal=principal, nonce=nonce, csrf=csrf, active="projects"
    )


def render_channels(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    accounts: list[ChannelAccount],
    flash: str | None = None,
    error: str | None = None,
) -> str:
    is_admin = getattr(principal, "can_write", False)

    def toggle_form(account: ChannelAccount) -> str:
        if not is_admin:
            return ""
        label = "停用" if account.enabled else "启用"
        return (
            f'<form class="inline-form" method="post" '
            f'action="/admin/channels/{e(account.id)}/toggle">{_csrf(csrf)}'
            f'<button class="btn secondary" type="submit">{label}</button></form>'
        )

    rows = "".join(
        f"<tr><td class='mono'>{e(a.id)}</td><td>{e(a.channel.value)}</td><td>{e(a.name)}</td>"
        f"<td class='mono small'>{e(a.env_prefix)}</td>"
        f"<td>{status_pill('paid' if a.enabled else 'failed', '启用' if a.enabled else '停用')}</td>"
        f"<td>{toggle_form(a)}</td></tr>"
        for a in accounts
    )
    add_form = ""
    if is_admin:
        add_form = f"""
<section class="card section">
  <h2>添加渠道账户</h2>
  <p class="small muted">账户密钥不在此录入；服务会从环境变量的 prefix 读取。prefix 需以 <span class="mono">_</span> 结尾。</p>
  <form method="post" action="/admin/channels">
    {_csrf(csrf)}
    <div class="form-grid">
      <label class="field"><span>账户 ID</span><input type="text" name="account_id" required pattern="[a-z][a-z0-9_-]{{2,31}}" placeholder="wechat-default"></label>
      <label class="field"><span>渠道</span><select name="channel"><option value="wechat">微信支付</option><option value="alipay">支付宝</option></select></label>
      <label class="field"><span>显示名称</span><input type="text" name="name" required placeholder="微信支付"></label>
      <label class="field"><span>环境变量前缀</span><input type="text" name="env_prefix" required placeholder="PYPAY_WECHAT_"></label>
    </div>
    <button class="btn" type="submit">添加账户</button>
  </form>
</section>"""
    body = f"""
{_head("渠道账户", "商户凭据只从环境变量或外部密钥文件读取")}
{_flash(flash)}
{_flash(error, "error")}
<section class="card section">
  <div class="table-wrap"><table>
    <thead><tr><th>账户 ID</th><th>渠道</th><th>名称</th><th>环境变量前缀</th><th>状态</th><th></th></tr></thead>
    <tbody>{rows or '<tr><td colspan="6" class="muted">暂无渠道账户</td></tr>'}</tbody>
  </table></div>
</section>
{add_form}
"""
    return _admin_page(
        "渠道账户", body, principal=principal, nonce=nonce, csrf=csrf, active="channels"
    )


def render_orders(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    orders: list[PaymentOrder],
    projects: list[Project],
    filters: dict[str, str],
) -> str:
    project_options = ['<option value="">全部项目</option>']
    for project in projects:
        selected = " selected" if filters.get("project_id") == project.id else ""
        project_options.append(
            f'<option value="{e(project.id)}"{selected}>{e(project.name)}</option>'
        )
    status_options = ['<option value="">全部状态</option>']
    for status in ("pending", "paid", "closed", "expired", "refunded", "failed"):
        selected = " selected" if filters.get("status") == status else ""
        status_options.append(
            f'<option value="{e(status)}"{selected}>{e(_ORDER_LABELS.get(OrderStatus(status), status))}</option>'
        )
    rows = "".join(
        f"<tr><td class='small mono'>{e(fmt_dt(o.created_at))}</td><td>{e(o.project_id)}</td>"
        f"<td class='mono small'><a href='/admin/orders/{e(o.id)}'>{e(o.business_order_no)}</a></td>"
        f"<td>{e(o.subject)}</td><td class='mono'>{money(o.amount_fen)}</td>"
        f"<td>{status_pill(o.status.value, _ORDER_LABELS.get(o.status))}</td>"
        f"<td class='small mono'>{e(fmt_dt(o.paid_at) if o.paid_at else '—')}</td>"
        f"<td class='small mono'>{money(o.refunded_amount_fen)}</td></tr>"
        for o in orders
    )
    body = f"""
{_head("支付订单", "按项目、状态或业务单号筛选")}
<section class="card section">
  <form method="get" action="/admin/orders" class="form-grid" style="align-items:end">
    <label class="field"><span>项目</span><select name="project_id">{"".join(project_options)}</select></label>
    <label class="field"><span>状态</span><select name="status">{"".join(status_options)}</select></label>
    <label class="field"><span>业务单号</span><input type="text" name="business_order_no" value="{e(filters.get("business_order_no", ""))}"></label>
    <div class="field"><button class="btn" type="submit">筛选</button></div>
  </form>
</section>
<section class="card section">
  <div class="table-wrap"><table>
    <thead><tr><th>创建</th><th>项目</th><th>业务单号</th><th>商品</th><th>金额</th><th>状态</th><th>支付时间</th><th>已退款</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="8" class="muted">没有匹配的订单</td></tr>'}</tbody>
  </table></div>
</section>
"""
    return _admin_page(
        "支付订单", body, principal=principal, nonce=nonce, csrf=csrf, active="orders"
    )


def render_order_detail(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    order: PaymentOrder,
    project: Project | None,
    attempts: list[PaymentAttempt],
    refunds: list[Refund],
    event_rows: list[tuple[OutboxEvent, list[DeliveryRecord]]],
    notifications: list[Any],
    audit: list[AuditEntry],
    refund_key: str,
    flash: str | None = None,
    error: str | None = None,
) -> str:
    is_admin = getattr(principal, "can_write", False)
    attempts_html = "".join(
        f"<tr><td>{e(a.channel.value)}</td><td class='mono small'>{e(a.out_trade_no)}</td>"
        f"<td>{status_pill(a.status.value, _ATTEMPT_LABELS.get(a.status))}</td>"
        f"<td class='mono small'>{e(a.transaction_id or '—')}</td>"
        f"<td class='small'>{e(a.channel_status or '—')}</td>"
        f"<td class='small'>{e(fmt_dt(a.created_at))}</td></tr>"
        for a in attempts
    )
    refunds_html = "".join(
        f"<tr><td class='mono small'>{e(r.out_refund_no)}</td><td class='mono'>{money(r.amount_fen)}</td>"
        f"<td>{status_pill(r.status.value)}</td><td class='small'>{e(r.reason or '—')}</td>"
        f"<td class='small'>{e(fmt_dt(r.succeeded_at))}</td></tr>"
        for r in refunds
    )
    notif_html = "".join(
        f"<tr><td>{e(row['channel'])}</td><td class='mono small'>{e(row['notification_id'])}</td>"
        f"<td class='small'>{e(row['event_type'])}</td>"
        f"<td class='mono small'>{e(row['out_trade_no'])}</td>"
        f"<td class='mono small'>{e(row['out_refund_no'] or '—')}</td>"
        f"<td class='small'>{e(fmt_dt(_dt(row['received_at'])))}</td></tr>"
        for row in notifications
    )
    event_blocks = []
    for event, deliveries in event_rows:
        delivery_html = "".join(
            f"<tr><td>#{d.attempt_no}</td><td>{status_pill(d.status)}</td>"
            f"<td class='mono'>{e(d.http_status if d.http_status is not None else '—')}</td>"
            f"<td class='small'>{e(d.error or '')}</td>"
            f"<td class='small'>{e(fmt_dt(d.sent_at))}</td></tr>"
            for d in deliveries
        )
        resend = ""
        if is_admin:
            resend = f"""
<form class="inline-form" method="post" action="/admin/orders/{e(order.id)}/resend">
  {_csrf(csrf)}<input type="hidden" name="event_id" value="{e(event.event_id)}">
  <button class="btn secondary" type="submit">手动重发</button>
</form>"""
        event_blocks.append(
            f"""
<div style="border-top:1px solid var(--line);padding-top:14px;margin-top:14px">
  <div class="btn-row" style="justify-content:space-between">
    <div>
      <span class="mono small">{e(event.event_id)}</span>
      <span class="pill">{e(event.event_type.value)}</span>
      {status_pill(event.status.value)}
      <span class="small muted">业务确认：{e(event.ack_status.value)}</span>
    </div>
    {resend}
  </div>
  <div class="small muted" style="margin-top:6px">尝试 {event.attempts} 次 · 手动重发 {event.manual_resends} 次 · 下次 {e(fmt_dt(event.next_attempt_at))} · {e(event.last_error or "无错误")}</div>
  <div class="table-wrap" style="margin-top:8px"><table>
    <thead><tr><th>次数</th><th>结果</th><th>HTTP</th><th>错误</th><th>时间</th></tr></thead>
    <tbody>{delivery_html or '<tr><td colspan="5" class="muted">尚未投递</td></tr>'}</tbody>
  </table></div>
</div>"""
        )
    audit_html = "".join(
        f"<tr><td class='small'>{e(fmt_dt(a.created_at))}</td><td>{e(a.actor)}</td>"
        f"<td class='small'>{e(a.action)}</td><td class='small mono'>{e(json.dumps(a.details, ensure_ascii=False))}</td></tr>"
        for a in audit
    )
    actions = ""
    refund_form = ""
    if is_admin:
        close_form = ""
        if order.paid_at is None:
            close_form = f"""
<form class="inline-form" method="post" action="/admin/orders/{e(order.id)}/close">
  {_csrf(csrf)}<button class="btn danger" type="submit">关闭订单</button>
</form>"""
        actions = f"""
<form class="inline-form" method="post" action="/admin/orders/{e(order.id)}/refresh">
  {_csrf(csrf)}<button class="btn secondary" type="submit">主动查单</button>
</form>{close_form}"""
        if order.paid_at is not None:
            remaining = order.amount_fen - order.refunded_amount_fen
            if remaining > 0:
                refund_form = f"""
<section class="card section">
  <h2>发起退款</h2>
  <p class="small muted">可退余额 {money(remaining)}。退款请求会先落库，再调用渠道；超时不会直接判定失败。</p>
  <form method="post" action="/admin/orders/{e(order.id)}/refund">
    {_csrf(csrf)}
    <div class="form-grid">
      <label class="field"><span>退款金额（分）</span><input type="number" name="amount_fen" min="1" max="{remaining}" value="{remaining}" required></label>
      <label class="field"><span>幂等键</span><input type="text" name="idempotency_key" value="{e(refund_key)}" maxlength="64"></label>
      <label class="field wide"><span>原因</span><input type="text" name="reason" maxlength="80" placeholder="用户申请退款"></label>
    </div>
    <button class="btn danger" type="submit">提交退款</button>
  </form>
</section>"""
    metadata = json.dumps(dict(order.metadata), ensure_ascii=False, indent=2, default=str)
    body = f"""
{_head(f"订单 {order.business_order_no}", order.subject, actions)}
{_flash(flash)}
{_flash(error, "error")}
<section class="card section">
  <h2>订单快照</h2>
  <div class="form-grid">
    <div><div class="small muted">支付 ID</div><div class="mono small">{e(order.id)}</div></div>
    <div><div class="small muted">项目</div><div>{e(project.name if project else order.project_id)}</div></div>
    <div><div class="small muted">金额</div><div class="mono" style="font-size:22px">{money(order.amount_fen)}</div></div>
    <div><div class="small muted">状态</div><div>{status_pill(order.status.value, _ORDER_LABELS.get(order.status))}</div></div>
    <div><div class="small muted">创建</div><div class="small">{e(fmt_dt(order.created_at))}</div></div>
    <div><div class="small muted">过期</div><div class="small">{e(fmt_dt(order.expires_at))}</div></div>
    <div><div class="small muted">支付</div><div class="small">{e(fmt_dt(order.paid_at))}</div></div>
    <div><div class="small muted">已退款</div><div class="mono">{money(order.refunded_amount_fen)}</div></div>
    <div class="wide"><div class="small muted">返回地址</div><div class="mono small">{e(order.return_url or "未设置")}</div></div>
    <div class="wide"><div class="small muted">收银台</div><div class="mono small">/pay/{e(order.cashier_token)}</div></div>
    <div class="wide"><div class="small muted">metadata</div><pre class="credential" style="margin:6px 0 0">{e(metadata)}</pre></div>
  </div>
</section>
<section class="card section">
  <h2>支付尝试</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>渠道</th><th>商户单号</th><th>状态</th><th>渠道交易号</th><th>渠道状态</th><th>创建</th></tr></thead>
    <tbody>{attempts_html or '<tr><td colspan="6" class="muted">收银台尚未发起支付</td></tr>'}</tbody>
  </table></div>
</section>
<section class="card section">
  <h2>退款</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>退款单号</th><th>金额</th><th>状态</th><th>原因</th><th>成功时间</th></tr></thead>
    <tbody>{refunds_html or '<tr><td colspan="5" class="muted">暂无退款</td></tr>'}</tbody>
  </table></div>
</section>
{refund_form}
<section class="card section">
  <h2>渠道原始通知</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>渠道</th><th>通知 ID</th><th>事件</th><th>商户单号</th><th>退款单号</th><th>接收</th></tr></thead>
    <tbody>{notif_html or '<tr><td colspan="6" class="muted">暂无渠道通知</td></tr>'}</tbody>
  </table></div>
</section>
<section class="card section">
  <h2>业务事件与投递</h2>
  {"".join(event_blocks) or '<p class="muted">尚无业务事件</p>'}
</section>
<section class="card section">
  <h2>审计</h2>
  <div class="table-wrap"><table>
    <thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>详情</th></tr></thead>
    <tbody>{audit_html or '<tr><td colspan="4" class="muted">暂无记录</td></tr>'}</tbody>
  </table></div>
</section>
"""
    return _admin_page(
        f"订单 {order.business_order_no}",
        body,
        principal=principal,
        nonce=nonce,
        csrf=csrf,
        active="orders",
    )


def render_audit(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    entries: list[AuditEntry],
) -> str:
    rows = "".join(
        f"<tr><td class='small'>{e(fmt_dt(a.created_at))}</td><td>{e(a.actor)}</td>"
        f"<td>{e(a.role)}</td><td>{e(a.action)}</td><td class='mono small'>{e(a.project_id or '—')}</td>"
        f"<td class='mono small'>{e(a.order_id or '—')}</td>"
        f"<td class='small mono'>{e(json.dumps(a.details, ensure_ascii=False))}</td></tr>"
        for a in entries
    )
    body = f"""
{_head("审计日志", "管理后台的写操作都会记录")}
<section class="card section">
  <div class="table-wrap"><table>
    <thead><tr><th>时间</th><th>操作者</th><th>角色</th><th>动作</th><th>项目</th><th>订单</th><th>详情</th></tr></thead>
    <tbody>{rows or '<tr><td colspan="7" class="muted">暂无记录</td></tr>'}</tbody>
  </table></div>
</section>
"""
    return _admin_page(
        "审计日志", body, principal=principal, nonce=nonce, csrf=csrf, active="audit"
    )


def _dt(value: str):
    return from_db(value)


def render_login(*, nonce: str, csrf: str, error: str | None = None, username: str = "") -> str:
    body = f"""
<main class="login-shell">
  <section class="login-brand" aria-labelledby="login-title">
    <div>
      <div class="brand">
        <span class="brand-mark">PP</span>
        <span class="brand-name">pypay 管理台</span>
      </div>
      <p class="login-kicker" style="margin:32px 0 0">Unified payment operations</p>
      <h1 id="login-title">支付运营，<br>从一张账单开始。</h1>
      <p class="lede">项目、渠道、订单、通知投递与业务确认状态集中在一个后台。</p>
      <ul class="login-points">
        <li>项目级凭证与渠道账户隔离</li>
        <li>支付、投递、业务确认状态分开记录</li>
        <li>退款和人工操作全部留痕</li>
      </ul>
    </div>
    <div class="login-foot">pypay unified payment service</div>
  </section>
  <section class="login-panel">
    <div class="card login-card">
      <h2>登录管理台</h2>
      <p class="sub">使用环境变量中配置的管理员或只读账号。</p>
      {_flash(error, "error")}
      <form method="post" action="/admin/login">
        {_csrf(csrf)}
        <label class="field"><span>账号</span>
          <input type="text" name="username" value="{e(username)}" autocomplete="username"
                 required autofocus>
        </label>
        <label class="field"><span>密码</span>
          <input type="password" name="password" autocomplete="current-password" required>
        </label>
        <button class="btn accent login-submit" type="submit">进入管理台</button>
      </form>
      <div class="login-meta"><span>仅限授权运营人员</span><span>会话到期后需重新登录</span></div>
    </div>
  </section>
</main>"""
    return page_layout("登录 · pypay 管理台", body, nonce=nonce, css=LOGIN_CSS, login=True)


def render_docs_index(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    entries: list[DocEntry],
    docs_dir: str,
) -> str:
    cards = "".join(
        f'<a class="card section" href="/admin/docs/{quote(entry.slug, safe="/")}" '
        f'style="display:block;text-decoration:none;margin-bottom:12px">'
        f'<div class="small muted mono">{e(entry.slug)}.md</div>'
        f'<h2 style="margin:6px 0 4px">{e(entry.title)}</h2>'
        f'<p class="muted small" style="margin:0">{e(entry.excerpt)}</p></a>'
        for entry in entries
    )
    empty = ""
    if not entries:
        empty = f"""
<div class="warn-box" style="margin-bottom:16px">
  当前文档目录没有可读取的 Markdown 文件：<span class="mono">{e(docs_dir)}</span>
</div>
<p class="muted">设置 <span class="mono">PYPAY_SERVICE_DOCS_DIR</span> 指向文档目录，或把
<span class="mono">.md</span> 文件放进该目录。文件会自动出现在这里，无需改代码。</p>"""
    body = f"""
{_head("文档", "把 Markdown 文件放进文档目录即可自动出现在这里")}
{empty}
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:14px">
  {cards}
</div>
<p class="small muted" style="margin-top:22px">
  文档目录：<span class="mono">{e(docs_dir)}</span> · 支持子目录、表格、代码块和相对链接。
</p>
"""
    return _admin_page("文档", body, principal=principal, nonce=nonce, csrf=csrf, active="docs")


def render_doc(
    *,
    principal: Any,
    nonce: str,
    csrf: str,
    entries: list[DocEntry],
    entry: DocEntry,
    content_html: str,
) -> str:
    def doc_link(item: DocEntry) -> str:
        active = ' class="active"' if item.slug == entry.slug else ""
        return f'<a href="/admin/docs/{quote(item.slug, safe="/")}"{active}>{e(item.title)}</a>'

    nav = "".join(doc_link(item) for item in entries)
    body = f"""
{_head(entry.title, entry.slug + ".md", '<a class="btn secondary" href="/admin/docs">文档列表</a>')}
<div class="doc-layout">
  <nav class="card doc-nav" aria-label="文档目录">{nav}</nav>
  <article class="card doc-body">{content_html}</article>
</div>
"""
    return _admin_page(
        entry.title, body, principal=principal, nonce=nonce, csrf=csrf, active="docs"
    )

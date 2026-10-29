from __future__ import annotations

from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

DISPLAY_TZ = ZoneInfo("Asia/Shanghai")

CSS = """
:root {
  --paper: #f4efe6;
  --paper-2: #ebe4d8;
  --surface: #fffdf8;
  --ink: #1b1a17;
  --ink-soft: #4d473d;
  --muted: #7a7164;
  --line: #d9cfbf;
  --accent: #0f6b5c;
  --accent-ink: #f4fffc;
  --rust: #a94b22;
  --gold: #b07a1a;
  --danger: #9e2b25;
  --radius: 14px;
  --shadow: 10px 10px 0 rgba(27, 26, 23, 0.08);
  --text-display: ui-serif, "Songti SC", "Noto Serif SC", Georgia, serif;
  --text-body: ui-sans-serif, -apple-system, BlinkMacSystemFont, "PingFang SC",
    "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  --text-mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Menlo, monospace;
}
* { box-sizing: border-box; }
html { color-scheme: light; }
body {
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(1100px 520px at 8% -10%, #fff9ee 0%, transparent 62%),
    radial-gradient(900px 460px at 96% 6%, #e7f1ed 0%, transparent 58%),
    repeating-linear-gradient(0deg, rgba(27,26,23,.022) 0 1px, transparent 1px 5px),
    var(--paper);
  font: 16px/1.55 var(--text-body);
  min-height: 100vh;
}
a { color: var(--accent); text-underline-offset: 3px; }
a:hover { color: var(--rust); }
button, input, select, textarea { font: inherit; color: inherit; }
:focus-visible { outline: 3px solid rgba(15, 107, 92, .32); outline-offset: 2px; }
.sr-only {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0;
}
.mono { font-family: var(--text-mono); font-variant-numeric: tabular-nums; }
.muted { color: var(--muted); }
.small { font-size: 13px; }
.wrap { width: min(1120px, 92vw); margin: 0 auto; }
.masthead {
  display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
  padding: 28px 0 14px;
}
.brand { display: flex; align-items: center; gap: 10px; letter-spacing: .02em; }
.brand-mark {
  width: 26px; height: 26px; border-radius: 8px 8px 8px 2px;
  background: var(--ink); color: var(--accent-ink); display: grid; place-items: center;
  font-family: var(--text-mono); font-size: 12px;
}
.brand-name { font-weight: 700; }
.card {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}
.pill {
  display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px;
  border-radius: 999px; border: 1px solid var(--line); background: var(--paper-2);
  font-size: 12px; font-weight: 650; letter-spacing: .04em;
}
.pill.pending { background: #fdf3d8; border-color: #ead7a3; color: #6b4a0d; }
.pill.paid { background: #dff3ea; border-color: #a9d8c8; color: #0a5a4c; }
.pill.expired, .pill.closed, .pill.failed { background: #f2e2dd; border-color: #d9b7ad; color: #7e2c1c; }
.pill.refunded { background: #e8e5f6; border-color: #c4bde7; color: #40357e; }
.pill.dead { background: #f7dcd8; border-color: #dca9a1; color: #8b241c; }
.pill.delivered { background: #dff3ea; border-color: #a9d8c8; color: #0a5a4c; }
.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  min-height: 42px; padding: 9px 16px; border-radius: 10px;
  border: 1px solid var(--ink); background: var(--ink); color: var(--accent-ink);
  font-weight: 650; text-decoration: none; cursor: pointer;
  transition: transform 120ms ease, box-shadow 120ms ease, background 120ms ease;
}
.btn:hover { background: #322f29; color: #fff; transform: translateY(-1px); }
.btn:active { transform: translateY(0); box-shadow: inset 0 2px 0 rgba(0,0,0,.25); }
.btn.secondary { background: transparent; color: var(--ink); border-color: var(--line); }
.btn.secondary:hover { background: var(--paper-2); }
.btn.accent { background: var(--accent); border-color: var(--accent); }
.btn.danger { background: var(--danger); border-color: var(--danger); }
.btn:disabled { opacity: .45; cursor: not-allowed; transform: none; }
.btn-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
input[type=text], input[type=number], input[type=url], input[type=password],
select, textarea {
  width: 100%; padding: 10px 12px; border-radius: 9px; border: 1px solid var(--line);
  background: #fffdf9;
}
textarea { min-height: 96px; resize: vertical; }
label.field { display: grid; gap: 6px; margin-bottom: 14px; }
label.field > span { font-size: 13px; font-weight: 650; color: var(--ink-soft); }
.help { font-size: 12px; color: var(--muted); }
.error-box {
  padding: 10px 12px; border-radius: 10px; border: 1px solid #dca9a1;
  background: #fbece9; color: #8b241c;
}
.notice-box {
  padding: 10px 12px; border-radius: 10px; border: 1px solid #a9d8c8;
  background: #eaf7f1; color: #0a5a4c;
}
.warn-box {
  padding: 10px 12px; border-radius: 10px; border: 1px solid #ead7a3;
  background: #fdf6e4; color: #6b4a0d;
}
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
tbody tr:hover { background: rgba(235, 228, 216, .55); }
.table-wrap { overflow-x: auto; }
@media (prefers-reduced-motion: reduce) {
  * { animation-duration: .001ms !important; transition-duration: .001ms !important; }
}
"""

CASHIER_CSS = """
.cashier { padding: 8px 0 64px; }
.cashier-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(280px, .65fr); gap: 26px; align-items: start; }
.pay-card { position: relative; padding: 30px; overflow: hidden; }
.pay-card::before {
  content: ""; position: absolute; inset: 0 0 auto; height: 7px;
  background: linear-gradient(90deg, var(--accent), #3f8f7c 55%, var(--gold));
}
.project-line { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; color: var(--muted); font-size: 13px; }
.subject { font-family: var(--text-display); font-size: clamp(28px, 4.4vw, 44px); line-height: 1.12; margin: 16px 0 10px; letter-spacing: -.01em; }
.amount { font-family: var(--text-mono); font-size: clamp(34px, 6vw, 54px); font-weight: 700; letter-spacing: -.04em; margin: 4px 0 2px; }
.amount small { font-size: .42em; letter-spacing: .02em; color: var(--muted); font-weight: 500; }
.meta-row { display: flex; flex-wrap: wrap; gap: 18px 28px; margin: 18px 0 0; padding-top: 18px; border-top: 1px dashed var(--line); }
.meta-row div { display: grid; gap: 3px; }
.meta-row dt { font-size: 11px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
.meta-row dd { margin: 0; font-size: 14px; }
.countdown { color: var(--rust); font-weight: 650; }
.perforation { height: 22px; margin: 26px -30px; background:
  radial-gradient(circle at 0 50%, var(--paper) 0 8px, transparent 8.5px) left center / 16px 22px repeat-x; }
.channel-grid { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.channel-grid form { display: contents; }
.channel {
  display: flex; align-items: center; gap: 12px; padding: 15px; border-radius: 12px;
  border: 1px solid var(--line); background: #fffdf9; text-align: left; cursor: pointer;
  transition: border-color 120ms ease, transform 120ms ease, box-shadow 120ms ease;
}
.channel:hover { border-color: var(--accent); transform: translateY(-2px); box-shadow: 4px 4px 0 rgba(15,107,92,.10); }
.channel .icon { width: 38px; height: 38px; border-radius: 10px; display: grid; place-items: center; flex: none; background: var(--paper-2); }
.channel strong { display: block; }
.channel span { font-size: 12px; color: var(--muted); }
.channel[disabled] { opacity: .5; cursor: not-allowed; transform: none; box-shadow: none; }
.qr-panel { display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 22px; align-items: center; padding: 18px; border: 1px solid var(--line); border-radius: 12px; background: #fff; }
.qr-frame { padding: 12px; border: 1px solid var(--line); border-radius: 10px; background: #fff; display: grid; place-items: center; }
.qr-frame img { width: 166px; height: 166px; display: block; }
.form-panel { padding: 18px; border: 1px solid var(--line); border-radius: 12px; background: #fbf8f1; }
.side-card { padding: 22px; }
.side-card h2 { font-family: var(--text-display); font-size: 22px; margin: 0 0 6px; }
.timeline { list-style: none; margin: 16px 0 0; padding: 0; display: grid; gap: 12px; }
.timeline li { display: grid; grid-template-columns: 10px 1fr; gap: 10px; align-items: start; }
.timeline .dot { width: 10px; height: 10px; margin-top: 5px; border-radius: 50%; background: var(--line); }
.timeline .dot.done { background: var(--accent); }
.timeline .dot.warn { background: var(--gold); }
.state-banner { display: flex; gap: 12px; align-items: flex-start; padding: 16px; border-radius: 12px; margin-top: 20px; }
.state-banner.success { background: #eaf7f1; border: 1px solid #a9d8c8; }
.state-banner.pending { background: #fdf6e4; border: 1px solid #ead7a3; }
.state-banner.closed { background: #f7e9e5; border: 1px solid #d9b7ad; }
.state-banner h3 { margin: 0 0 2px; font-size: 16px; }
.state-banner p { margin: 0; color: var(--ink-soft); font-size: 14px; }
.sr-live { min-height: 1.4em; }
@media (max-width: 860px) {
  .cashier-grid { grid-template-columns: 1fr; }
  .pay-card { padding: 24px 20px; }
  .perforation { margin: 24px -20px; }
}
@media (max-width: 520px) {
  .channel-grid { grid-template-columns: 1fr; }
  .qr-panel { grid-template-columns: 1fr; }
  .qr-frame { justify-self: center; }
}
"""

ADMIN_CSS = """
.admin-shell { display: grid; grid-template-columns: 232px minmax(0, 1fr); min-height: 100vh; }
.admin-side { background: #201f1b; color: #f1ebdf; padding: 24px 18px; position: sticky; top: 0; height: 100vh; overflow: auto; }
.admin-side .brand-mark { background: var(--accent); }
.admin-side a { color: #d8d0c1; text-decoration: none; display: block; padding: 9px 12px; border-radius: 9px; font-size: 14px; }
.admin-side a:hover, .admin-side a.active { background: rgba(255,255,255,.09); color: #fff; }
.admin-side .nav-label { font-size: 11px; letter-spacing: .1em; text-transform: uppercase; color: #8f8678; margin: 18px 12px 6px; }
.admin-main { padding: 28px clamp(18px, 4vw, 44px) 64px; min-width: 0; }
.admin-head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 22px; }
.admin-head h1 { font-family: var(--text-display); font-size: clamp(26px, 3.4vw, 38px); margin: 0; letter-spacing: -.01em; }
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 24px; }
.kpi { padding: 16px; }
.kpi .k { font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
.kpi .v { font-family: var(--text-mono); font-size: 28px; font-weight: 700; margin-top: 4px; }
.section { padding: 22px; margin-bottom: 22px; }
.section h2 { font-family: var(--text-display); font-size: 21px; margin: 0 0 14px; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 18px; }
.form-grid .wide { grid-column: 1 / -1; }
.inline-form { display: inline; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
.code { font-family: var(--text-mono); font-size: 13px; word-break: break-all; }
.credential { padding: 12px; border-radius: 10px; background: #1f1e1a; color: #f4efe6; font-family: var(--text-mono); word-break: break-all; }
.subnav { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
.subnav a { padding: 6px 10px; border-radius: 999px; border: 1px solid var(--line); text-decoration: none; font-size: 13px; }
.subnav a.active { background: var(--ink); color: var(--accent-ink); border-color: var(--ink); }
@media (max-width: 860px) {
  .admin-shell { grid-template-columns: 1fr; }
  .admin-side { position: static; height: auto; display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
  .admin-side .nav-label { margin: 0 8px; }
  .form-grid { grid-template-columns: 1fr; }
}
"""


def e(value: object) -> str:
    return escape("" if value is None else str(value), quote=True)


def money(fen: int) -> str:
    return f"¥{fen // 100}.{fen % 100:02d}"


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")


def iso_utc(value: datetime) -> str:
    from .timeutil import to_db

    return to_db(value)


def status_pill(status: str, label: str | None = None) -> str:
    return f'<span class="pill {e(status)}">{e(label or status)}</span>'


def _nav(active: str | None) -> str:
    items = [
        ("dashboard", "/admin", "总览"),
        ("projects", "/admin/projects", "项目"),
        ("channels", "/admin/channels", "渠道账户"),
        ("orders", "/admin/orders", "支付订单"),
        ("audit", "/admin/audit", "审计日志"),
    ]
    links = []
    for key, href, label in items:
        cls = ' class="active"' if key == active else ""
        links.append(f'<a href="{e(href)}"{cls}>{e(label)}</a>')
    return "".join(links)


def page_layout(
    title: str,
    body: str,
    *,
    nonce: str,
    css: str = "",
    admin: bool = False,
    principal: object | None = None,
    active: str | None = None,
) -> str:
    if admin:
        username = getattr(principal, "username", "")
        role = getattr(principal, "role", "")
        shell = f"""
<div class="admin-shell">
  <aside class="admin-side">
    <div class="brand">
      <span class="brand-mark">PP</span>
      <span class="brand-name">pypay 管理台</span>
    </div>
    <div class="nav-label">运营</div>
    {_nav(active)}
    <div class="nav-label">身份</div>
    <div class="small" style="padding: 0 12px; color:#b9b0a2">
      {e(username)} · {e(role)}
    </div>
  </aside>
  <main class="admin-main">{body}</main>
</div>"""
    else:
        shell = f'<div class="wrap cashier">{body}</div>'
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{e(title)}</title>
<style nonce="{e(nonce)}">{CSS}{css}</style>
</head>
<body>{shell}</body>
</html>"""


def cashier_header(project_name: str, project_id: str) -> str:
    return f"""
<header class="masthead">
  <div class="brand">
    <span class="brand-mark">PP</span>
    <span class="brand-name">{e(project_name)}</span>
  </div>
  <div class="small muted mono">{e(project_id)}</div>
</header>"""

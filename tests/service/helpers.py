from __future__ import annotations

import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from pypay import (
    Channel,
    ClosePaymentResult,
    CreatePaymentResult,
    NotificationEvent,
    PaymentNotFoundError,
    PaymentNotification,
    PaymentStatus,
    QueryPaymentResult,
    QueryRefundResult,
    RefundResult,
    RefundStatus,
)
from pypay_service.app import create_app
from pypay_service.config import ChannelAccountSeed, ServiceConfig
from pypay_service.db import Database
from pypay_service.models import CreatedProject, ProjectAuth
from pypay_service.notifier import DeliveryResult
from pypay_service.projects_service import create_project, sync_channel_accounts
from pypay_service.security import SecretBox, cashier_form_token, request_signature
from pypay_service.worker import OutboxWorker

MASTER_KEY = b"m" * 32
PUBLIC_BASE_URL = "https://pay.test"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    def timestamp(self) -> int:
        return int(self.now.timestamp())


@dataclass
class FakeGateway:
    channel: Channel
    create_result: CreatePaymentResult | None = None
    create_error: Exception | None = None
    query_result: QueryPaymentResult | None = None
    query_error: Exception | None = None
    close_error: Exception | None = None
    refund_result: RefundResult | None = None
    refund_error: Exception | None = None
    query_refund_result: QueryRefundResult | None = None
    query_refund_error: Exception | None = None
    parse_error: Exception | None = None
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def _create(self, request) -> CreatePaymentResult:
        return CreatePaymentResult(
            channel=self.channel,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            status=PaymentStatus.PENDING,
            qr_code_url=(
                f"weixin://wxpay/bizpayurl?pr={request.out_trade_no}"
                if self.channel is Channel.WECHAT
                else None
            ),
            form_html=(
                f'<form action="https://openapi.alipay.test/gateway.do" method="post">'
                f'<input name="out_trade_no" value="{request.out_trade_no}"></form>'
                if self.channel is Channel.ALIPAY
                else None
            ),
        )

    async def create_payment(self, request) -> CreatePaymentResult:
        self.calls.append(("create_payment", request))
        if self.create_error:
            raise self.create_error
        return self.create_result or self._create(request)

    async def query_payment(self, out_trade_no: str) -> QueryPaymentResult:
        self.calls.append(("query_payment", out_trade_no))
        if self.query_error:
            raise self.query_error
        if self.query_result is None:
            raise PaymentNotFoundError("no query result")
        return self.query_result

    async def close_payment(self, out_trade_no: str) -> ClosePaymentResult:
        self.calls.append(("close_payment", out_trade_no))
        if self.close_error:
            raise self.close_error
        return ClosePaymentResult(
            channel=self.channel, out_trade_no=out_trade_no, status=PaymentStatus.CLOSED
        )

    async def refund(self, request) -> RefundResult:
        self.calls.append(("refund", request))
        if self.refund_error:
            raise self.refund_error
        if self.refund_result:
            return self.refund_result
        return RefundResult(
            channel=self.channel,
            out_refund_no=request.out_refund_no,
            out_trade_no=request.out_trade_no,
            amount_fen=request.amount_fen,
            total_amount_fen=request.total_amount_fen,
            status=RefundStatus.SUCCEEDED,
        )

    async def query_refund(
        self, out_refund_no: str, out_trade_no: str | None = None
    ) -> QueryRefundResult:
        self.calls.append(("query_refund", (out_refund_no, out_trade_no)))
        if self.query_refund_error:
            raise self.query_refund_error
        if self.query_refund_result is None:
            raise PaymentNotFoundError("no refund query result")
        return self.query_refund_result

    async def parse_notification(self, headers, body, **kwargs) -> PaymentNotification:
        self.calls.append(("parse_notification", body))
        if self.parse_error:
            raise self.parse_error
        data = json.loads(body or b"{}")
        return PaymentNotification(
            channel=self.channel,
            event_type=NotificationEvent(data.get("event_type", "payment.succeeded")),
            notification_id=data.get("notification_id", "notify-1"),
            out_trade_no=data["out_trade_no"],
            amount_fen=int(data.get("amount_fen", 100)),
            currency=data.get("currency", "CNY"),
            status=PaymentStatus(data.get("status", "paid")),
            transaction_id=data.get("transaction_id"),
            out_refund_no=data.get("out_refund_no"),
            refund_amount_fen=data.get("refund_amount_fen"),
        )


@dataclass
class FakeNotifier:
    responses: deque = field(default_factory=deque)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def queue(self, response: DeliveryResult) -> None:
        self.responses.append(response)

    def send(
        self,
        *,
        notify_url: str,
        notify_secret: str,
        event_id: str,
        event_type: str,
        body: bytes,
    ) -> DeliveryResult:
        self.calls.append(
            {
                "notify_url": notify_url,
                "notify_secret": notify_secret,
                "event_id": event_id,
                "event_type": event_type,
                "body": body,
                "payload": json.loads(body),
            }
        )
        if self.responses:
            response = self.responses.popleft()
            if isinstance(response, Exception):
                raise response
            return response
        return DeliveryResult(status_code=200, body_excerpt="ok")


@dataclass
class ServiceEnv:
    config: ServiceConfig
    db: Database
    box: SecretBox
    clock: Clock
    gateways: dict[str, FakeGateway]
    notifier: FakeNotifier
    clients: dict[str, TestClient]
    projects: dict[str, CreatedProject]

    def auth(self, project_id: str) -> ProjectAuth:
        created = self.projects[project_id]
        return ProjectAuth(
            project=created.project,
            api_secret=created.api_secret,
            notify_secret=created.notify_secret,
        )

    def gateway(self, account_id: str) -> FakeGateway:
        return self.gateways[account_id]

    def run_outbox(self) -> Any:
        worker = OutboxWorker(self.db, self.config, self.box, self.notifier, clock=self.clock)
        return worker.run_once(now=self.clock())


def build_env(
    tmp_path: Path,
    *,
    project_channels: dict[str, dict[Channel, str]] | None = None,
    service_docs_dir: Path | None = None,
) -> ServiceEnv:
    clock = Clock()
    seeds = (
        ChannelAccountSeed(
            id="wechat-default",
            channel=Channel.WECHAT,
            name="微信支付",
            env_prefix="PYPAY_WECHAT_",
        ),
        ChannelAccountSeed(
            id="alipay-default",
            channel=Channel.ALIPAY,
            name="支付宝",
            env_prefix="PYPAY_ALIPAY_",
        ),
    )
    config = ServiceConfig(
        database_path=tmp_path / "service.db",
        public_base_url=PUBLIC_BASE_URL,
        master_key=MASTER_KEY,
        admin_username="admin",
        admin_password="admin-password",
        viewer_username="viewer",
        viewer_password="viewer-password",
        service_docs_dir=service_docs_dir,
        outbox_max_attempts=3,
        outbox_backoff_base_seconds=10,
        outbox_lease_seconds=60,
        reconcile_grace_seconds=30,
        channel_accounts=seeds,
    )
    db = Database(config.database_path)
    db.migrate_up()
    sync_channel_accounts(db, config, clock())
    box = SecretBox(config.master_key)
    projects: dict[str, CreatedProject] = {}
    default_channels = {
        "project-a": {Channel.WECHAT: "wechat-default", Channel.ALIPAY: "alipay-default"},
        "project-b": {Channel.WECHAT: "wechat-default"},
    }
    selection = project_channels or default_channels
    for project_id, channels in selection.items():
        projects[project_id] = create_project(
            db,
            box,
            config,
            project_id=project_id,
            name=f"Project {project_id[-1].upper()}",
            notify_url=f"https://business-{project_id[-1]}.test/pypay/notify",
            return_urls=[f"https://business-{project_id[-1]}.test/return"],
            channels=channels,
            now=clock(),
        )
    gateways = {
        "wechat-default": FakeGateway(Channel.WECHAT),
        "alipay-default": FakeGateway(Channel.ALIPAY),
    }
    notifier = FakeNotifier()
    app = create_app(
        config,
        gateways=gateways,
        notifier=notifier,
        clock=clock,
        db=db,
        migrate=False,
    )
    clients = {
        "admin": TestClient(app, base_url="https://testserver"),
        "anon": TestClient(app, base_url="https://testserver"),
    }
    return ServiceEnv(
        config=config,
        db=db,
        box=box,
        clock=clock,
        gateways=gateways,
        notifier=notifier,
        clients=clients,
        projects=projects,
    )


def json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signed_headers(
    auth: ProjectAuth,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    nonce_value = nonce or secrets.token_hex(16)
    signature = request_signature(auth.api_secret, method, path, ts, nonce_value, body)
    return {
        "X-Pypay-Key-Id": auth.project.api_key_id,
        "X-Pypay-Timestamp": ts,
        "X-Pypay-Nonce": nonce_value,
        "X-Pypay-Signature": signature,
    }


def post_json(
    client: TestClient,
    auth: ProjectAuth,
    path: str,
    payload: dict[str, Any],
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
):
    body = json_body(payload)
    headers = signed_headers(auth, "POST", path, body, timestamp=timestamp, nonce=nonce)
    headers["Content-Type"] = "application/json"
    return client.post(path, content=body, headers=headers)


def get_json(
    client: TestClient,
    auth: ProjectAuth,
    path: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
):
    headers = signed_headers(auth, "GET", path, b"", timestamp=timestamp, nonce=nonce)
    return client.get(path, headers=headers)


def create_payment(
    client: TestClient,
    auth: ProjectAuth,
    *,
    business_order_no: str = "B-1",
    subject: str = "测试套餐",
    amount_fen: int = 100,
    expires_in_seconds: int = 1800,
    return_url: str | None = None,
    metadata: dict[str, Any] | None = None,
    timestamp: int | None = None,
    nonce: str | None = None,
):
    payload: dict[str, Any] = {
        "business_order_no": business_order_no,
        "subject": subject,
        "amount_fen": amount_fen,
        "expires_in_seconds": expires_in_seconds,
    }
    if return_url is not None:
        payload["return_url"] = return_url
    if metadata is not None:
        payload["metadata"] = metadata
    return post_json(client, auth, "/v1/payments", payload, timestamp=timestamp, nonce=nonce)


def cashier_path(cashier_url: str) -> str:
    return "/" + cashier_url.split("/", 3)[3]


def cashier_token(cashier_url: str) -> str:
    return cashier_url.rstrip("/").rsplit("/", 1)[1]


def form_token(env: ServiceEnv, token: str) -> str:
    return cashier_form_token(env.config.master_key, token)


def start_attempt(
    env: ServiceEnv,
    client: TestClient,
    token: str,
    channel: str,
    *,
    follow_redirects: bool = False,
):
    return client.post(
        f"/pay/{token}/start",
        data={"csrf_token": form_token(env, token), "channel": channel},
        follow_redirects=follow_redirects,
    )


def notify(
    client: TestClient,
    channel: str,
    account_id: str,
    *,
    out_trade_no: str,
    amount_fen: int = 100,
    notification_id: str | None = None,
    event_type: str = "payment.succeeded",
    status: str = "paid",
    out_refund_no: str | None = None,
    refund_amount_fen: int | None = None,
    transaction_id: str | None = None,
    currency: str = "CNY",
):
    payload: dict[str, Any] = {
        "notification_id": notification_id or "notify-" + secrets.token_hex(8),
        "out_trade_no": out_trade_no,
        "amount_fen": amount_fen,
        "event_type": event_type,
        "status": status,
        "currency": currency,
    }
    if out_refund_no is not None:
        payload["out_refund_no"] = out_refund_no
    if refund_amount_fen is not None:
        payload["refund_amount_fen"] = refund_amount_fen
    if transaction_id is not None:
        payload["transaction_id"] = transaction_id
    return client.post(
        f"/notify/{channel}/{account_id}",
        content=json_body(payload),
        headers={"Content-Type": "application/json"},
    )


def attempt_out_trade_no(client: TestClient, auth: ProjectAuth, payment_id: str) -> str:
    response = get_json(client, auth, f"/v1/payments/{payment_id}")
    assert response.status_code == 200, response.text
    attempts = response.json()["attempts"]
    assert attempts
    return attempts[-1]["payment_order_no"]


def payment_response(client: TestClient, auth: ProjectAuth, payment_id: str) -> dict[str, Any]:
    response = get_json(client, auth, f"/v1/payments/{payment_id}")
    assert response.status_code == 200, response.text
    return response.json()

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from pypay import PaymentError

from .config import ServiceConfig
from .db import Database
from .enums import AckStatus
from .errors import ServiceError
from .gateways import GatewayRegistry
from .notifier import Notifier
from .payments_service import expire_orders, refresh_order
from .refunds_service import reconcile_pending_refunds
from .repo_orders import list_orders_for_reconcile
from .repo_outbox import (
    claim_event,
    get_event,
    list_due_event_ids,
    mark_dead,
    mark_delivered,
    mark_retry,
    record_delivery,
    reset_stale_leases,
)
from .repo_projects import get_project_row
from .security import SecretBox, SecurityError
from .timeutil import utcnow

logger = logging.getLogger("pypay_service.worker")


@dataclass(frozen=True)
class WorkerReport:
    attempted: int = 0
    delivered: int = 0
    retried: int = 0
    dead: int = 0


@dataclass(frozen=True)
class MaintenanceReport:
    expired: int = 0
    queried: int = 0
    refunds_reconciled: int = 0


class OutboxWorker:
    def __init__(
        self,
        db: Database,
        config: ServiceConfig,
        box: SecretBox,
        notifier: Notifier,
        *,
        clock=utcnow,
    ) -> None:
        self._db = db
        self._config = config
        self._box = box
        self._notifier = notifier
        self._clock = clock

    def run_once(self, now: datetime | None = None) -> WorkerReport:
        current = now or self._clock()
        with self._db.transaction() as conn:
            reset_stale_leases(conn, current)
        with self._db.read() as conn:
            due = list_due_event_ids(conn, current, self._config.outbox_batch_size)
        attempted = delivered = retried = dead = 0
        for event_id in due:
            outcome = self._deliver_one(event_id, current)
            if outcome is None:
                continue
            attempted += 1
            if outcome == "delivered":
                delivered += 1
            elif outcome == "dead":
                dead += 1
            else:
                retried += 1
        return WorkerReport(attempted=attempted, delivered=delivered, retried=retried, dead=dead)

    def _deliver_one(self, event_id: str, now: datetime) -> str | None:
        lease_until = now + timedelta(seconds=self._config.outbox_lease_seconds)
        with self._db.transaction() as conn:
            claimed = claim_event(conn, event_id, lease_until, now)
            event = get_event(conn, event_id)
            project_row = get_project_row(conn, event.project_id) if event else None
        if not claimed or event is None:
            return None
        if project_row is None:
            self._finish_dead(event_id, event.attempts + 1, "project not found", now)
            return "dead"
        try:
            notify_secret = self._box.decrypt(project_row["notify_secret_enc"])
        except SecurityError:
            self._finish_dead(event_id, event.attempts + 1, "notify secret could not be read", now)
            return "dead"

        body = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        result = self._notifier.send(
            notify_url=project_row["notify_url"],
            notify_secret=notify_secret,
            event_id=event.event_id,
            event_type=event.event_type.value,
            body=body,
        )
        sent_at = self._clock()
        attempts = event.attempts + 1
        if result.delivered:
            with self._db.transaction() as conn:
                record_delivery(
                    conn,
                    event_id=event_id,
                    attempt_no=attempts,
                    status="delivered",
                    http_status=result.status_code,
                    response_excerpt=result.body_excerpt,
                    error=None,
                    duration_ms=result.duration_ms,
                    sent_at=sent_at,
                )
                mark_delivered(conn, event_id, attempts, sent_at)
            return "delivered"

        error = result.error or f"HTTP {result.status_code}"
        if result.permanent_failure:
            self._finish_dead(
                event_id,
                attempts,
                error,
                sent_at,
                http_status=result.status_code,
                body_excerpt=result.body_excerpt,
                duration_ms=result.duration_ms,
                rejected=True,
            )
            return "dead"

        if attempts >= self._config.outbox_max_attempts:
            self._finish_dead(
                event_id,
                attempts,
                error,
                sent_at,
                http_status=result.status_code,
                body_excerpt=result.body_excerpt,
                duration_ms=result.duration_ms,
                rejected=False,
            )
            return "dead"

        backoff = min(
            self._config.outbox_backoff_base_seconds * (2 ** (attempts - 1)),
            3600.0,
        )
        with self._db.transaction() as conn:
            record_delivery(
                conn,
                event_id=event_id,
                attempt_no=attempts,
                status="retry",
                http_status=result.status_code,
                response_excerpt=result.body_excerpt,
                error=error,
                duration_ms=result.duration_ms,
                sent_at=sent_at,
            )
            mark_retry(
                conn,
                event_id,
                attempts=attempts,
                next_attempt_at=sent_at + timedelta(seconds=backoff),
                last_error=error,
                now=sent_at,
            )
        return "retry"

    def _finish_dead(
        self,
        event_id: str,
        attempts: int,
        error: str,
        now: datetime,
        *,
        http_status: int | None = None,
        body_excerpt: str | None = None,
        duration_ms: int | None = None,
        rejected: bool = False,
    ) -> None:
        with self._db.transaction() as conn:
            record_delivery(
                conn,
                event_id=event_id,
                attempt_no=attempts,
                status="dead",
                http_status=http_status,
                response_excerpt=body_excerpt,
                error=error,
                duration_ms=duration_ms,
                sent_at=now,
            )
            mark_dead(
                conn,
                event_id,
                attempts=attempts,
                last_error=error,
                ack_status=AckStatus.REJECTED if rejected else AckStatus.PENDING,
                now=now,
            )
        logger.error("outbox event %s is dead after %s attempts: %s", event_id, attempts, error)

    def run_forever(self) -> None:
        logger.info("outbox worker started")
        while True:
            try:
                report = self.run_once()
                if report.attempted:
                    logger.info(
                        "outbox pass attempted=%s delivered=%s retried=%s dead=%s",
                        report.attempted,
                        report.delivered,
                        report.retried,
                        report.dead,
                    )
            except Exception:
                logger.exception("outbox pass failed")
            time.sleep(self._config.worker_poll_interval_seconds)


class MaintenanceWorker:
    def __init__(
        self,
        db: Database,
        config: ServiceConfig,
        gateways: GatewayRegistry,
        *,
        clock=utcnow,
    ) -> None:
        self._db = db
        self._config = config
        self._gateways = gateways
        self._clock = clock

    async def run_once(self, now: datetime | None = None) -> MaintenanceReport:
        current = now or self._clock()
        expired = await self._expire(current)
        queried = await self._reconcile_orders(current)
        refunds = await self._reconcile_refunds(current)
        return MaintenanceReport(expired=expired, queried=queried, refunds_reconciled=refunds)

    async def _expire(self, now: datetime) -> int:
        try:
            return expire_orders(self._db, self._config, now=now)
        except Exception:
            logger.exception("expiry sweep failed")
            return 0

    async def _reconcile_orders(self, now: datetime) -> int:
        grace = timedelta(seconds=self._config.reconcile_grace_seconds)
        earliest = now - timedelta(days=7)
        with self._db.read() as conn:
            orders = list_orders_for_reconcile(
                conn,
                created_before=now - grace,
                created_after=earliest,
                limit=50,
            )
        queried = 0
        for order in orders:
            try:
                await refresh_order(
                    self._db,
                    self._config,
                    self._gateways,
                    project_id=order.project_id,
                    payment_id=order.id,
                    now=now,
                )
                queried += 1
            except (PaymentError, ServiceError):
                logger.warning("reconcile failed for %s", order.id)
            except Exception:
                logger.exception("reconcile crashed for %s", order.id)
        return queried

    async def _reconcile_refunds(self, now: datetime) -> int:
        try:
            return await reconcile_pending_refunds(self._db, self._config, self._gateways, now=now)
        except Exception:
            logger.exception("refund reconcile failed")
            return 0

    async def run_forever(self) -> None:
        logger.info("maintenance worker started")
        while True:
            try:
                report = await self.run_once()
                if report.expired or report.queried or report.refunds_reconciled:
                    logger.info(
                        "maintenance expired=%s queried=%s refunds=%s",
                        report.expired,
                        report.queried,
                        report.refunds_reconciled,
                    )
            except Exception:
                logger.exception("maintenance pass failed")
            await asyncio.sleep(self._config.maintenance_interval_seconds)


class PaymentWorker:
    """Runs outbox delivery and periodic reconciliation in one process."""

    def __init__(
        self,
        db: Database,
        config: ServiceConfig,
        box: SecretBox,
        notifier: Notifier,
        gateways: GatewayRegistry,
        *,
        clock=utcnow,
    ) -> None:
        self._outbox = OutboxWorker(db, config, box, notifier, clock=clock)
        self._maintenance = MaintenanceWorker(db, config, gateways, clock=clock)
        self._config = config

    async def run_once(self, now: datetime | None = None):
        outbox_report = await asyncio.to_thread(self._outbox.run_once, now)
        maintenance_report = await self._maintenance.run_once(now)
        return outbox_report, maintenance_report

    async def run_forever(self) -> None:
        logger.info("payment worker started")
        next_maintenance = time.monotonic()
        while True:
            try:
                await asyncio.to_thread(self._outbox.run_once)
            except Exception:
                logger.exception("outbox pass failed")
            if time.monotonic() >= next_maintenance:
                try:
                    await self._maintenance.run_once()
                except Exception:
                    logger.exception("maintenance pass failed")
                next_maintenance = time.monotonic() + self._config.maintenance_interval_seconds
            await asyncio.sleep(self._config.worker_poll_interval_seconds)

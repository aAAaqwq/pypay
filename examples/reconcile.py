"""Query every pending order and apply a paid or closed provider state.

Run this on a schedule to compensate for lost asynchronous notifications.
"""

import asyncio
import logging

from pypay import Channel, PaymentError, PaymentNotFoundError, PaymentStatus

from .db import AmountMismatch, Database
from .fastapi_app import gateways_from_env

logger = logging.getLogger("pypay.reconcile")


async def reconcile(db_path: str = "pypay_demo.db") -> None:
    database = Database(db_path)
    gateways = gateways_from_env()
    for order in database.pending_orders():
        gateway = gateways.get(Channel(order.channel))
        if gateway is None:
            logger.warning("no gateway configured for %s", order.channel)
            continue
        try:
            result = await gateway.query_payment(order.out_trade_no)
        except PaymentNotFoundError:
            continue
        except PaymentError as exc:
            logger.warning("query failed for %s: %s", order.out_trade_no, type(exc).__name__)
            continue
        try:
            if result.status is PaymentStatus.PAID:
                database.mark_paid_from_query(
                    out_trade_no=order.out_trade_no,
                    amount_fen=result.amount_fen,
                    transaction_id=result.transaction_id,
                    paid_at=result.paid_at.isoformat() if result.paid_at else None,
                )
            elif result.status is PaymentStatus.CLOSED:
                database.mark_closed_from_query(order.out_trade_no)
            elif result.status is PaymentStatus.REFUNDED:
                database.mark_refunded_from_query(order.out_trade_no)
        except AmountMismatch:
            logger.warning("provider amount mismatch for %s", order.out_trade_no)


if __name__ == "__main__":
    asyncio.run(reconcile())

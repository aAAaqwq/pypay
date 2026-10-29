import secrets
from datetime import datetime, timezone


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")


def new_payment_id() -> str:
    return "pay_" + secrets.token_hex(12)


def new_attempt_id() -> str:
    return "att_" + secrets.token_hex(12)


def new_refund_id() -> str:
    return "ref_" + secrets.token_hex(12)


def new_event_id() -> str:
    return "evt_" + secrets.token_hex(12)


def new_cashier_token() -> str:
    return secrets.token_urlsafe(32)


def new_out_trade_no() -> str:
    # 2 + 12 + 12 = 26 characters, inside the 32-character channel limit.
    return "PP" + _stamp() + secrets.token_hex(6).upper()


def new_out_refund_no() -> str:
    return "RF" + _stamp() + secrets.token_hex(6).upper()


def new_api_key_id() -> str:
    return "pk_" + secrets.token_hex(8)


def new_secret() -> str:
    return secrets.token_urlsafe(32)

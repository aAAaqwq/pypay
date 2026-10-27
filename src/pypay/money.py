from decimal import Decimal, InvalidOperation

from .errors import PaymentValidationError

_CENT = Decimal("0.01")


def validate_fen(value: object, field: str = "amount_fen") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PaymentValidationError(f"{field} must be an integer number of fen")
    if value <= 0:
        raise PaymentValidationError(f"{field} must be greater than zero")
    return value


def fen_to_yuan(value: int) -> str:
    validate_fen(value)
    return format((Decimal(value) / Decimal(100)).quantize(_CENT), "f")


def yuan_to_fen(value: object) -> int:
    if isinstance(value, bool):
        raise PaymentValidationError("amount must be a decimal string")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PaymentValidationError("amount must be a decimal string") from exc
    if not amount.is_finite():
        raise PaymentValidationError("amount must be finite")
    if amount <= 0:
        raise PaymentValidationError("amount must be greater than zero")
    fen = amount * 100
    if fen != fen.to_integral_value():
        raise PaymentValidationError("amount must not contain sub-fen precision")
    return int(fen)

import pytest

from pypay.errors import PaymentValidationError
from pypay.money import fen_to_yuan, validate_fen, yuan_to_fen


@pytest.mark.parametrize(
    ("fen", "yuan"),
    [(1, "0.01"), (10, "0.10"), (100, "1.00"), (12345, "123.45"), (1000000, "10000.00")],
)
def test_fen_to_yuan(fen: int, yuan: str) -> None:
    assert fen_to_yuan(fen) == yuan


@pytest.mark.parametrize(
    ("yuan", "fen"),
    [("0.01", 1), ("0.1", 10), ("1", 100), ("1.5", 150), ("123.45", 12345)],
)
def test_yuan_to_fen(yuan: str, fen: int) -> None:
    assert yuan_to_fen(yuan) == fen


@pytest.mark.parametrize("value", ["0.001", "-1.00", "0", "abc", "NaN", "Infinity"])
def test_yuan_to_fen_rejects_invalid_amounts(value: str) -> None:
    with pytest.raises(PaymentValidationError):
        yuan_to_fen(value)


@pytest.mark.parametrize("value", [True, False, 1.5, "1", None])
def test_validate_fen_rejects_non_integers(value: object) -> None:
    with pytest.raises(PaymentValidationError):
        validate_fen(value)  # type: ignore[arg-type]


def test_validate_fen_rejects_non_positive() -> None:
    with pytest.raises(PaymentValidationError):
        validate_fen(0)
    with pytest.raises(PaymentValidationError):
        validate_fen(-1)

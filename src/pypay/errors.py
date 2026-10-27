class PaymentError(Exception):
    """Base class for every error raised by pypay."""


class PaymentConfigError(PaymentError):
    """Configuration is missing, ambiguous, or cannot be loaded."""


class PaymentValidationError(PaymentError):
    """Local input or provider data fails a required business check."""


class PaymentSignatureError(PaymentError):
    """A response or notification signature could not be verified."""


class PaymentProviderError(PaymentError):
    """The provider rejected the request or returned an error response."""

    def __init__(
        self,
        message: str,
        *,
        provider_code: str | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_code = provider_code
        self.http_status = http_status


class PaymentNotFoundError(PaymentProviderError):
    """The provider has no record of the requested order or refund."""


class PaymentNetworkError(PaymentError):
    """A transport failure occurred.

    A timeout does not mean the payment or refund failed. The request may have
    been accepted. Query by the original merchant order number before retrying
    or treating the operation as failed.
    """

    def __init__(self, message: str, *, may_have_succeeded: bool = True) -> None:
        super().__init__(message)
        self.may_have_succeeded = may_have_succeeded

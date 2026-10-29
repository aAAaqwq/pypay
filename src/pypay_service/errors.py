from __future__ import annotations


class ServiceError(Exception):
    code = "service_error"
    http_status = 500

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ValidationError(ServiceError):
    code = "validation_error"
    http_status = 400


class AuthenticationError(ServiceError):
    code = "authentication_error"
    http_status = 401


class ForbiddenError(ServiceError):
    code = "forbidden"
    http_status = 403


class NotFoundError(ServiceError):
    code = "not_found"
    http_status = 404


class ConflictError(ServiceError):
    code = "conflict"
    http_status = 409


class ProviderError(ServiceError):
    code = "provider_error"
    http_status = 502


class AmbiguousResult(ServiceError):
    code = "ambiguous_result"
    http_status = 202

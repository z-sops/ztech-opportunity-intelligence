"""Engine error model. Every error crossing a boundary carries a stable code."""

from __future__ import annotations

from typing import Any

from .taxonomy import ErrorCode


class EngineError(Exception):
    """Base error. `code` is part of the public contract."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        provider: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider = provider
        self.retryable = retryable
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code.value, "message": self.message, "retryable": self.retryable}
        if self.provider:
            out["provider"] = self.provider
        if self.details:
            out["details"] = self.details
        return out


class ValidationFailed(EngineError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, details=details)


class UnsafeTarget(EngineError):
    """Raised by the URL guard (invalid URL, private network, bad scheme)."""


class FetchError(EngineError):
    """Raised by the HTTP layer; providers convert it into an honest status."""


class NotFound(EngineError):
    def __init__(self, message: str) -> None:
        super().__init__(ErrorCode.NOT_FOUND, message)

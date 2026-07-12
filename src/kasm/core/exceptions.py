"""Domain-specific exceptions."""


class KasmError(Exception):
    """Base exception for the package."""


class ValidationError(KasmError, ValueError):
    """Raised when a domain object is invalid."""


class NotFoundError(KasmError, LookupError):
    """Raised when a requested record does not exist."""

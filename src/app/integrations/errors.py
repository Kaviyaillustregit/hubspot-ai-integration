class IntegrationError(Exception):
    """Normalized base error for external integration failures."""


class IntegrationAuthenticationError(IntegrationError):
    pass


class IntegrationRateLimitError(IntegrationError):
    pass


class IntegrationTimeoutError(IntegrationError):
    pass


class IntegrationPermissionError(IntegrationError):
    """The provider refused the call for lack of permission (e.g. a missing OAuth scope)."""

    def __init__(self, message: str, *, scope: str | None = None) -> None:
        super().__init__(message)
        self.scope = scope


class IntegrationNotFoundError(IntegrationError):
    pass

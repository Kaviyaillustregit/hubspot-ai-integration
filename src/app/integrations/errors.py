class IntegrationError(Exception):
    """Normalized base error for external integration failures."""


class IntegrationAuthenticationError(IntegrationError):
    pass


class IntegrationRateLimitError(IntegrationError):
    pass


class IntegrationTimeoutError(IntegrationError):
    pass

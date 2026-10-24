class QQError(ValueError):
    """An error safe to pass across the Agent protocol boundary."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

"""Domain errors. The API layer maps these onto HTTP status codes."""


class DomainError(Exception):
    def __init__(self, message: str, **extra):
        super().__init__(message)
        self.message = message
        self.extra = extra


class Unauthorized(DomainError):
    pass


class Forbidden(DomainError):
    pass


class NotFound(DomainError):
    pass


class Conflict(DomainError):
    pass

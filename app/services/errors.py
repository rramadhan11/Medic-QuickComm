from app.engine.selection import Plan


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str, plan: Plan | None = None):
        super().__init__(message)
        self.message = message
        self.plan = plan


class NotFound(DomainError):
    status_code = 404


class Conflict(DomainError):
    status_code = 409


class Unprocessable(DomainError):
    status_code = 422

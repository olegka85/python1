class BusinessError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


class NotFound(BusinessError):
    def __init__(self, resource: str):
        super().__init__("not_found", f"{resource} not found", 404)

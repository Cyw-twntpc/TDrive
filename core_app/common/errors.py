class TDriveError(Exception):
    """Base exception class."""
    pass

class PathNotFoundError(TDriveError):
    """Path does not exist."""
    pass

class ItemAlreadyExistsError(TDriveError):
    """Item already exists."""
    pass

class InvalidNameError(TDriveError):
    """Invalid name used."""
    pass

class InvalidOperationError(TDriveError):
    """Operation is not allowed."""
    pass

class ErrorCode:
    """Standardized error codes for frontend communication."""
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    ITEM_ALREADY_EXISTS = "ITEM_ALREADY_EXISTS"
    INVALID_OPERATION = "INVALID_OPERATION"
    DB_READ_FAILED = "DB_READ_FAILED"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    FLOOD_WAIT_ERROR = "FLOOD_WAIT_ERROR"
    TASK_FAILED = "TASK_FAILED"
    ASYNC_CALL_FAILED = "ASYNC_CALL_FAILED"
    BUSY = "BUSY"
class TDriveError(Exception):
    """Base exception class for all custom exceptions in this project."""
    pass

class PathNotFoundError(TDriveError):
    """Raised when an operation targets a file or folder path that does not exist."""
    pass

class ItemAlreadyExistsError(TDriveError):
    """Raised when attempting to create a file or folder with a name that already exists at the target location."""
    pass

class InvalidNameError(TDriveError):
    """Raised when an invalid name (e.g., containing illegal characters) is used for a file or folder."""
    pass

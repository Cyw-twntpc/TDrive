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

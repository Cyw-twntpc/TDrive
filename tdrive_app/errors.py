class TDriveError(Exception):
    """專案的基礎錯誤類別"""
    pass

class PathNotFoundError(TDriveError):
    """當操作的路徑不存在時拋出"""
    pass

class ItemAlreadyExistsError(TDriveError):
    """當試圖建立的檔案/資料夾名稱已存在時拋出"""
    pass

class InvalidNameError(TDriveError):
    """當試圖使用無效的名稱時拋出"""
    pass

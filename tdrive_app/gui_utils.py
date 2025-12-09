from PySide6.QtWidgets import QFileDialog

def core_select_files(multiple=False, title="選取檔案", initial_dir=None):
    """
    開啟一個用於選擇單一或多個檔案的對話方塊的核心邏輯 (使用 PySide6)。
    """
    # 確保 initial_dir 不是 None，雖然 Qt 通常可以處理 None，但給空字串更保險
    start_dir = initial_dir if initial_dir else ""

    # parent 設為 None，因為這是一個工具函式，它會作為頂層視窗彈出
    # 或者依附於當前活躍的 Application 實例
    
    if multiple:
        # getOpenFileNames 返回 (檔案列表, 篩選器字串)
        file_paths, _ = QFileDialog.getOpenFileNames(
            None, 
            title, 
            start_dir, 
            "All Files (*)"
        )
        return file_paths
    else:
        # getOpenFileName 返回 (檔案路徑, 篩選器字串)
        file_path, _ = QFileDialog.getOpenFileName(
            None, 
            title, 
            start_dir, 
            "All Files (*)"
        )
        return [file_path] if file_path else []

def core_select_directory(title="選取資料夾", initial_dir=None):
    """
    開啟一個用於選擇資料夾的對話方塊的核心邏輯 (使用 PySide6)。
    """
    start_dir = initial_dir if initial_dir else ""
    
    # getExistingDirectory 只返回路徑字串
    folder_path = QFileDialog.getExistingDirectory(
        None, 
        title, 
        start_dir
    )
    
    return folder_path if folder_path else ""